"""ic crew：团队编排。

up       按 ~/.infocenter/config.json 逐 agent 拉起：
         launch → 每项目一个 tmux 会话（ic-crew-<hash>）每人一窗（无 tmux 退化 osascript 开 Terminal）
         app    → `open -a <app> <项目目录>`
         headless → 启动收信守护（watch）：状态键 = 身份 × 项目，同一身份可在多个项目各跑一个守护；
                    守护长轮询 /v1/events（带 workspace 范围），只处理属于本项目的信
kickoff  以 coordinator（或 --from 指定身份）向其他所有 agent 发 high 启动信（分工 + 合作约定），
         workspace 记项目目录
status   查看守护进程、tmux 会话、config 启动方式（--dir 只看某个项目）
down     杀守护进程、关 tmux 会话（--dir 只清某个项目）
watch    守护进程本体（由 up 派生，不面向用户）
"""

import json
import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from infocenter import config
from infocenter.client import ApiError, request
from infocenter.store import MailError, normalize_name, utcnow
from infocenter.paths import ROOT

SESSION_PREFIX = "ic-crew"
DEFAULT_POLL = 300

CONVENTIONS = """---
合作约定（kickoff 自动附加）：
1. 本信由 coordinator {coordinator} 发出。coordinator 的分工裁定优先执行；有异议只回一条针对冲突点的信，不要并行改文件。
2. 单一写者原则：每个文件/文档只有一个 owner agent 可写；其他人只以留言发 diff 建议，由 owner 合入。
3. 规划纪律：PLAN.md（任务分解）由 coordinator 独家产出和维护，每项任务带 owner、交付物、验收标准、依赖；各成员只把进展写进自己的 TODO-<名字>.md；要调整计划就给 coordinator 发信，不要直接改 PLAN.md。
4. 状态自报：开始/切换/完成任务时更新自己的 activity（MCP 调 ic_activity，CLI 用 ic register --activity "一句话状态"）。coordinator 想知道各家在做什么，直接看 `ic agents`，**不要发问询信**；要干预、要调配才发信。
5. 礼仪：无新信息不回帖，禁止 +1 附议；同一分歧最多两轮，之后交 coordinator/human 裁定。
6. 收到本信后先回一条确认认领的分工，再开始动手。"""


def _key(agent: str, project: str) -> str:
    """守护状态键 = 身份 × 项目。同一身份在每个项目各有一个守护，互不抢占。"""
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:8]
    return f"{agent}--{digest}"


def _session_name(project: str) -> str:
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:6]
    return f"{SESSION_PREFIX}-{digest}"


def _window_name(project: str, agent: str) -> str:
    return f"{Path(project).name}-{agent}"[:60]


def state_dir(home) -> Path:
    path = Path(home) / "crew"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_file(home, key: str) -> Path:
    return state_dir(home) / f"{key}.pid"


def _meta_file(home, key: str) -> Path:
    return state_dir(home) / f"{key}.json"


def _cursor_file(home, key: str) -> Path:
    return state_dir(home) / f"{key}.cursor"


