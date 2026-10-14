"""ic crew：团队编排。

up       按 ~/.infocenter/config.json 逐 agent 拉起：
         launch → 每项目一个 tmux 会话（ic-crew-<hash>）每人一窗（无 tmux 退化 osascript 开 Terminal）
         app    → `open -a <app> <项目目录>`
         headless → 启动收信守护（watch）：状态键 = 身份 × 项目，同一身份可在多个项目各跑一个守护；
                    守护长轮询 /v1/events（带 workspace 范围），只处理属于本项目的信
kickoff  以 coordinator（或 --from 指定身份）向其他所有 agent 发 high 启动信（分工 + 合作约定），
         workspace 记项目目录
status   查看守护进程、tmux 会话、config 启动方式（--dir 只看某个项目）
down     杀守护进程、关 tmux 会话（--dir 只清某个项目，--only 只停点名的身份）
pause / resume / abort / close
         按身份暂停、恢复、中止或办结其办公室和案卷。
         暂停会打断该项目里正在跑的 headless；办公室处于暂停时，守护不再开新一轮。
watch    守护进程本体（由 up 派生，不面向用户）。启动时生成窗口号，退出时上报离场
"""

import calendar
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
from infocenter.store import MailError, _workspace_scope, new_window_id, normalize_name, utcnow
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


# 新 pid 文件带启动时刻；和进程启动时间差过这么多秒就当成 pid 已被别人复用。
_PID_START_SLACK = 15
OFFICE_REGISTER_ATTEMPTS = 3
_HEADLESS_RETRY = {"timeout", "fail", "start_failed", "interrupted"}
_HEADLESS_RETRY_BASE = 2
_HEADLESS_RETRY_MAX = 300


def _retry_delay(rounds: int) -> int:
    """N2：headless 失败重试的指数退避（2、4、8…），封顶 `_HEADLESS_RETRY_MAX` 秒。

    永久性失败时不再每轮固定 2s 空转刷屏，也避免反复写大量 [自动] 中断节点。
    """
    rounds = max(1, int(rounds))
    return min(_HEADLESS_RETRY_MAX, _HEADLESS_RETRY_BASE * (2 ** min(rounds - 1, 16)))


def _pid_alive(pid: int) -> bool:
    # C-2：pid<=1 不是可管理的目标进程——0 会打整个进程组、1 是 init。一律判死。
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _load_pid_record(home, key: str) -> dict | None:
    try:
        text = _pid_file(home, key).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    if text[:1].isdigit():
        try:
            return {"pid": int(text)}
        except ValueError:
            return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "pid" in data:
        return data
    return None


def _write_pid_record(home, key: str, pid: int, started_at: str) -> None:
    payload = {"pid": pid, "started_at": started_at, "marker": "ic-crew"}
    _pid_file(home, key).write_text(json.dumps(payload), encoding="utf-8")


def _reserve_pid_file(home, key: str) -> int:
    """C-9：原子独占创建 pid 文件，堵住并发 `crew up` 的 TOCTOU。返回写权限的 fd。

    - 文件不存在：O_EXCL 创建成功并返回 fd（此时内容由调用方写占位）。
    - 已存在且里面是活着的本仓库守护：报 already_running。
    - 已存在且是并发 up 的占位（pid==0）：也报 already_running。
    - 已存在但内容是陈旧/死 pid：清掉后重试一次。
    """
    path = _pid_file(home, key)
    for _ in range(2):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            record = _load_pid_record(home, key)
            pid = None
            if record:
                try:
                    pid = int(record["pid"])
                except (TypeError, ValueError):
                    pid = None
            if pid == 0:
                raise MailError("already_running", f"{key} 的守护正在启动（并发 up）")
            if pid is not None and pid > 1 and _read_pid(home, key) is not None:
                raise MailError("already_running", f"{key} 的守护已在运行（pid {pid}）")
            try:
                path.unlink()
            except OSError:
                pass
            continue
    raise MailError("already_running", f"{key} 的 pid 文件无法独占创建")


def _process_cmdline(pid: int) -> str | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    text = out.strip()
    return text or None


def _parse_etime(text: str) -> int | None:
    text = " ".join(text.split())
    if not text:
        return None
    days = 0
    if "-" in text:
        day_s, text = text.split("-", 1)
        if not day_s.isdigit():
            return None
        days = int(day_s)
    parts = text.split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    nums = [int(part) for part in parts]
    if len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _process_start_epoch(pid: int) -> float | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "etime="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    elapsed = _parse_etime(out)
    if elapsed is None:
        return None
    return time.time() - elapsed