def log_file(home, key: str) -> Path:
    return state_dir(home) / f"{key}.log"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _read_pid(home, key: str) -> int | None:
    try:
        pid = int(_pid_file(home, key).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _read_meta(home, key: str) -> dict:
    try:
        return json.loads(_meta_file(home, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_dir(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser().resolve())


def _select(home, only: str) -> list[dict]:
    entries = config.load(home)["agents"]
    if not only.strip():
        return entries
    wanted = [normalize_name(part) for part in only.split(",") if part.strip()]
    by_name = {item["name"]: item for item in entries}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise MailError("unknown_agent", f"config.json 里没有：{', '.join(missing)}")
    return [by_name[name] for name in wanted]


def _tmux_windows(session: str) -> set[str]:
    if not shutil.which("tmux"):
        return set()
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _tmux_sessions() -> set[str]:
    if not shutil.which("tmux"):
        return set()
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    return {
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(SESSION_PREFIX)
    }


def _tmux_session_exists(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    result = subprocess.run(
        ["tmux", "has-session", "-t", session], capture_output=True, text=True,
    )
    return result.returncode == 0


def plan_up(home, url: str, project: str, only: str = "") -> list[dict]:
    """生成 up 的动作清单。每项：{agent, kind, command(list), note}。"""
    actions: list[dict] = []
    use_tmux = shutil.which("tmux") is not None
    session = _session_name(project)
    existing = _tmux_windows(session)
    for item in _select(home, only):
        agent = item["name"]
        app = str(item.get("app") or "").strip()
        launch = str(item.get("launch") or "").strip()
        headless = str(item.get("headless") or "").strip()
        if app:
            actions.append({"agent": agent, "kind": "app", "command": ["open", "-a", app, project]})
        if launch:
            if use_tmux:
                window = _window_name(project, agent)
                if window in existing:
                    actions.append({"agent": agent, "kind": "skip", "command": [], "note": f"tmux 窗口 {window} 已存在"})
                else:
                    actions.append({
                        "agent": agent, "kind": "tmux",
                        "command": ["tmux", "new-window", "-t", session, "-n", window, "-c", project, launch],
                    })
            else:
                inner = f"cd {shlex.quote(project)} && {launch}"
                actions.append({
                    "agent": agent, "kind": "terminal",
                    "command": [
                        "osascript", "-e",
                        "tell application \"Terminal\" to do script " + shlex.quote(inner),
                    ],
                })
        if headless:
            key = _key(agent, project)
            if _read_pid(home, key) is not None:
                actions.append({"agent": agent, "kind": "skip", "command": [], "note": "本项目收信守护已在运行"})
            else:
                poll = _poll_seconds(item)
                beat = _heartbeat_seconds(item)
                command = [
                    sys.executable, "-m", "infocenter", "crew", "watch", agent,
                    "--dir", project, "--url", url, "--home", str(home), "--poll", str(poll),
                ]
                if beat:
                    command += ["--heartbeat", str(beat)]
                actions.append({"agent": agent, "kind": "daemon", "command": command})
        if not (app or launch or headless):
            actions.append({"agent": agent, "kind": "skip", "command": [], "note": "未配置 launch/app/headless"})
    if use_tmux and any(action["kind"] == "tmux" for action in actions) and not _tmux_session_exists(session):
        actions.insert(0, {
            "agent": "-", "kind": "tmux-session",
            "command": ["tmux", "new-session", "-d", "-s", session, "-c", project],
        })
    return actions


def _poll_seconds(item: dict) -> int:
    try:
        poll = int(item.get("poll") or DEFAULT_POLL)
    except (TypeError, ValueError):
        poll = DEFAULT_POLL
    return max(5, min(poll, 3600))


def _heartbeat_seconds(item: dict) -> int:
    try:
        beat = int(item.get("heartbeat") or 0)
    except (TypeError, ValueError):
        beat = 0
    return max(0, min(beat, 86400))


def up(home, url: str, project: str, only: str = "", dry_run: bool = False) -> dict:
    actions = plan_up(home, url, project, only)
    lines = []
    for action in actions:
        rendered = shlex.join(action["command"]) if action["command"] else ""
        if action["kind"] == "skip":
            lines.append(f"[跳过] {action['agent']}：{action.get('note', '')}")
            continue
        if dry_run:
            lines.append(f"[{action['kind']}] {action['agent']}: {rendered}")
            continue
        if action["kind"] == "daemon":
            key = _key(action["agent"], project)
            pid = _start_daemon(home, key, project, action["command"])
            lines.append(f"[守护] {action['agent']} pid {pid}，日志 {log_file(home, key)}")
            continue
        result = subprocess.run(action["command"], capture_output=True, text=True)
        if result.returncode == 0:
            lines.append(f"[{action['kind']}] {action['agent']}：已启动")
        else:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            lines.append(f"[{action['kind']}] {action['agent']}：失败 {detail[-1] if detail else result.returncode}")
    if dry_run:
        lines.insert(0, f"dry-run，项目目录 {project}，将执行：")
    return {"project": project, "dry_run": dry_run, "actions": actions, "lines": lines}


def _start_daemon(home, key: str, project: str, command: list[str]) -> int:
    log = log_file(home, key)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    with open(log, "ab") as stream:
        proc = subprocess.Popen(
            command, cwd=project, stdout=stream, stderr=stream,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    agent = key.split("--", 1)[0]
    _pid_file(home, key).write_text(str(proc.pid))
    _meta_file(home, key).write_text(json.dumps({
        "agent": agent, "key": key, "dir": project, "pid": proc.pid, "started_at": utcnow(),
    }, ensure_ascii=False))
    return proc.pid


def kickoff(
    home,
    url: str,
    plan_path: str,
    project: str,
    subject: str | None = None,
    as_name: str | None = None,
) -> dict:
    coordinator = normalize_name(as_name) if as_name else config.coordinator(home)
    if not coordinator:
        raise MailError(
            "no_coordinator",
            "config.json 里没有 coordinator。先执行：ic agents add <名字> --role coordinator（多项目可用 --from 指定发信身份）",
        )
    plan = Path(plan_path).expanduser().read_text(encoding="utf-8")
    if not plan.strip():
        raise MailError("empty_plan", f"{plan_path} 是空的")
    others = [item["name"] for item in config.load(home)["agents"] if item["name"] != coordinator]
    if not others:
        raise MailError("nobody_to_kickoff", "除了 coordinator 没有别的 agent 可通知")
    body = plan.rstrip() + "\n\n" + CONVENTIONS.format(coordinator=coordinator)
    payload = request(url, "POST", "/v1/messages", coordinator, {
        "to": ",".join(others),
        "subject": subject or "【kickoff】项目启动：分工与约定",
        "body": body,
        "priority": "high",
        "workspace": project,
    })
    lines = [
        f"已以 {coordinator}（coordinator）身份发出 {payload['id']}",
        f"给: {', '.join(payload['to'])}",
        f"主题: {payload['subject']}（high，workspace {project}）",
    ]
    return {"message": payload, "coordinator": coordinator, "lines": lines}


def status(home, project: str | None = None) -> dict:
    entries = config.load(home)["agents"]
    sessions = _tmux_sessions()
    target = _resolve_dir(project)
    rows = []
    for path in sorted(Path(state_dir(home)).glob("*.pid")):
        key = path.stem
        meta = _read_meta(home, key)
        directory = meta.get("dir") or ""
        if target and _resolve_dir(directory) != target:
            continue
        pid = _read_pid(home, key)
        try:
            cursor = int(_cursor_file(home, key).read_text().strip())
        except (OSError, ValueError):
            cursor = None
        rows.append({
            "agent": key.split("--", 1)[0],
            "key": key,
            "dir": directory,
            "daemon_pid": pid,
            "daemon_alive": pid is not None,
            "cursor": cursor,
        })
    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        by_agent.setdefault(row["agent"], []).append(row)
    lines = ["tmux 会话: " + (", ".join(sorted(sessions)) if sessions else "不在")]
    for item in entries:
        agent = item["name"]
        parts = []
        if str(item.get("role") or "") == "coordinator":
            parts.append("coordinator")
        daemons = by_agent.get(agent, [])
        if daemons:
            parts += [
                f"守护 pid {d['daemon_pid']} → {Path(d['dir']).name or d['dir']}" if d["daemon_alive"]
                else f"守护（{Path(d['dir']).name}）已死"
                for d in daemons
            ]
        elif str(item.get("headless") or "").strip():
            parts.append("守护未运行")
        if daemons or not str(item.get("headless") or "").strip():
            ways = [key for key in ("launch", "app", "headless") if str(item.get(key) or "").strip()]
            if not ways and not daemons:
                parts.append("未配置启动方式")
        lines.append(f"  {agent}: " + ("；".join(parts) if parts else "空闲"))
    for agent, daemons in by_agent.items():
        if agent not in {item["name"] for item in entries}:
            for d in daemons:
                lines.append(f"  {agent}（不在名单）: 守护 pid {d['daemon_pid']} → {d['dir']}")
    return {"sessions": sorted(sessions), "agents": rows, "lines": lines}


def down(home, project: str | None = None) -> dict:
    target = _resolve_dir(project)
    lines = []
    killed = []
    for path in sorted(Path(state_dir(home)).glob("*.pid")):
        key = path.stem
        meta = _read_meta(home, key)
        directory = meta.get("dir") or ""
        if target and _resolve_dir(directory) != target:
            continue
        pid = _read_pid(home, key)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(key)
                lines.append(f"已终止守护 {key}（pid {pid}）")
            except OSError as exc:
                lines.append(f"终止 {key} 失败：{exc}")
        for suffix in (".pid", ".json", ".cursor"):
            try:
                path.with_suffix(suffix).unlink()
            except OSError:
                pass
    sessions = {_session_name(target)} if target else _tmux_sessions()
    for session in sorted(sessions):
        if not shutil.which("tmux"):
            break
        result = subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        if result.returncode == 0:
            lines.append(f"已关闭 tmux 会话 {session}")
    if not lines:
        lines.append("没有需要清理的守护或 tmux 会话")
    return {"killed": killed, "lines": lines}


def build_prompt(agent: str, project: str, fresh: list[dict], unread_total: int) -> str:
    listing = "\n".join(
        f"- [{item['id']}] 来自 {item['from']}《{item['subject']}》"
        + ("（high）" if item.get("priority") == "high" else "")
        + (f" workspace {item['workspace']}" if item.get("workspace") else "")
        for item in fresh
    )
    return (
        f"你是 {agent}，项目目录 {project}。信息中心有 {unread_total} 封属于本项目的未读留言，最新 {len(fresh)} 封：\n"
        f"{listing}\n"
        f"请处理：`ic --as {agent} inbox --full` 读信；处理后 `ic --as {agent} read <id>` 签收；"
        f"需要回应时 `ic --as {agent} reply <id> -b \"...\"`。无新信息不回帖，禁止 +1 附议。\n"
        f"只处理上面列出的、属于本项目的信；其他项目或无 workspace 的未读不要在本轮动手"
        f"（无 workspace 的信由交互会话处理）。"
    )


def build_heartbeat_prompt(agent: str, project: str, coordinator: bool = False) -> str:
    if coordinator:
        return (
            f"你是 {agent}（coordinator），项目目录 {project}。心跳检查（没有新信件）："
            f"1）维护 {project}/PLAN.md：吸收新需求、更新任务状态、勾掉完成项、拆解停滞项；"
            f"2）调配资源：用 `ic --as {agent} agents` 看各家的 activity 状态和未读数，对照成员 TODO-<名字>.md 进展；"
            f"发现谁停滞、跑偏或闲下来，就重排任务（更新 PLAN.md 并发信通知相关方）或发信催办；"
            f"3）处理收件箱里等你裁定的信：`ic --as {agent} inbox --full`。"
            f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
        )
    return (
        f"你是 {agent}，项目目录 {project}。心跳检查（没有新信件）："
        f"阅读 {project} 下的 PLAN.md 和你自己的 TODO-{agent}.md，"
        f"推进你名下（owner={agent}）的下一个未完成事项，并把进展写回 TODO-{agent}.md。"
        f"发现 PLAN.md 有空缺、依赖过期或需要调整，给 coordinator 发信说明，不要直接改 PLAN.md。"
        f"开始或切换任务时，用 `ic --as {agent} register --activity \"一句话状态\"` 自报进展，coordinator 靠它调配资源。"
        f"无 workspace 的未读信不会自动派给守护：查收件时若发现明确属于本项目的，顺手处理；否则留给交互会话。"
        f"需要别人输入或裁定时，用 `ic --as {agent} send/reply` 给相关人留言；"
        f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
    )


def watch(home, agent: str, project: str, url: str, poll: int = DEFAULT_POLL, heartbeat: int | None = None) -> int:
    """守护进程本体：长轮询 /v1/events（限定本项目 workspace），新信即时触发一次 headless 运行；
    没有信时每隔 heartbeat 秒触发一次 headless 规划运行（0=关闭，纯信件驱动）。
    状态键 = 身份 × 项目：同一身份可以在多个项目各跑一个守护，互不抢信。"""
    agent = normalize_name(agent)
    key = _key(agent, project)
    entry = config.entry(home, agent)
    template = str((entry or {}).get("headless") or "").strip()
    if not template:
        raise MailError("no_headless", f"{agent} 在 config.json 里没有 headless 命令")
    poll = max(5, min(int(poll or DEFAULT_POLL), 3600))
    if heartbeat is None:
        heartbeat = _heartbeat_seconds(entry or {})
    heartbeat = max(0, min(int(heartbeat or 0), 86400))
    wait = min(poll, 120)
    cursor_path = _cursor_file(home, key)
    try:
        cursor = int(cursor_path.read_text().strip())
    except (OSError, ValueError):
        cursor = 0
    print(
        f"[{utcnow()}] watch 启动 key={key} dir={project} url={url} poll={poll} heartbeat={heartbeat}",
        flush=True,
    )
    last_run = time.monotonic()
    while True:
        try:
            payload = request(
                url, "GET", "/v1/events", agent,
                query={"since": str(cursor), "wait": str(wait), "limit": "20", "workspace": project},
                timeout=wait + 15,
            )
        except (ApiError, SystemExit, OSError, ValueError, KeyError) as exc:
            print(f"[{utcnow()}] events 失败：{exc}，2 秒后重试", flush=True)
            time.sleep(2)
            continue
        cursor = max(cursor, int(payload.get("cursor") or 0))
        cursor_path.write_text(str(cursor))
        fresh = payload.get("new") or []
        if fresh:
            prompt = build_prompt(agent, project, fresh, int(payload.get("unread_total") or len(fresh)))
            print(f"[{utcnow()}] 本项目新信 {len(fresh)} 封，触发 headless", flush=True)
            _run_headless(template, prompt, project)
            last_run = time.monotonic()
        elif heartbeat and time.monotonic() - last_run >= heartbeat:
            print(f"[{utcnow()}] 无新信，心跳 {heartbeat}s 到期，触发规划运行", flush=True)
            is_coord = str((entry or {}).get("role") or "") == "coordinator"
            _run_headless(template, build_heartbeat_prompt(agent, project, is_coord), project)
            last_run = time.monotonic()


def _run_headless(template: str, prompt: str, project: str) -> None:
    if "{prompt}" in template:
        command = template.replace("{prompt}", shlex.quote(prompt))
        try:
            proc = subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, text=True, timeout=1800)
            tail = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            print(f"[{utcnow()}] headless 超时（30 分钟）", flush=True)
            return
    else:
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", template], cwd=project,
                input=prompt, capture_output=True, text=True, timeout=1800,
            )
            tail = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            print(f"[{utcnow()}] headless 超时（30 分钟）", flush=True)
            return
    print(f"[{utcnow()}] headless 退出码 {proc.returncode}，输出尾部：{tail.strip()[-500:]}", flush=True)