def _parse_utc(value: str) -> float | None:
    try:
        moment = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return float(calendar.timegm(moment))


def _daemon_identity_ok(pid: int, started_at: str) -> bool:
    """pid 还活着不够：命令行必须是本仓库的 crew watch，且启动时刻对得上。"""
    cmdline = _process_cmdline(pid)
    if not cmdline or "infocenter" not in cmdline or "watch" not in cmdline:
        return False
    proc_start = _process_start_epoch(pid)
    recorded = _parse_utc(started_at)
    if proc_start is None or recorded is None:
        return False
    return abs(proc_start - recorded) <= _PID_START_SLACK


def _read_pid(home, key: str) -> int | None:
    record = _load_pid_record(home, key)
    if not record:
        return None
    try:
        pid = int(record["pid"])
    except (TypeError, ValueError):
        return None
    # C-2：pid<=1 视为脏记录，不认。
    if pid <= 1:
        return None
    if not _pid_alive(pid):
        return None
    started = record.get("started_at")
    if not started:
        return pid
    return pid if _daemon_identity_ok(pid, str(started)) else None


def _release_own_pid() -> None:
    home = _daemon_ctx.get("home")
    key = _daemon_ctx.get("key")
    if not home or not key:
        return
    record = _load_pid_record(home, key)
    if not record:
        return
    try:
        pid = int(record["pid"])
    except (TypeError, ValueError):
        return
    if pid != os.getpid():
        return
    try:
        _pid_file(home, key).unlink()
    except OSError:
        pass


def _read_meta(home, key: str) -> dict:
    try:
        return json.loads(_meta_file(home, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(home, key: str, meta: dict) -> None:
    path = _meta_file(home, key)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# watch 把当前守护的 home/key 放这里，_run_headless 据此记下正在跑的子进程。
# 调用签名保持不变，避免已有的 _run_headless 替身因多出来的参数报错。
_daemon_ctx: dict = {}


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
    # C-13：身份名可能含 `--`，从右边取分隔符前的部分才是完整名字。
    agent = key.rsplit("--", 1)[0]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    env["INFOCENTER_AGENT"] = agent
    # 父进程的办公室号不能传给新守护，否则多个项目共用一个窗口。
    env.pop("INFOCENTER_WINDOW", None)
    # C-9：先独占占位 pid 文件，再 Popen，避免并发 up 各起一个守护。
    fd = _reserve_pid_file(home, key)
    with os.fdopen(fd, "w", encoding="utf-8") as holder:
        holder.write(json.dumps({"pid": 0, "started_at": utcnow(), "marker": "ic-crew"}))
    try:
        with open(log, "ab") as stream:
            proc = subprocess.Popen(
                command, cwd=project, stdout=stream, stderr=stream,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env,
            )
    except BaseException:
        try:
            _pid_file(home, key).unlink()
        except OSError:
            pass
        raise
    started_at = utcnow()
    _write_pid_record(home, key, proc.pid, started_at)
    _write_meta(home, key, {
        "agent": agent, "key": key, "dir": project, "pid": proc.pid, "started_at": started_at,
    })
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


def _office_mark(row: dict) -> str:
    if row.get("office_registered") is False:
        return " \033[31m办公室未登记\033[0m"
    return ""


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
        office = meta.get("office_registered") if "office_registered" in meta else None
        rows.append({
            "agent": key.rsplit("--", 1)[0],
            "key": key,
            "dir": directory,
            "daemon_pid": pid,
            "daemon_alive": pid is not None,
            "cursor": cursor,
            "office_registered": office,
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
                (
                    f"守护 pid {d['daemon_pid']} → {Path(d['dir']).name or d['dir']}" if d["daemon_alive"]
                    else f"守护（{Path(d['dir']).name}）已死"
                )
                + _office_mark(d)
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


def running_headless(home, project: str | None = None) -> list[dict]:
    """列出正在跑的 headless：扫守护 meta 里非空的 headless_pid。"""
    out = []
    target = None
    if project:
        try:
            target = str(Path(project).expanduser().resolve())
        except OSError:
            target = project
    for path in sorted(state_dir(home).glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = meta.get("headless_pid")
        if not pid:
            continue
        d = meta.get("dir") or ""
        if target is not None:
            try:
                if str(Path(d).expanduser().resolve()) != target:
                    continue
            except OSError:
                if d != project:
                    continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        prompt = ""
        pf = state_dir(home) / f"{path.stem}.prompt"
        try:
            if pf.exists():
                prompt = pf.read_text(encoding="utf-8")
        except OSError:
            prompt = ""
        out.append({
            "agent": meta.get("agent") or "",
            "key": meta.get("key") or path.stem,
            "dir": d,
            "pid": pid_int,
            "alive": _pid_alive(pid_int),
            "prompt": prompt,
        })
    return out


def _record_run(home, key: str, rec: dict) -> None:
    path = state_dir(home) / f"{key}.runs.jsonl"
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 60:
            path.write_text("\n".join(lines[-60:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def headless_runs(home, project: str | None = None, limit: int = 30) -> list[dict]:
    """最近完成的 headless 运行（含提示词上下文与输出尾部）。"""
    target = _resolve_dir(project) if project else None
    out: list[dict] = []
    for path in sorted(state_dir(home).glob("*.runs.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            d = rec.get("dir") or ""
            if target and (_resolve_dir(d) if d else None) != target:
                continue
            out.append(rec)
    out.sort(key=lambda r: str(r.get("end") or ""), reverse=True)
    return out[:limit]


def down(home, project: str | None = None, only: str = "") -> dict:
    target = _resolve_dir(project)
    wanted = [item["name"] for item in _select(home, only)] if str(only or "").strip() else None
    lines = []
    killed = []
    projects: set[str] = {target} if target else set()
    for path in sorted(Path(state_dir(home)).glob("*.pid")):
        key = path.stem
        meta = _read_meta(home, key)
        directory = meta.get("dir") or ""
        resolved = _resolve_dir(directory) if directory else None
        agent_name = str(meta.get("agent") or key.rsplit("--", 1)[0])
        if target and resolved != target:
            continue
        if wanted is not None and agent_name not in wanted:
            continue
        if resolved:
            projects.add(resolved)
        pid = _read_pid(home, key)
        # C-2：pid>1 才可能是本仓库守护，绝不对 0/1 发信号。
        if pid is not None and pid > 1:
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
    if wanted is None:
        sessions = {_session_name(target)} if target else _tmux_sessions()
        for session in sorted(sessions):
            if not shutil.which("tmux"):
                break
            result = subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
            if result.returncode == 0:
                lines.append(f"已关闭 tmux 会话 {session}")
    else:
        _stop_tmux_windows(projects, wanted, lines)
    if not lines:
        lines.append("没有需要清理的守护或 tmux 会话")
    return {"killed": killed, "lines": lines}


def _stop_tmux_windows(projects: set[str], agents: list[str], lines: list[str]) -> None:
    if not shutil.which("tmux") or not projects:
        return
    for project in sorted(projects):
        if not project:
            continue
        session = _session_name(project)
        existing = _tmux_windows(session)
        for agent in agents:
            window = _window_name(project, agent)
            if window not in existing:
                continue
            result = subprocess.run(
                ["tmux", "kill-window", "-t", f"{session}:{window}"],
                capture_output=True,
            )
            if result.returncode == 0:
                lines.append(f"已关闭 tmux 窗口 {session}:{window}")


def control(home, url: str, action: str, agent: str, project: str | None = None, note: str | None = None) -> dict:
    """按身份暂停、恢复、中止或办结。暂停/恢复走办公室；中止/办结只封它名下未封卷的案卷。"""
    if action not in {"pause", "resume", "abort", "close"}:
        raise MailError("invalid_action", "只能是 pause、resume、abort 或 close")
    # C-12：空身份必须报错，不能 _select("") 落到名单第一个 agent。
    if not isinstance(agent, str) or not agent.strip():
        raise MailError("unknown_agent", "需要一个身份名（例如 ic crew pause <名字>）")
    name = _select(home, agent)[0]["name"]
    target = _resolve_dir(project) if project else None
    lines: list[str] = []
    # M4：暂停/中止/办结都要先打断正在跑的 headless（对齐 pause），
    # 且先打断再查办公室。只读接口 404 或超时时，正在跑的 headless 也必须先停。
    if action in {"pause", "abort", "close"}:
        lines.extend(_interrupt_headless(home, name, target))
    windows = _api(url, "GET", "/v1/windows", name, query={"agent": name}).get("windows") or []
    cases = _api(url, "GET", "/v1/cases", name, query={"owner": name, "limit": "100"}).get("cases") or []
    windows = [item for item in windows if _in_project(item, target)]
    cases = [item for item in cases if _in_project(item, target) and not item.get("sealed")]
    body = {"note": note} if note else {}
    handled_windows: set[str] = set()
    if action in {"pause", "resume"}:
        for item in windows:
            state = item.get("state")
            if action == "pause" and state in {"left", "paused"}:
                continue
            if action == "resume" and state != "paused":
                continue
            wid = item.get("window_id") or item.get("id")
            if not wid:
                continue
            _api(url, "POST", f"/v1/windows/{wid}/{action}", name, body or {})
            handled_windows.add(wid)
            lines.append(f"已{'暂停' if action == 'pause' else '恢复'}办公室 {wid}")
    verb = {"pause": "暂停", "resume": "恢复", "abort": "中止", "close": "办结"}[action]
    for item in cases:
        owner_window = item.get("owner_window")
        if owner_window in handled_windows:
            continue
        status = item.get("status")
        if action == "pause" and status == "paused":
            continue
        if action == "resume" and status != "paused":
            continue
        case_body = dict(body)
        if action == "pause":
            case_body["interrupted"] = True
        _api(url, "POST", f"/v1/cases/{item['id']}/{action}", name, case_body)
        lines.append(f"已{verb}案卷 {item['id']}")
    if not lines:
        lines.append(f"没有需要{verb}的办公室或案卷")
    return {"agent": name, "action": action, "lines": lines}


def _signal_group(pid: int, sig: int) -> bool:
    """向 headless 所在进程组发信号。pid 必须是该组组长，不能是守护自己。"""
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return False
    try:
        os.killpg(pid, sig)
        return True
    except OSError:
        # C-10：进程组不存在（ProcessLookupError）或不是组长时，回落到直接给该 pid 发信号。
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False


def _interrupt_headless(home, agent: str, project: str | None) -> list[str]:
    """暂停时打断该身份、该项目里正在跑的 headless。不动守护进程本身。"""
    lines: list[str] = []
    target = _resolve_dir(project) if project else None
    for path in sorted(state_dir(home).glob("*.json")):
        meta = _read_meta(home, path.stem)
        if str(meta.get("agent") or "") != agent:
            continue
        directory = str(meta.get("dir") or "")
        resolved = _resolve_dir(directory) if directory else None
        if target and resolved != target:
            continue
        try:
            pid = int(meta.get("headless_pid"))
        except (TypeError, ValueError):
            continue
        try:
            daemon_pid = int(meta.get("pid"))
        except (TypeError, ValueError):
            daemon_pid = None
        if daemon_pid is not None and pid == daemon_pid:
            continue
        if not _pid_alive(pid):
            continue
        if not _signal_group(pid, signal.SIGTERM):
            lines.append(f"打断 {agent} 的 headless（pid {pid}）失败")
            continue
        # C-11：SIGTERM 后短轮询确认真的退出，仍不死就升级 SIGKILL。
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            _signal_group(pid, signal.SIGKILL)
            lines.append(f"已打断 {agent} 正在跑的 headless（pid {pid}；升级 SIGKILL）")
        else:
            lines.append(f"已打断 {agent} 正在跑的 headless（pid {pid}）")
    return lines


def _note_headless_pid(pid: int | None) -> None:
    home = _daemon_ctx.get("home")
    key = _daemon_ctx.get("key")
    if not home or not key:
        return
    meta = _read_meta(home, key)
    if not meta:
        meta = {
            "agent": _daemon_ctx.get("agent") or "",
            "key": key,
            "dir": _daemon_ctx.get("dir") or "",
            "pid": os.getpid(),
            "started_at": utcnow(),
        }
    if pid is None:
        meta.pop("headless_pid", None)
    else:
        meta["headless_pid"] = pid
    _write_meta(home, key, meta)


def _window_paused(url: str, agent: str, window_id: str) -> bool:
    payload = _office_request(
        url, "GET", "/v1/windows", agent, query={"agent": agent}, timeout=5,
    )
    for item in payload.get("windows") or []:
        wid = item.get("window_id") or item.get("id")
        if wid == window_id:
            return item.get("state") == "paused"
    return False


def _in_project(item: dict, project: str | None) -> bool:
    if not project:
        return True
    return _workspace_scope(item.get("workspace"), project)


def _api(url: str, method: str, path: str, agent: str, body=None, query=None):
    try:
        return request(url, method, path, agent, body, query)
    except ApiError as exc:
        raise MailError(exc.code, str(exc), exc.status) from exc


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
        f"本轮动作固定为：读案卷 → 干活 → 写回一行节点。\n"
        f"先 `ic --as {agent} case <thread>` 读这封信所在案卷（没有案卷就按信本身办）；"
        f"再处理来信：`ic --as {agent} inbox --full` 读信；处理后 `ic --as {agent} read <id>` 签收；"
        f"需要回应时 `ic --as {agent} reply <id> -b \"...\"`。无新信息不回帖，禁止 +1 附议。\n"
        f"办完写回一行节点：`ic --as {agent} case <thread> --note \"结论\"`。\n"
        f"只处理上面列出的、属于本项目的信；其他项目或无 workspace 的未读不要在本轮动手"
        f"（无 workspace 的信由交互会话处理）。"
    )


def build_heartbeat_prompt(
    agent: str,
    project: str,
    coordinator: bool = False,
    custom_prompt: str | None = None,
    coordinator_name: str | None = None,
) -> str:
    if custom_prompt and str(custom_prompt).strip():
        return (
            str(custom_prompt)
            .strip()
            .replace("{agent}", agent)
            .replace("{project}", project)
        )
    if coordinator:
        return (
            f"你是 {agent}（coordinator），项目目录 {project}。心跳检查（没有新信件）："
            f"1）维护 {project}/PLAN.md：吸收新需求、更新任务状态、勾掉完成项、拆解停滞项；"
            f"2）调配资源：用 `ic --as {agent} agents` 看各家的 activity 状态和未读数，对照成员 TODO-<名字>.md 进展；"
            f"发现谁停滞、跑偏或闲下来，就重排任务（更新 PLAN.md 并发信通知相关方）或发信催办；"
            f"3）处理收件箱里等你裁定的信：`ic --as {agent} inbox --full`。"
            f"接着办已有的事时，先 `ic --as {agent} cases` 读案卷，干完用 "
            f"`ic --as {agent} case <thread> --note \"结论\"` 写回一行节点。"
            f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
        )
    coord_target = (
        f"主协调 agent `{coordinator_name.strip()}`（`ic agents` 里 role=coordinator 的那位）"
        if coordinator_name and str(coordinator_name).strip()
        else "主协调 agent（`ic agents` 里 role=coordinator 的那位）"
    )
    return (
        f"你是 {agent}，项目目录 {project}。心跳检查（没有新信件）："
        f"阅读 {project} 下的 PLAN.md 和你自己的 TODO-{agent}.md，"
        f"推进你名下（owner={agent}）的下一个未完成事项，并把进展写回 TODO-{agent}.md。"
        f"发现 PLAN.md 有空缺、依赖过期或需要调整，给{coord_target}发信说明，不要直接改 PLAN.md。"
        f"开始或切换任务时，用 `ic --as {agent} register --activity \"一句话状态\"` 自报进展，主协调 agent 靠它调配资源。"
        f"无 workspace 的未读信不会自动派给守护：查收件时若发现明确属于本项目的，顺手处理；否则留给交互会话。"
        f"需要别人输入或裁定时，用 `ic --as {agent} send/reply` 给相关人留言。"
        f"接着办已有的事时，先 `ic --as {agent} cases` 读案卷，干完用 "
        f"`ic --as {agent} case <thread> --note \"结论\"` 写回一行节点。"
        f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
    )


def watch(
    home,
    agent: str,
    project: str,
    url: str,
    poll: int = DEFAULT_POLL,
    heartbeat: int | None = None,
    window: str | None = None,
) -> int:
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
    mode = str((entry or {}).get("heartbeat_mode") or "idle").strip().lower()
    custom_prompt = str((entry or {}).get("heartbeat_prompt") or "").strip() or None
    wait = min(poll, 120)
    cursor_path = _cursor_file(home, key)
    try:
        cursor = int(cursor_path.read_text().strip())
    except (OSError, ValueError):
        cursor = 0
    window_id, previous_window = _adopt_window(window)
    print(
        f"[{utcnow()}] watch 启动 key={key} dir={project} url={url} poll={poll} "
        f"heartbeat={heartbeat} window={window_id}",
        flush=True,
    )
    _daemon_ctx.clear()
    _daemon_ctx.update({"home": home, "key": key, "agent": agent, "dir": project})

    stopping = False
    registered = False
    # N2：headless 连续失败的重试次数与「这批信的中断节点是否已补」标志。
    retry_rounds = 0
    interrupted_noted = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        raise SystemExit(0)

    def _commit(new_cursor: int) -> None:
        nonlocal cursor
        cursor = new_cursor
        cursor_path.write_text(str(cursor))

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        for attempt in range(1, OFFICE_REGISTER_ATTEMPTS + 1):
            registered = _register_office(url, agent, window_id, project)
            _mark_office(registered)
            if registered:
                break
            print(
                f"[{utcnow()}] 办公室登记第 {attempt}/{OFFICE_REGISTER_ATTEMPTS} 次失败",
                flush=True,
            )
            if attempt < OFFICE_REGISTER_ATTEMPTS:
                time.sleep(1)
        if not registered:
            print(f"[{utcnow()}] 办公室登记连续失败，守护退出，不进入收信循环", flush=True)
            return 1
        last_run = time.monotonic()
        while True:
            try:
                try:
                    payload = request(
                        url, "GET", "/v1/events", agent,
                        query={"since": str(cursor), "wait": str(wait), "limit": "20", "workspace": project},
                        timeout=wait + 15,
                    )
                except SystemExit:
                    if stopping:
                        break
                    print(f"[{utcnow()}] events 失败：连不上信息中心，2 秒后重试", flush=True)
                    time.sleep(2)
                    continue
                except (ApiError, OSError, ValueError, KeyError) as exc:
                    print(f"[{utcnow()}] events 失败：{exc}，2 秒后重试", flush=True)
                    time.sleep(2)
                    continue
                fresh = payload.get("new") or []
                try:
                    office_paused = _window_paused(url, agent, window_id)
                except SystemExit:
                    if stopping:
                        break
                    office_paused = False
                except (ApiError, OSError, ValueError, KeyError, MailError):
                    office_paused = False
                if office_paused:
                    print(f"[{utcnow()}] 办公室 {window_id} 已暂停，跳过 headless", flush=True)
                    if fresh:
                        time.sleep(2)
                    continue
                new_cursor = max(cursor, int(payload.get("cursor") or 0))
                if fresh:
                    prompt = build_prompt(agent, project, fresh, int(payload.get("unread_total") or len(fresh)))
                    print(f"[{utcnow()}] 本项目新信 {len(fresh)} 封，触发 headless", flush=True)
                    outcome = _run_headless(template, prompt, project, agent=agent)
                    if outcome in _HEADLESS_RETRY:
                        retry_rounds += 1
                        # N2：同一批信只补一次 [自动] 中断节点，避免永久失败时刷屏写一堆。
                        if not interrupted_noted:
                            _note_interrupted(url, agent, window_id, fresh)
                            interrupted_noted = True
                        delay = _retry_delay(retry_rounds)
                        print(
                            f"[{utcnow()}] headless {outcome}，游标留在 {cursor}，"
                            f"第 {retry_rounds} 次重试，{delay}s 后",
                            flush=True,
                        )
                        time.sleep(delay)
                    else:
                        retry_rounds = 0
                        interrupted_noted = False
                        _commit(new_cursor)
                        if mode != "always":
                            last_run = time.monotonic()
                elif heartbeat and time.monotonic() - last_run >= heartbeat:
                    print(f"[{utcnow()}] 无新信，心跳 {heartbeat}s 到期，触发规划运行", flush=True)
                    is_coord = str((entry or {}).get("role") or "") == "coordinator"
                    coord_name = config.coordinator(home)
                    _run_headless(
                        template,
                        build_heartbeat_prompt(agent, project, is_coord, custom_prompt, coordinator_name=coord_name),
                        project,
                        agent=agent,
                    )
                    _commit(new_cursor)
                    last_run = time.monotonic()
                elif new_cursor != cursor:
                    _commit(new_cursor)
            except StopIteration:
                raise
            except SystemExit:
                if stopping:
                    break
                print(f"[{utcnow()}] watch 收到退出信号，2 秒后重试", flush=True)
                time.sleep(2)
                continue
            except Exception as exc:
                print(f"[{utcnow()}] watch 本轮异常，记日志后继续：{exc}", flush=True)
                time.sleep(2)
                continue
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        if registered:
            _leave_office(url, agent, window_id)
        if stopping:
            _release_own_pid()
        _daemon_ctx.clear()
        if previous_window is None:
            os.environ.pop("INFOCENTER_WINDOW", None)
        else:
            os.environ["INFOCENTER_WINDOW"] = previous_window
    return 0


_PROMPT_MARKER = "{prompt}"


def _inside_quotes(text: str, pos: int) -> bool:
    """pos 处的字符是否落在 shell 单/双引号字符串内部（忽略单引号内的转义）。"""
    quote = ""
    i = 0
    while i < pos:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = ""
        else:
            if ch == "\\":
                i += 1
            elif ch in {"'", '"'}:
                quote = ch
        i += 1
    return quote != ""


def _substitute_prompt(template: str, prompt: str) -> str:
    """C-15：安全替换 `{prompt}`。

    `shlex.quote` 只在占位符不被引号包裹时有效。于是：
    - 裸 `{prompt}`：直接换成 shlex.quote(prompt)。
    - 整个 `'{prompt}'` / `"{prompt}"`：去掉外层引号后换成 shlex.quote(prompt)。
    - 占位符嵌在引号内且不独占（如 `-p "{prompt} extra"`）：引号会绕过转义导致命令注入，
      直接拒绝并提示改法。
    """
    quoted = shlex.quote(prompt)
    out: list[str] = []
    i = 0
    while True:
        j = template.find(_PROMPT_MARKER, i)
        if j == -1:
            out.append(template[i:])
            break
        before = template[j - 1] if j > 0 else ""
        after_index = j + len(_PROMPT_MARKER)
        after = template[after_index] if after_index < len(template) else ""
        if before and before == after and before in {"'", '"'}:
            out.append(template[i : j - 1])
            out.append(quoted)
            i = after_index + 1
            continue
        if _inside_quotes(template, j):
            raise MailError(
                "unsafe_template",
                "headless 模板把 {prompt} 嵌在引号里（且不是整段独占），会绕过转义导致命令注入；"
                "请改成裸 {prompt}（不加引号），或让引号只包住 {prompt} 本身。",
            )
        out.append(template[i:j])
        out.append(quoted)
        i = after_index
    return "".join(out)


def _run_headless(template: str, prompt: str, project: str, agent: str | None = None) -> str:
    """跑一轮 headless。返回 ok / timeout / interrupted / fail / start_failed。"""
    env = os.environ.copy()
    if agent:
        env["INFOCENTER_AGENT"] = agent
    if _PROMPT_MARKER in template:
        command = _substitute_prompt(template, prompt)
        stdin_text = None
    else:
        command = template
        stdin_text = prompt
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=project,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            # 独立进程组：pause 只打断这一轮，不会把收信守护一起杀掉。
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[{utcnow()}] headless 启动失败：{exc}", flush=True)
        return "start_failed"
    _note_headless_pid(proc.pid)
    ctx = _daemon_ctx
    h_home = ctx.get("home")
    h_key = ctx.get("key")
    started_at = utcnow()
    if h_home and h_key:
        try:
            (state_dir(h_home) / f"{h_key}.prompt").write_text(prompt, encoding="utf-8")
        except OSError:
            pass
    run_tail = ""
    outcome = "fail"
    try:
        try:
            if stdin_text is None:
                stdout, stderr = proc.communicate(timeout=1800)
            else:
                stdout, stderr = proc.communicate(stdin_text, timeout=1800)
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            _signal_group(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        except UnicodeDecodeError as exc:
            outcome = "fail"
            print(f"[{utcnow()}] headless 输出无法按 UTF-8 解码：{exc}", flush=True)
            if proc.poll() is None:
                _signal_group(proc.pid, signal.SIGKILL)
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.communicate()
            stdout, stderr = "", ""
        except BaseException:
            if proc.poll() is None:
                _signal_group(proc.pid, signal.SIGTERM)
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    _signal_group(proc.pid, signal.SIGKILL)
                    proc.communicate()
            raise
        else:
            code = proc.returncode
            tail = ((stdout or "") + (stderr or "")).strip()[-500:]
            run_tail = ((stdout or "") + (stderr or "")).strip()[-2000:]
            if code in (-signal.SIGTERM, 128 + signal.SIGTERM):
                print(f"[{utcnow()}] headless 被暂停打断（退出码 {code}），输出尾部：{tail}", flush=True)
                outcome = "interrupted"
            elif code == 0:
                print(f"[{utcnow()}] headless 退出码 {code}，输出尾部：{tail}", flush=True)
                outcome = "ok"
            else:
                print(f"[{utcnow()}] headless 退出码 {code}，输出尾部：{tail}", flush=True)
                outcome = "fail"
        if outcome == "timeout":
            print(f"[{utcnow()}] headless 超时（30 分钟）", flush=True)
    finally:
        _note_headless_pid(None)
        if h_home and h_key:
            _record_run(
                h_home,
                h_key,
                {
                    "agent": agent or ctx.get("agent") or "",
                    "key": h_key,
                    "dir": project,
                    "pid": proc.pid,
                    "start": started_at,
                    "end": utcnow(),
                    "outcome": outcome,
                    "prompt": prompt,
                    "tail": run_tail,
                },
            )
            try:
                (state_dir(h_home) / f"{h_key}.prompt").unlink(missing_ok=True)
            except OSError:
                pass
    return outcome


def _adopt_window(explicit: str | None = None) -> tuple[str, str | None]:
    """只认显式窗口号。环境里残留的 INFOCENTER_WINDOW 不复用，避免多守护串号。"""
    previous = os.environ.get("INFOCENTER_WINDOW")
    chosen = (explicit or "").strip()
    if not chosen:
        chosen = new_window_id()
    os.environ["INFOCENTER_WINDOW"] = chosen
    return chosen, previous


def _office_request(*args, **kwargs):
    """不走 crew.request，避免测试替身把登记/离场当成一轮 events。"""
    from infocenter import client

    return client.request(*args, **kwargs)


def _mark_office(registered: bool) -> None:
    home = _daemon_ctx.get("home")
    key = _daemon_ctx.get("key")
    if not home or not key:
        return
    meta = _read_meta(home, key)
    if not meta:
        meta = {
            "agent": _daemon_ctx.get("agent") or "",
            "key": key,
            "dir": _daemon_ctx.get("dir") or "",
            "pid": os.getpid(),
            "started_at": utcnow(),
        }
    meta["office_registered"] = bool(registered)
    _write_meta(home, key, meta)


def _register_office(url: str, agent: str, window_id: str, project: str) -> bool:
    try:
        _office_request(
            url,
            "POST",
            "/v1/windows",
            agent,
            {
                "window_id": window_id,
                "workspace": project,
                "activity": "收信守护",
                "state": "active",
                "pid": os.getpid(),
            },
            timeout=10,
        )
    except SystemExit as exc:
        # 0 是守护自己的 SIGTERM 退出；连不上服务时 client 抛的是带文案的 SystemExit。
        if exc.code in (0, None):
            raise
        print(f"[{utcnow()}] 登记办公室 {window_id} 失败：{exc}", flush=True)
        return False
    except (ApiError, OSError, ValueError, MailError) as exc:
        print(f"[{utcnow()}] 登记办公室 {window_id} 失败：{exc}", flush=True)
        return False
    return True


def _note_interrupted(url: str, agent: str, window_id: str, fresh: list[dict]) -> None:
    """headless 超时或失败时补一行 [自动] 中断，避免这批触发信静默消失。"""
    seen: set[str] = set()
    for item in fresh:
        thread_id = str(item.get("thread_id") or "").strip()
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        body = {"note": "中断", "action": "note", "auto": True}
        try:
            _office_request(
                url, "POST", f"/v1/cases/{thread_id}/nodes", agent, body,
                timeout=10, window=window_id,
            )
            continue
        except SystemExit as exc:
            if exc.code in (0, None):
                raise
            print(f"[{utcnow()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
            continue
        except ApiError as exc:
            if exc.status != 404:
                print(f"[{utcnow()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
                continue
        except (OSError, ValueError, MailError) as exc:
            print(f"[{utcnow()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
            continue
        try:
            _office_request(
                url, "POST", "/v1/cases", agent,
                {
                    "title": item.get("subject") or "中断",
                    "thread_id": thread_id,
                    "workspace": item.get("workspace"),
                    "owner_window": window_id,
                    "note": "立案",
                },
                timeout=10,
                window=window_id,
            )
            _office_request(
                url, "POST", f"/v1/cases/{thread_id}/nodes", agent, body,
                timeout=10, window=window_id,
            )
        except SystemExit as exc:
            if exc.code in (0, None):
                raise
            print(f"[{utcnow()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
        except (ApiError, OSError, ValueError, MailError) as exc:
            print(f"[{utcnow()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)


def _leave_office(url: str, agent: str, window_id: str) -> None:
    try:
        _office_request(
            url,
            "POST",
            f"/v1/windows/{window_id}/leave",
            agent,
            {"note": "办公室离场"},
            timeout=5,
        )
        print(f"[{utcnow()}] 办公室 {window_id} 已离场", flush=True)
    except (ApiError, SystemExit, OSError, ValueError, MailError):
        print(f"[{utcnow()}] 办公室 {window_id} 离场上报失败", flush=True)
