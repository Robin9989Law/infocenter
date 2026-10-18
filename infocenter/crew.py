"""ic crew：团队编排。

up       按 ~/.infocenter/config.json 逐 agent 拉起：
         launch → 每项目一个 tmux 会话（ic-crew-<hash>）每人一窗（无 tmux 退化 osascript 开 Terminal）
         app    → `open -a <app> <项目目录>`
         headless → 启动收信守护（watch）：状态键 = 身份 × 项目，同一身份可在多个项目各跑一个守护；
                    守护长轮询 /v1/events（带 workspace 范围），只处理属于本项目的信
kickoff  以 manager（或 --from 指定身份）向其他所有 agent 发 high 启动信（分工 + 合作约定），
         workspace 记项目目录
status   查看守护进程、tmux 会话、config 启动方式（--dir 只看某个项目）
down     杀守护进程、关 tmux 会话（--dir 只清某个项目，--only 只停点名的身份）
pause / resume / abort / close
         按身份暂停、恢复、中止或办结其办公室和案卷。
         暂停会打断该项目里正在跑的 headless；办公室处于暂停时，守护不再开新一轮。
watch    守护进程本体（由 up 派生，不面向用户）。启动时生成窗口号，退出时上报离场
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
from infocenter.client import NO_PID_ENV, ApiError, request, request_mail
from infocenter.store import (
    MailError,
    _parse_utc as parse_utc,   # 时间格式的唯一真相；本文件 `_started_at_epoch` 只做换算
    _workspace_scope,
    atomic_replace,
    beijing_stamp,
    new_window_id,
    normalize_name,
    pid_alive,
    utcnow,
)
from infocenter.paths import ROOT

SESSION_PREFIX = "ic-crew"
DEFAULT_POLL = 300
# 每家的运行记录只留最近这么多条（日志不能无限长）。**代价**：更早的触发数会被截掉，
# 所以按窗口数触发数时只能当**下限**用（见 run_stats 的 truncated）。
RUNS_KEEP = 60

CONVENTIONS = """---
合作约定（kickoff 自动附加）：
1. 本信由**全局 manager**（{coordinator}）发出。**项目里的 coordinator 由 manager 指定**（旧名 human 仍被接受），它在该项目里的分工裁定优先执行；有异议只回一条针对冲突点的信，不要并行改文件。
2. 单一写者原则：每个文件/文档只有一个 owner agent 可写；其他人只以留言发 diff 建议，由 owner 合入。
3. 规划纪律：PLAN.md（任务分解）由本项目的 coordinator 独家产出和维护，每项任务带 owner、交付物、验收标准、依赖；各成员只把进展写进自己的 TODO-<名字>.md；要调整计划就给**本项目的 coordinator**发信（`ic projects` 查；查不到回落 manager＝用户本人），不要直接改 PLAN.md。
4. 状态自报：开始/切换/完成任务时更新自己的 activity（MCP 调 ic_activity，CLI 用 ic register --activity "一句话状态"）。本项目的 coordinator 想知道各家在做什么，直接看 `ic agents`，**不要发问询信**；要干预、要调配才发信。
5. 礼仪：无新信息不回帖，禁止 +1 附议；同一分歧最多两轮，之后交**本项目的 coordinator**裁定（`ic projects` 查；查不到回落 manager＝用户本人）。
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
_HEADLESS_RETRY = {"timeout", "fail", "start_failed", "interrupted", "quota"}
# 单次 headless 的默认墙钟上限（秒）；可在身份配置里用 "headless_timeout" 覆盖。
HEADLESS_TIMEOUT = 1800
_HEADLESS_RETRY_BASE = 2
_HEADLESS_RETRY_MAX = 300

# —— 空转治理（idle guard）——
# 背景：守护每 heartbeat 秒无条件跑一整个 headless 会话；模型额度打爆、凭据失效后
# 仍照原间隔硬撞，空烧额度且日志刷屏。这里把「模型已不可用」与「本轮确实无事」分开处理：
#   1) 永久性失败（额度/鉴权）指纹命中 ⇒ 长冷却，冷却期内不再触发 headless（新信也不跑，游标不前进）；
#   2) 普通连续失败达阈值 ⇒ 短冷却；
#   3) 心跳「空转」（进展信号文件未变）⇒ 心跳间隔指数拉长，封顶 idle_backoff_max。
# 只留短语。裸词 insufficient / quota / unauthorized / authentication / billing
# 会把「容量不够」这类普通失败判成 6 小时长冷却。insufficient_quota 必须整段出现。
PERMANENT_FAILURE_PATTERNS = (
    "usage limit",
    "spend limit",
    "monthly cycle",
    "insufficient_quota",
    "out of credit",
    "no credit",
    "credit balance",
    "payment required",
    "invalid credential",
    "invalid api key",
)
FAILURE_THRESHOLD = 3
FAILURE_COOLDOWN = 3600
PERMANENT_COOLDOWN = 21600
IDLE_BACKOFF_MAX = 7200


def _retry_delay(rounds: int) -> int:
    """N2：headless 失败重试的指数退避（2、4、8…），封顶 `_HEADLESS_RETRY_MAX` 秒。

    永久性失败时不再每轮固定 2s 空转刷屏，也避免反复写大量 [自动] 中断节点。
    """
    rounds = max(1, int(rounds))
    return min(_HEADLESS_RETRY_MAX, _HEADLESS_RETRY_BASE * (2 ** min(rounds - 1, 16)))


def _classify_failure(text: str) -> str:
    """按 headless 输出尾部把非零退出归类。

    命中额度/鉴权类指纹 ⇒ "quota"（模型已不可用，进长冷却，别拿 heartbeat 硬撞）；
    否则 ⇒ "fail"（普通失败，走指数退避 + 连续失败冷却）。
    """
    low = (text or "").lower()
    for pat in PERMANENT_FAILURE_PATTERNS:
        if pat in low:
            return "quota"
    return "fail"


def _int_or(item: dict, key: str, default: int) -> int:
    """取整数配置：缺失/空/非法回落默认；显式 0 视为有效值。"""
    raw = item.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _failure_threshold(item: dict) -> int:
    return max(1, min(_int_or(item, "failure_threshold", FAILURE_THRESHOLD), 1000))


def _cooldown_seconds(item: dict, kind: str) -> int:
    """冷却时长（秒）：永久性失败用 permanent_cooldown，普通失败用 failure_cooldown。"""
    permanent = kind == "quota"
    key = "permanent_cooldown" if permanent else "failure_cooldown"
    default = PERMANENT_COOLDOWN if permanent else FAILURE_COOLDOWN
    return max(0, min(_int_or(item, key, default), 30 * 86400))


def _cooldown_failure(outcome: str) -> bool:
    """这次 headless 的结局算不算「失败」到要点名冷却。

    **「超时」不算**：超时只说明这一轮跑得久（模型慢、活重），不是模型坏了——
    把超时按失败处理会变成「配置的超时压得比默认短 ⇒ 慢模型必被判死 ⇒ 冷却一小时 ⇒
    必然空转」的死循环。超时走退避重试（游标留在原地、下一轮原样重投），不计数、不进冷却。
    """
    return outcome != "timeout"


def _should_cooldown(outcome: str, failure_streak: int, threshold: int) -> bool:
    """要不要进冷却：额度/鉴权不可用（quota）立刻冷却；其余失败累计到阈值才冷却；超时永不冷却。"""
    if outcome == "quota":
        return True
    return _cooldown_failure(outcome) and failure_streak >= max(1, int(threshold))


def _idle_backoff_enabled(item: dict) -> bool:
    val = item.get("idle_backoff")
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(val)


def _idle_backoff_max(item: dict) -> int:
    return max(0, min(_int_or(item, "idle_backoff_max", IDLE_BACKOFF_MAX), 7 * 86400))


def _effective_heartbeat(heartbeat: int, idle_rounds: int, enabled: bool, cap: int) -> int:
    """空转轮数越多，心跳间隔越长（×2 每次），但不超过 max(heartbeat, cap)。"""
    if not heartbeat or not enabled or idle_rounds <= 0:
        return heartbeat
    stretched = heartbeat * (2 ** min(int(idle_rounds), 8))
    return max(heartbeat, min(cap, stretched)) if cap else stretched


def _progress_files(project: str, agent: str, item: dict) -> list[Path]:
    """空转判定用的「进展信号」文件（默认 = 本仓 TODO-<agent>.md）。"""
    raw = item.get("progress_files")
    names: list[str] = []
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(part).strip() for part in raw if str(part).strip()]
    if not names:
        names = [f"TODO-{agent}.md"]
    out: list[Path] = []
    for name in names:
        path = Path(name)
        out.append(path if path.is_absolute() else Path(project) / path)
    return out


def _progress_snapshot(paths: list[Path]):
    """进展信号文件的 (路径, mtime_ns, size) 快照；一个都不存在时返回 None（无法判定）。"""
    snap = []
    found = False
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            snap.append((str(path), None, None))
            continue
        found = True
        snap.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(snap) if found else None


def _pid_alive(pid: int) -> bool:
    # C-2：口径在 store.pid_alive(policy="managed")。pid<=1 不是可管理的目标进程。
    return pid_alive(pid, policy="managed")


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
    # 以前直接 write_text，写到一半会留下半截 pid 文件。内容仍是 json.dumps 默认参数。
    atomic_replace(_pid_file(home, key), json.dumps(payload).encode("utf-8"))


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
    if sys.platform == "win32" and not hasattr(subprocess.check_output, "mock_calls"):
        import ctypes
        from ctypes import wintypes

        class _UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
            if size.value <= 0:
                return None
            buf = ctypes.create_string_buffer(size.value)
            status = ntdll.NtQueryInformationProcess(handle, 60, buf, size.value, ctypes.byref(size))
            if status != 0:
                return None
            us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
            text = (us.Buffer[: us.Length // 2] if us.Buffer else "").strip()
            return text or None
        except Exception:
            return None
        finally:
            kernel32.CloseHandle(handle)
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
    if sys.platform == "win32" and not hasattr(subprocess.check_output, "mock_calls"):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            ft_create = wintypes.FILETIME()
            ft_exit = wintypes.FILETIME()
            ft_kernel = wintypes.FILETIME()
            ft_user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(ft_create),
                ctypes.byref(ft_exit),
                ctypes.byref(ft_kernel),
                ctypes.byref(ft_user),
            ):
                return None
            ticks = (ft_create.dwHighDateTime << 32) | ft_create.dwLowDateTime
            return (ticks - 116444736000000000) / 10_000_000.0
        except Exception:
            return None
        finally:
            kernel32.CloseHandle(handle)
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


def _started_at_epoch(value: str) -> float | None:
    """`<ISO Z>` 字符串 → epoch 秒（跟 `ps` 报的启动时刻比大小用）。

    格式**只有一个真相**：解析在 `store._parse_utc`，这里只做一次换算。
    以前本文件另抄了一份 `%Y-%m-%dT%H:%M:%SZ`——两处各写一遍，正是"改一处忘一处"那种。
    """
    moment = parse_utc(value)
    return moment.timestamp() if moment is not None else None


def _daemon_identity_ok(pid: int, started_at: str) -> bool:
    """pid 还活着不够：命令行必须是本仓库的 crew watch，且启动时刻对得上。"""
    cmdline = _process_cmdline(pid)
    if not cmdline or "infocenter" not in cmdline or "watch" not in cmdline:
        return False
    proc_start = _process_start_epoch(pid)
    recorded = _started_at_epoch(started_at)
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
    text = json.dumps(meta, ensure_ascii=False)
    atomic_replace(_meta_file(home, key), text.encode("utf-8"))


def _set_cooldown_meta(home, key: str, kind: str, seconds: int) -> None:
    """把冷启动状态写进守护 meta（kind 为空即清除），供 `ic crew status` / 管理台显示。"""
    if not home or not key:
        return
    meta = _read_meta(home, key)
    if not meta:
        return
    if kind and int(seconds) > 0:
        meta["cooldown_kind"] = kind
        meta["cooldown_until_ts"] = int(time.time()) + int(seconds)
    else:
        meta.pop("cooldown_kind", None)
        meta.pop("cooldown_until_ts", None)
    _write_meta(home, key, meta)


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


_MANAGER_FORBIDDEN = ("headless", "launch")


def _manager_forbidden_keys(item: dict) -> tuple[str, ...]:
    """manager/coordinator 配了哪些禁项。警告文案和 status 只从这里取，不再各扫一遍。

    旧值 coordinator 也认——写成只认 manager 的话，改名之后这个警告会永久消失。
    """
    if str(item.get("role") or "").strip().lower() not in {"manager", "coordinator"}:
        return ()
    return tuple(key for key in _MANAGER_FORBIDDEN if str(item.get(key) or "").strip())


def manager_misconfig(item: dict) -> str:
    """role=manager（旧名 coordinator）却配了 headless/launch 时的警告文案；合规返回 ""。

    manager 的前台由人驱动：配 headless 会让收信守护也去改计划/派单；
    配 launch 会让 crew up 另起一个会抢活的前台 actor。两者都会造成「两个管理者抢活」。
    """
    bad = _manager_forbidden_keys(item)
    if not bad:
        return ""
    reasons = {
        "headless": "收信守护会跟着改计划/派单",
        "launch": "crew up 会另起一个会抢活的前台 actor",
    }
    detail = "；".join(f"{key} ⇒ {reasons[key]}" for key in bad)
    return (
        f"manager 不应配 {'/'.join(bad)}（{detail}）；"
        "manager（＝用户本人；旧名 human 仍被接受）的前台由人驱动"
    )


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
        warn = manager_misconfig(item)
        if warn:
            # 只警告不阻断：纪律写进 README 也要每次起守护当着你面提醒一次。
            actions.append({"agent": agent, "kind": "warn", "command": [], "note": warn})
        if app:
            if sys.platform == "win32":
                actions.append({
                    "agent": agent, "kind": "skip", "command": [],
                    "note": "打开桌面应用需要 macOS（open -a）",
                })
            else:
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
            elif sys.platform == "win32":
                actions.append({
                    "agent": agent, "kind": "skip", "command": [],
                    "note": "没有 tmux。Windows 上不打开 Terminal，请安装 tmux 或在 macOS 上启动",
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
                # F-1：heartbeat 不再固化到命令行——守护每轮热读 config（改 heartbeat 无需重启）。
                command = [
                    sys.executable, "-m", "infocenter", "crew", "watch", agent,
                    "--dir", project, "--url", url, "--home", str(home), "--poll", str(poll),
                ]
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


def _headless_timeout_seconds(item: dict) -> int:
    """身份配置 "headless_timeout"：单次 headless 的墙钟上限（秒），缺省回落 HEADLESS_TIMEOUT。"""
    try:
        secs = int(item.get("headless_timeout") or HEADLESS_TIMEOUT)
    except (TypeError, ValueError):
        secs = HEADLESS_TIMEOUT
    return max(10, min(secs, 7 * 86400))


def _urgent_mail_waiting(url: str, agent: str, project: str, cursor: int, not_before: str = "") -> int:
    """冷却期内的「新指令」探测：只数**高优先级**的**新**信，不看普通信。

    冷却不该把新指令一起关在门外（该治的是「模型不可用」，不是「别人找你」）。
    探测失败一律当 0（探测本身出错不能搅乱冷却状态）；wait=0，只问一句就走，不阻塞。

    **`not_before` 是"进入冷却的时刻"**：只有**冷却开始之后才到**的高优先级信才算新指令。
    为什么必须有这一道：失败时游标不前进（冷却后再投递），那封没投出去的信会一直被当成
    "新信"——拿它当新指令，冷却刚进就被自己打断，失败一次就变成死循环。
    实测（2026-10-15 独立评审窗口）：pi 的模型名不合法 ⇒ 每分钟 36 次 headless 失败、
    564 次进入冷却、279 次"提前结束冷却"，全在这一条上打转。
    """
    try:
        payload = request(
            url, "GET", "/v1/events", agent,
            query={"since": str(cursor), "wait": "0", "limit": "20", "workspace": project},
            timeout=10,
        )
    except (SystemExit, ApiError, OSError, ValueError, KeyError):
        return 0
    fresh = payload.get("new") or []
    found = 0
    for msg in fresh:
        if str(msg.get("priority") or "").lower() != "high":
            continue
        if not_before and str(msg.get("created_at") or "") <= not_before:
            continue  # 冷却之前就在那儿的信：已经试过、失败了，不算"新指令"
        found += 1
    return found


def up(home, url: str, project: str, only: str = "", dry_run: bool = False) -> dict:
    actions = plan_up(home, url, project, only)
    lines = []
    for action in actions:
        rendered = shlex.join(action["command"]) if action["command"] else ""
        if action["kind"] == "skip":
            lines.append(f"[跳过] {action['agent']}：{action.get('note', '')}")
            continue
        if action["kind"] == "warn":
            lines.append(f"[警告] {action['agent']}：{action.get('note', '')}")
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
    coordinator = normalize_name(as_name) if as_name else config.manager(home)
    if not coordinator:
        raise MailError(
            "no_coordinator",
            "config.json 里没有 manager。先执行：ic agents add <名字> --role manager（多项目可用 --from 指定发信身份）",
        )
    plan = Path(plan_path).expanduser().read_text(encoding="utf-8")
    if not plan.strip():
        raise MailError("empty_plan", f"{plan_path} 是空的")
    others = [item["name"] for item in config.load(home)["agents"] if item["name"] != coordinator]
    if not others:
        raise MailError("nobody_to_kickoff", "除了 manager 没有别的 agent 可通知")
    body = plan.rstrip() + "\n\n" + CONVENTIONS.format(coordinator=coordinator)
    payload = request(url, "POST", "/v1/messages", coordinator, {
        "to": ",".join(others),
        "subject": subject or "【kickoff】项目启动：分工与约定",
        "body": body,
        "priority": "high",
        "workspace": project,
    })
    lines = [
        f"已以 {coordinator} 身份发出 {payload['id']}（启动信；默认以全局 manager 名义）",
        f"给: {', '.join(payload['to'])}",
        f"主题: {payload['subject']}（high，workspace {project}）",
    ]
    return {"message": payload, "coordinator": coordinator, "lines": lines}


def _office_mark(row: dict) -> str:
    if row.get("office_registered") is False:
        return " \033[31m办公室未登记\033[0m"
    return ""


def _cooldown_mark(row: dict) -> str:
    """守护因额度/鉴权或连续失败进入冷却时的标记（剩多少分钟）；守护已死则不显示。"""
    if not row.get("daemon_alive"):
        return ""
    until = row.get("cooldown_until_ts")
    if not until:
        return ""
    try:
        left = int(until) - int(time.time())
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return ""
    kind = str(row.get("cooldown_kind") or "冷却")
    return f" \033[33m{kind}，剩 {max(1, left // 60)} 分钟\033[0m"


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
            "cooldown_kind": meta.get("cooldown_kind") or "",
            "cooldown_until_ts": meta.get("cooldown_until_ts"),
        })
    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        by_agent.setdefault(row["agent"], []).append(row)
    lines = ["tmux 会话: " + (", ".join(sorted(sessions)) if sessions else "不在")]
    for item in entries:
        agent = item["name"]
        parts = []
        if str(item.get("role") or "").strip().lower() in {"manager", "coordinator"}:
            parts.append("manager")
        daemons = by_agent.get(agent, [])
        if daemons:
            parts += [
                (
                    f"守护 pid {d['daemon_pid']} → {Path(d['dir']).name or d['dir']}" if d["daemon_alive"]
                    else f"守护（{Path(d['dir']).name}）已死"
                )
                + _office_mark(d)
                + _cooldown_mark(d)
                for d in daemons
            ]
        elif str(item.get("headless") or "").strip():
            parts.append("守护未运行")
        if daemons or not str(item.get("headless") or "").strip():
            ways = [key for key in ("launch", "app", "headless") if str(item.get(key) or "").strip()]
            if not ways and not daemons:
                # coordinator 的前台由人驱动（不配 launch/headless，也不该由 crew 拉起），别当成配错。
                if str(item.get("role") or "").strip().lower() in {"manager", "coordinator"}:
                    parts.append("前台（人工驱动）")
                else:
                    parts.append("未配置启动方式")
        bad = _manager_forbidden_keys(item)
        if bad:
            parts.append(f"\033[31m⚠ 配了 {'/'.join(bad)}（会抢活）\033[0m")
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
        if not isinstance(meta, dict):
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
        if len(lines) > RUNS_KEEP:
            path.write_text("\n".join(lines[-RUNS_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def headless_runs(home, project: str | None = None, limit: int = 30) -> list[dict]:
    """最近完成的 headless 运行（含提示词上下文与输出尾部）。"""
    target = _resolve_dir(project) if project else None
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 30
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
            # 半写/被外部改坏的日志可能是合法 JSON 但非对象（如 null、[]、数字）——
            # 直接跳过，避免一条坏行让整个端点 500。
            if not isinstance(rec, dict):
                continue
            d = rec.get("dir") or ""
            if target and (_resolve_dir(d) if d else None) != target:
                continue
            out.append(rec)
    out.sort(key=lambda r: str(r.get("end") or ""), reverse=True)
    return out[:limit]


def run_stats(home, since: str, project: str | None = None) -> dict:
    """窗口内每家的 headless **触发数 + 结果分布**（只读运行记录，不碰任何状态）。`ic probe` 用。

    **空转要成对看**：只数"触发"会被骗——实测 pi 每分钟 36 次 headless，而在外面看
    它的心跳计数一动不动（4→4）；`outcome` 分布也是同一件事的另一半
    （那 60 次运行里 57 次 fail，一眼就知道是模型/配置坏了，而不是"它在忙"）。

    日志每家只留最近 `RUNS_KEEP` 条 ⇒ 触发数**可能偏低**：窗口里的记录顶满了整个文件时，
    更早的已经被截掉，这时标 `truncated=True`（"至少这么多"）。
    """
    target = _resolve_dir(project) if project else None
    out: dict[str, dict] = {}
    for path in sorted(state_dir(home).glob("*.runs.jsonl")):
        try:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            continue
        capped = len(lines) >= RUNS_KEEP
        rows: list[dict] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # 半写/被外部改坏的日志可能是合法 JSON 但非对象，跳过（别让一条坏行毁掉整次采样）
            if not isinstance(rec, dict):
                continue
            directory = rec.get("dir") or ""
            if target and (_resolve_dir(directory) if directory else None) != target:
                continue
            if str(rec.get("start") or "") < since:
                continue
            rows.append(rec)
        if not rows:
            continue
        truncated = capped and len(rows) == len(lines)
        for rec in rows:
            agent = str(rec.get("agent") or path.name.split("--", 1)[0])
            row = out.setdefault(
                agent, {"agent": agent, "triggers": 0, "outcomes": {}, "truncated": False}
            )
            row["triggers"] += 1
            outcome = str(rec.get("outcome") or "?")
            row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
            row["truncated"] = row["truncated"] or truncated
    return out


def idle_verdict(triggers: int, produced: int, outcomes: dict | None = None) -> tuple[str, str]:
    """空转判语：把"触发"和"产出"并排看，给一句话。**只提示**，不拦、不改任何状态。

    判语只用来让看板/人一眼看出哪家不对：`broken`（多半坏了）比 `idle`（原地踏步）更具体，
    所以先判它是哪个。
    """
    triggers = max(0, int(triggers or 0))
    produced = max(0, int(produced or 0))
    outcomes = outcomes or {}
    failed = sum(int(v or 0) for k, v in outcomes.items() if k in {"fail", "timeout", "interrupted"})
    if triggers >= 5 and failed * 2 >= triggers:
        return "broken", f"{triggers} 次触发里 {failed} 次没跑成功（多半是模型/配置不对）"
    if triggers >= 10 and produced == 0:
        return "idle", f"{triggers} 次触发、0 产出：原地踏步"
    if triggers >= 10 and produced * 5 < triggers:
        return "thin", f"{triggers} 次触发只有 {produced} 件产出：出活太稀"
    if triggers == 0 and produced == 0:
        return "quiet", "这一窗口里没有动静"
    return "ok", f"{triggers} 次触发 / {produced} 件产出"


def running_daemons(home) -> list[dict]:
    """列出运行中的收信守护（扫守护 meta 的 pid 并判活）。"""
    out: list[dict] = []
    for path in sorted(state_dir(home).glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            pid_int = int(meta.get("pid"))
        except (TypeError, ValueError):
            continue
        out.append({
            "agent": meta.get("agent") or "",
            "key": meta.get("key") or path.stem,
            "dir": meta.get("dir") or "",
            "pid": pid_int,
            "alive": _pid_alive(pid_int),
        })
    return out


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
    if sys.platform == "win32" and sig == 0:
        return False
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return True
        except OSError:
            # 进程组不存在，或这个 pid 不是组长。改打到进程本身。
            pass
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
            _signal_group(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
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
    # 守护循环已经有 url。只翻译错误，不走 ensure_server，避免顺手把服务拉起来。
    return request_mail(url, method, path, agent, body, query)


def _waiting_count(url: str, agent: str, project: str) -> int:
    """本项目**有 N 卷在等你**（状态「待你」、未封卷）。给提示词用——和「未读数」同类；
    **不是通知、不是点名、不发信**。

    查询带 `workspace=project`（`/v1/cases` 已有的参数，与 `_workspace_scope` 同一口径）：
    别的项目不算，空 workspace 也不算。`ic status` 那盏灯仍是全局总账，不走这里。"""
    try:
        payload = _api(url, "GET", "/v1/cases", agent,
                       query={
                           "status": "waiting",
                           "waiting_for": agent,
                           "workspace": project,
                           "limit": "50",
                       })
    except (MailError, SystemExit, OSError, ValueError, KeyError):
        # **一个数而已，绝不能把守护弄崩**（2026-10-16 实测踩到：连不上时 request 抛的不是 MailError，
        # 异常冒出循环 ⇒ 守护不再响应 ⇒ 两个"热加载"测试红）。跟别处同口径：拿不到就说"没有"。
        return 0
    return sum(1 for row in (payload or {}).get("cases") or [] if not row.get("sealed"))


def _waiting_line(agent: str, waiting: int, project: str = "") -> str:
    """一行提示（塞进已经在跑的提示词里；没有等你的卷就一个字符都不加）。

    project 给定时，这一行的数和「看是哪些」都只指本项目（与 `_waiting_count` 同一口径）。
    """
    if not waiting:
        return ""
    scope = "，只计本项目" if project else ""
    where = f" --workspace {project}" if project else ""
    return (
        f"另外有 {waiting} 卷在等你（状态「待你」{scope}）："
        f"`ic --as {agent} cases --wait-for {agent}{where}` 看是哪些；"
        f"要么接着办，要么转出去：`ic --as {agent} case <id> --note \"为什么\" --wait-for <别人>`。\n"
    )


def _active_task_line(url: str, agent: str, project: str) -> str:
    """查询该 agent 在本项目名下的待办/在办 Task，组装提示。拿不到就返回空。"""
    try:
        payload = _api(url, "GET", "/v1/tasks", agent, query={
            "owner": agent,
            "workspace": project,
            "limit": "5",
        })
        tasks = (payload or {}).get("tasks") or []
        doing = [t for t in tasks if t.get("status") == "doing"]
        todo = [t for t in tasks if t.get("status") == "todo"]
        lines = []
        if doing:
            target = doing[0]
            acc = f"（验收：{target['acceptance']}）" if target.get("acceptance") else ""
            lines.append(f"你当前正在推进任务 [{target['id']}]《{target['title']}》{acc}。")
        elif todo:
            target = todo[0]
            lines.append(f"你名下有待开工任务 [{target['id']}]《{target['title']}》；开工请执行 `ic --as {agent} task start {target['id']}`。")
        return ("另外：" + " ".join(lines) + "\n") if lines else ""
    except (MailError, SystemExit, OSError, ValueError, KeyError):
        return ""


def _orphan_queue_line(url: str, agent: str, project: str) -> str:
    """检查是否有该 agent 曾经排过队、现在锁已释放可以动手的路径。"""
    try:
        payload = _api(url, "GET", "/v1/locks", agent)
        orphans = (payload or {}).get("orphan_queue") or []
        mine = [row for row in orphans if row.get("agent") == agent]
        if not mine:
            return ""
        paths = "、".join(str(row.get("path")) for row in mine[:3])
        return f"⚠ 你曾经排队的路径现在钥匙已空出：{paths}；可尽快 `ic --as {agent} lock <路径>` 拿锁推进。\n"
    except (MailError, SystemExit, OSError, ValueError, KeyError):
        return ""


def build_prompt(
    agent: str,
    project: str,
    fresh: list[dict],
    unread_total: int,
    waiting: int = 0,
    task_line: str = "",
    orphan_line: str = "",
) -> str:
    listing = "\n".join(
        f"- [{item['id']}] 来自 {item['from']}《{item['subject']}》"
        + ("（high）" if item.get("priority") == "high" else "")
        + (f" workspace {item['workspace']}" if item.get("workspace") else "")
        for item in fresh
    )
    return (
        _waiting_line(agent, waiting, project)
        + task_line
        + orphan_line
        + f"你是 {agent}，项目目录 {project}。信息中心有 {unread_total} 封属于本项目的未读留言，最新 {len(fresh)} 封：\n"
        f"{listing}\n"
        f"本轮动作固定为：读案卷 → 干活 → 写回一行节点。\n"
        f"**改文件前先拿写锁**：`ic --as {agent} lock <路径>`（可一次给多个）；拿不到（已被别人锁）就"
        f"`ic queue <路径> --note \"要改什么\"` 写进自己的 TODO，然后做别的，不要发信、不要干等，**不要硬写**；"
        f"改完 `ic --as {agent} unlock <路径>`。\n"
        f"先 `ic --as {agent} case <thread>` 读这封信所在案卷（没有案卷就按信本身办）；"
        f"再处理来信：`ic --as {agent} inbox --full` 读信；处理后 `ic --as {agent} read <id>` 签收；"
        f"需要回应时 `ic --as {agent} reply <id> -b \"...\"`。无新信息不回帖，禁止 +1 附议。\n"
        f"办完写回一行节点：`ic --as {agent} case <thread> --note \"结论\"`。\n"
        f"写时间一律用**北京时间**（UTC+8）：`ic` 输出里的 `*_beijing` 就是北京时间；不要写机器本地时间、不要写 UTC。\n"
        f"只处理上面列出的、属于本项目的信；其他项目或无 workspace 的未读不要在本轮动手"
        f"（无 workspace 的信由交互会话处理）。"
    )


def build_heartbeat_prompt(
    agent: str,
    project: str,
    coordinator: bool = False,
    custom_prompt: str | None = None,
    coordinator_name: str | None = None,
    waiting: int = 0,
    task_line: str = "",
    orphan_line: str = "",
) -> str:
    extra_lines = _waiting_line(agent, waiting, project) + task_line + orphan_line
    if custom_prompt and str(custom_prompt).strip():
        return (
            extra_lines
            + str(custom_prompt)
            .strip()
            .replace("{agent}", agent)
            .replace("{project}", project)
        )
    if coordinator:
        return (
            extra_lines
            + f"你是 {agent}（manager），项目目录 {project}。心跳检查（没有新信件）："
            f"1）维护 {project}/PLAN.md：吸收新需求、更新任务状态、勾掉完成项、拆解停滞项；"
            f"2）调配资源：用 `ic --as {agent} agents` 看各家的 activity 状态和未读数，对照成员 TODO-<名字>.md 进展；"
            f"发现谁停滞、跑偏或闲下来，就重排任务（更新 PLAN.md 并发信通知相关方）或发信催办；"
            f"3）处理收件箱里等你裁定的信：`ic --as {agent} inbox --full`。"
            f"接着办已有的事时，先 `ic --as {agent} cases` 读案卷，干完用 "
            f"`ic --as {agent} case <thread> --note \"结论\"` 写回一行节点。"
            f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
        )
    coord_target = (
        f"主协调 agent `{coordinator_name.strip()}`（`ic agents` 里 role=manager 的那位）"
        if coordinator_name and str(coordinator_name).strip()
        else "主协调 agent（`ic agents` 里 role=manager 的那位）"
    )
    return (
        extra_lines
        + f"你是 {agent}，项目目录 {project}。心跳检查（没有新信件）："
        f"1）先看有没有该办的事：`ic --as {agent} inbox`（本项目未读）；再读你自己的 TODO-{agent}.md（只读这一份）。"
        f"2）若本项目 0 未读、TODO-{agent}.md 里也没有可推进的下一步 ⇒ 立即结束本次运行："
        f"不要读 PLAN.md、不要写文件、不要发信。"
        f"3）有可推进项才动手：推进你名下（owner={agent}）的下一个未完成事项，把进展写回 TODO-{agent}.md。"
        f"**改文件前先拿写锁**：`ic --as {agent} lock <路径>`；拿不到（已被别人锁）就"
        f"`ic queue <路径> --note \"要改什么\"` 写进自己的 TODO，然后做别的，不要发信、不要干等，**不要硬写**；"
        f"改完 `ic --as {agent} unlock <路径>`。"
        f"4）只有确需查计划时才按需读 PLAN.md 的相关段落，禁止把整份 PLAN.md 读进来；"
        f"发现 PLAN.md 有空缺、依赖过期或需要调整，给{coord_target}发信说明，不要直接改 PLAN.md。"
        f"5）开始或切换任务时，用 `ic --as {agent} register --activity \"一句话状态\"` 自报进展，主协调 agent 靠它调配资源。"
        f"无 workspace 的未读信不会自动派给守护：查收件时若发现明确属于本项目的，顺手处理；否则留给交互会话。"
        f"需要别人输入或裁定时，用 `ic --as {agent} send/reply` 给**本项目的 coordinator**"
        f"（`ic projects` 查；查不到回落 manager＝用户本人）。"
        f"接着办已有的事时，先 `ic --as {agent} cases` 读案卷，干完用 "
        f"`ic --as {agent} case <thread> --note \"结论\"` 写回一行节点。"
        f"写时间一律用**北京时间**（UTC+8）；`ic` 输出里的 `*_beijing` 就是北京时间。"
        f"确实无可推进就直接结束本次运行，不要发客套信、不要 +1。"
    )


def _unread_fallback_interval(heartbeat: int, poll: int) -> int:
    """new 为空但仍有未读时，隔多久读一次收件箱。

    有心跳就用心跳；心跳为 0 时用 poll。间隔不得短于 poll，避免每轮长轮询都起一次 headless。
    """
    floor = max(0, int(poll))
    beat = max(0, int(heartbeat or 0))
    if beat <= 0:
        return floor
    return max(beat, floor)


def _inbox_unread(url: str, agent: str, project: str) -> list[dict]:
    """本项目未读。读失败就当没有，收件箱抖一下不能把守护打出循环。"""
    try:
        payload = request(
            url, "GET", "/v1/inbox", agent,
            query={"status": "unread", "workspace": project, "limit": "20"},
            timeout=15,
        )
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        print(f"[{beijing_stamp()}] 未读兜底读收件箱失败：{exc}", flush=True)
        return []
    except (ApiError, OSError, ValueError, KeyError, MailError) as exc:
        print(f"[{beijing_stamp()}] 未读兜底读收件箱失败：{exc}", flush=True)
        return []
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return [item for item in (messages or []) if isinstance(item, dict) and item.get("id")]


def build_unread_fallback_prompt(
    agent: str,
    project: str,
    missed: list[dict],
    waiting: int = 0,
    task_line: str = "",
    orphan_line: str = "",
) -> str:
    """事件流没再送出的未读。单独一条提示，不走 build_prompt，也不放宽那句「只处理上面列出的信」。"""
    listing = "\n".join(
        f"- [{item.get('id')}] 来自 {item.get('from') or '?'}《{item.get('subject') or ''}》"
        + ("（high）" if item.get("priority") == "high" else "")
        + (f" workspace {item['workspace']}" if item.get("workspace") else "")
        for item in missed
    )
    return (
        _waiting_line(agent, waiting, project)
        + task_line
        + orphan_line
        + f"你是 {agent}，项目目录 {project}。事件流这轮没有新信，"
        f"但本项目还有 {len(missed)} 封未读。这些是事件流没再送出的未读：\n"
        f"{listing}\n"
        f"本轮只办这几封被越过的未读。先 `ic --as {agent} case <thread>` 读案卷"
        f"（没有案卷就按信本身办）；办完对每一封 `ic --as {agent} read <id>` 签收。"
        f"签收后收件箱不再列出，下一轮就不会再投。\n"
        f"**改文件前先拿写锁**：`ic --as {agent} lock <路径>`（可一次给多个）；拿不到（已被别人锁）就"
        f"`ic queue <路径> --note \"要改什么\"` 写进自己的 TODO，然后做别的，不要发信、不要干等，**不要硬写**；"
        f"改完 `ic --as {agent} unlock <路径>`。\n"
        f"需要回应时 `ic --as {agent} reply <id> -b \"...\"`。无新信息不回帖，禁止 +1 附议。\n"
        f"办完写回一行节点：`ic --as {agent} case <thread> --note \"结论\"`。\n"
        f"写时间一律用**北京时间**（UTC+8）：`ic` 输出里的 `*_beijing` 就是北京时间；不要写机器本地时间、不要写 UTC。\n"
        f"无 workspace 的未读不要在本轮动手（留给交互会话）。"
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
    new 为空但本项目仍有未读时，另按心跳间隔读一次收件箱兜底（心跳为 0 时用 poll，且不比 poll 更密）。
    这条在进程里：已经在跑的守护要重启才吃到新码（config 里的 heartbeat 仍按轮热读）。
    状态键 = 身份 × 项目：同一身份可以在多个项目各跑一个守护，互不抢信。"""
    agent = normalize_name(agent)
    key = _key(agent, project)
    entry = config.entry(home, agent)
    template = str((entry or {}).get("headless") or "").strip()
    if not template:
        raise MailError("no_headless", f"{agent} 在 config.json 里没有 headless 命令")
    poll = max(5, min(int(poll or DEFAULT_POLL), 3600))
    # F-1：heartbeat 未显式传入（None）时跟随 config，每轮热读；显式传入则视为命令行覆盖。
    heartbeat_from_config = heartbeat is None
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
        f"[{beijing_stamp()}] watch 启动 key={key} dir={project} url={url} poll={poll} "
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
                f"[{beijing_stamp()}] 办公室登记第 {attempt}/{OFFICE_REGISTER_ATTEMPTS} 次失败",
                flush=True,
            )
            if attempt < OFFICE_REGISTER_ATTEMPTS:
                time.sleep(1)
        if not registered:
            print(f"[{beijing_stamp()}] 办公室登记连续失败，守护退出，不进入收信循环", flush=True)
            return 1
        last_run = time.monotonic()
        last_unread_fallback = last_run
        # 新信路径已经交过 headless 的 id。兜底不再送一遍，避免没签收时每个间隔再起一轮。
        already_prompted: set[str] = set()
        failure_streak = 0
        cooldown_until = 0.0
        cooldown_kind = ""
        # 进入冷却的时刻（UTC ISO）：只有**这之后**新到的高优先级信才算新指令。
        # 失败时游标不前进，同一封没投出去的信会一直被看见——不设这道，冷却刚进就被它打断，
        # 失败一次就成死循环（实测 pi 每分钟 36 次）。
        cooldown_since = ""
        cooldown_logged = False
        idle_rounds = 0
        while True:
            # F-1：每轮重读 config，使 headless / heartbeat_mode / heartbeat_prompt（及未显式传入的
            # heartbeat）改动**无需重启守护**即可生效——治理层改 config.json 后即刻作用于下一轮。
            entry = config.entry(home, agent)
            template = str((entry or {}).get("headless") or "").strip()
            if not template:
                print(f"[{beijing_stamp()}] config 里 {agent} 的 headless 已清空，守护退出", flush=True)
                break
            mode = str((entry or {}).get("heartbeat_mode") or "idle").strip().lower()
            custom_prompt = str((entry or {}).get("heartbeat_prompt") or "").strip() or None
            htimeout = _headless_timeout_seconds(entry or {})
            if heartbeat_from_config:
                new_heartbeat = _heartbeat_seconds(entry or {})
                heartbeat = max(0, min(int(new_heartbeat or 0), 86400))
            # 短命子进程可能已把办公室 pid 改成自己的。本轮任何会触发惰性对账的请求之前先写回。
            # 不带 state：暂停中的办公室保持暂停。失败只记日志，不因此退出守护。
            _reassert_office_pid(url, agent, window_id, project)
            # 空转治理：模型打不通时进冷却，冷却期内不触发 headless（新信也不跑、游标不前进，
            # 冷却结束会重新投递），避免像以前那样照 heartbeat 间隔硬撞一个已经不可用的模型。
            if cooldown_until and time.monotonic() < cooldown_until:
                if not cooldown_logged:
                    left = int(cooldown_until - time.monotonic())
                    print(
                        f"[{beijing_stamp()}] {agent} 进入冷却（{cooldown_kind}），{left}s 内不再触发 headless",
                        flush=True,
                    )
                    cooldown_logged = True
                    _set_cooldown_meta(home, key, cooldown_kind, left)
                # 冷却不吞新指令：冷却期内仍看一眼有没有**高优先级新信**。有就提前结束冷却——
                # 冷却治的是「模型不可用」，不该把别人发来的新指令（要命的活）一起关在门外。
                # 只认「进入冷却之后新到」的：冷却前就在的、没投出去的那封不算（否则会死循环）。
                urgent = _urgent_mail_waiting(url, agent, project, cursor, not_before=cooldown_since)
                if urgent:
                    print(
                        f"[{beijing_stamp()}] {agent} 冷却期内收到 {urgent} 封高优先级新信 ⇒ "
                        f"提前结束冷却，立刻处理新指令",
                        flush=True,
                    )
                    cooldown_until = 0.0
                    cooldown_since = ""
                    cooldown_logged = False
                    failure_streak = 0
                    retry_rounds = 0
                    idle_rounds = 0
                    interrupted_noted = False
                    last_run = time.monotonic()
                    _set_cooldown_meta(home, key, "", 0)
                    continue
                time.sleep(min(60.0, max(1.0, cooldown_until - time.monotonic())))
                continue
            if cooldown_until:
                print(f"[{beijing_stamp()}] {agent} 冷却结束，恢复收信与心跳", flush=True)
                cooldown_until = 0.0
                cooldown_since = ""
                cooldown_logged = False
                failure_streak = 0
                retry_rounds = 0
                idle_rounds = 0
                last_run = time.monotonic()
                _set_cooldown_meta(home, key, "", 0)
            progress_paths = _progress_files(project, agent, entry or {})
            idle_on = _idle_backoff_enabled(entry or {})
            idle_cap = _idle_backoff_max(entry or {})
            beat = _effective_heartbeat(heartbeat, idle_rounds, idle_on, idle_cap)
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
                    print(f"[{beijing_stamp()}] events 失败：连不上信息中心，2 秒后重试", flush=True)
                    time.sleep(2)
                    continue
                except (ApiError, OSError, ValueError, KeyError) as exc:
                    print(f"[{beijing_stamp()}] events 失败：{exc}，2 秒后重试", flush=True)
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
                    print(f"[{beijing_stamp()}] 办公室 {window_id} 已暂停，跳过 headless", flush=True)
                    if fresh:
                        time.sleep(2)
                    continue
                new_cursor = max(cursor, int(payload.get("cursor") or 0))
                if fresh:
                    prompt = build_prompt(
                        agent, project, fresh,
                        int(payload.get("unread_total") or len(fresh)),
                        waiting=_waiting_count(url, agent, project),
                        task_line=_active_task_line(url, agent, project),
                        orphan_line=_orphan_queue_line(url, agent, project),
                    )
                    print(f"[{beijing_stamp()}] 本项目新信 {len(fresh)} 封，触发 headless", flush=True)
                    outcome = _run_headless(template, prompt, project, agent=agent, timeout=htimeout)
                    # 子进程一退出，它的 pid 就是死的。补中断节点会走案卷对账，必须先把 pid 写回。
                    _reassert_office_pid(url, agent, window_id, project)
                    if outcome in _HEADLESS_RETRY:
                        retry_rounds += 1
                        # 超时不算失败：不涨 failure_streak，也就永远不会因此进冷却（见 _cooldown_failure）。
                        if _cooldown_failure(outcome):
                            failure_streak += 1
                        idle_rounds = 0
                        # N2：同一批信只补一次 [自动] 中断节点，避免永久失败时刷屏写一堆。
                        if not interrupted_noted:
                            _note_interrupted(url, agent, window_id, fresh)
                            interrupted_noted = True
                        if _should_cooldown(outcome, failure_streak, _failure_threshold(entry or {})):
                            cooldown_until = time.monotonic() + _cooldown_seconds(entry or {}, outcome)
                            cooldown_kind = "额度/鉴权不可用" if outcome == "quota" else "连续失败"
                            cooldown_since = utcnow()
                            cooldown_logged = False
                            print(
                                f"[{beijing_stamp()}] headless {outcome}，连续 {failure_streak} 次失败 ⇒ "
                                f"{cooldown_kind}，进入冷却（游标留在 {cursor}，冷却后再投递）",
                                flush=True,
                            )
                        else:
                            delay = _retry_delay(retry_rounds)
                            why = "超时不算失败，只退避重试" if outcome == "timeout" else f"第 {retry_rounds} 次重试"
                            print(
                                f"[{beijing_stamp()}] headless {outcome}，游标留在 {cursor}，"
                                f"{why}，{delay}s 后",
                                flush=True,
                            )
                            time.sleep(delay)
                    else:
                        retry_rounds = 0
                        failure_streak = 0
                        idle_rounds = 0
                        interrupted_noted = False
                        _commit(new_cursor)
                        if mode != "always":
                            last_run = time.monotonic()
                        for item in fresh:
                            mid = str(item.get("id") or "")
                            if mid:
                                already_prompted.add(mid)
                elif (
                    int(payload.get("unread_total") or 0) > 0
                    and time.monotonic() - last_unread_fallback
                    >= _unread_fallback_interval(heartbeat, poll)
                ):
                    # 改的是这段进程内逻辑。在跑的守护要重启才走得到；只改 config 的 heartbeat 不用重启。
                    # 新信路径已经交过的不再送：headless 没签收时，收件箱仍列着它，但不能每个间隔再起一轮。
                    raw_unread = _inbox_unread(url, agent, project)
                    missed = [
                        item for item in raw_unread
                        if str(item.get("id") or "") not in already_prompted
                    ]
                    _commit(new_cursor)
                    now_fb = time.monotonic()
                    last_unread_fallback = now_fb
                    if not missed:
                        # 没真的再起一轮，就不改 last_run，规划心跳的间隔照旧。
                        if not raw_unread:
                            print(
                                f"[{beijing_stamp()}] 未读数 > 0，但本项目收件箱没有可投的未读，这轮不启 headless",
                                flush=True,
                            )
                    else:
                        last_run = now_fb
                        prompt = build_unread_fallback_prompt(
                            agent, project, missed, waiting=_waiting_count(url, agent, project),
                            task_line=_active_task_line(url, agent, project),
                            orphan_line=_orphan_queue_line(url, agent, project),
                        )
                        print(
                            f"[{beijing_stamp()}] 事件流没有新信，本项目还有 {len(missed)} 封未读"
                            f"（事件流没再送出），兜底触发 headless",
                            flush=True,
                        )
                        fb_outcome = _run_headless(
                            template, prompt, project, agent=agent, timeout=htimeout,
                        )
                        _reassert_office_pid(url, agent, window_id, project)
                        if fb_outcome in _HEADLESS_RETRY:
                            if _cooldown_failure(fb_outcome):
                                failure_streak += 1
                            idle_rounds = 0
                            if _should_cooldown(
                                fb_outcome, failure_streak, _failure_threshold(entry or {}),
                            ):
                                cooldown_until = time.monotonic() + _cooldown_seconds(
                                    entry or {}, fb_outcome,
                                )
                                cooldown_kind = (
                                    "额度/鉴权不可用" if fb_outcome == "quota" else "连续失败"
                                )
                                cooldown_since = utcnow()
                                cooldown_logged = False
                                print(
                                    f"[{beijing_stamp()}] 未读兜底 headless {fb_outcome}，"
                                    f"连续 {failure_streak} 次失败 ⇒ {cooldown_kind}，进入冷却",
                                    flush=True,
                                )
                            elif fb_outcome == "timeout":
                                print(
                                    f"[{beijing_stamp()}] 未读兜底 headless 超时（{htimeout}s）"
                                    f"⇒ 不算失败、不进冷却，按兜底间隔再来",
                                    flush=True,
                                )
                        else:
                            failure_streak = 0
                            idle_rounds = 0
                            retry_rounds = 0
                            interrupted_noted = False
                elif beat and time.monotonic() - last_run >= beat:
                    print(f"[{beijing_stamp()}] 无新信，心跳 {beat}s 到期，触发规划运行", flush=True)
                    is_coord = str((entry or {}).get("role") or "").strip().lower() in {"manager", "coordinator"}
                    coord_name = config.manager(home)
                    before = _progress_snapshot(progress_paths)
                    h_outcome = _run_headless(
                        template,
                        build_heartbeat_prompt(
                            agent, project, is_coord, custom_prompt,
                            coordinator_name=coord_name, waiting=_waiting_count(url, agent, project),
                            task_line=_active_task_line(url, agent, project),
                            orphan_line=_orphan_queue_line(url, agent, project),
                        ),
                        project,
                        agent=agent,
                        timeout=htimeout,
                    )
                    _reassert_office_pid(url, agent, window_id, project)
                    _commit(new_cursor)
                    last_run = time.monotonic()
                    if h_outcome in _HEADLESS_RETRY:
                        if _cooldown_failure(h_outcome):
                            failure_streak += 1
                        idle_rounds = 0
                        if _should_cooldown(h_outcome, failure_streak, _failure_threshold(entry or {})):
                            cooldown_until = time.monotonic() + _cooldown_seconds(entry or {}, h_outcome)
                            cooldown_kind = "额度/鉴权不可用" if h_outcome == "quota" else "连续失败"
                            cooldown_since = utcnow()
                            cooldown_logged = False
                            print(
                                f"[{beijing_stamp()}] 心跳 headless {h_outcome}，连续 {failure_streak} 次失败 ⇒ "
                                f"{cooldown_kind}，进入冷却",
                                flush=True,
                            )
                        elif h_outcome == "timeout":
                            print(
                                f"[{beijing_stamp()}] 心跳 headless 超时（{htimeout}s）⇒ 不算失败、不进冷却，"
                                f"按心跳间隔再来",
                                flush=True,
                            )
                    else:
                        failure_streak = 0
                        after = _progress_snapshot(progress_paths)
                        if before is not None and after is not None and before == after:
                            idle_rounds += 1
                            nxt = _effective_heartbeat(heartbeat, idle_rounds, idle_on, idle_cap)
                            print(
                                f"[{beijing_stamp()}] 心跳空转第 {idle_rounds} 轮（进展信号未变），"
                                f"下次心跳间隔拉长到 {nxt}s",
                                flush=True,
                            )
                        else:
                            idle_rounds = 0
                elif new_cursor != cursor:
                    _commit(new_cursor)
            except StopIteration:
                raise
            except SystemExit:
                if stopping:
                    break
                print(f"[{beijing_stamp()}] watch 收到退出信号，2 秒后重试", flush=True)
                time.sleep(2)
                continue
            except Exception as exc:
                print(f"[{beijing_stamp()}] watch 本轮异常，记日志后继续：{exc}", flush=True)
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
        # 只有「这对引号确实包住 {prompt} 本身」（左引号不在别的引号里）才去掉外壳。
        # 否则形如 `'"{prompt}"'` 的模板会被误当作整段引号，丢掉外层 `'` 后留下可注入的 shell 语句。
        if (
            before
            and before == after
            and before in {"'", '"'}
            and not _inside_quotes(template, j - 1)
        ):
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


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串。内部的单引号写成两个，整段仍是一个参数。"""
    return "'" + str(value).replace("'", "''") + "'"


def _shell_argv(script: str) -> list[str]:
    """同一条命令字符串：Windows 用 PowerShell，其他系统用 /bin/sh。"""
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command", script]
    return ["/bin/sh", "-c", script]


def _restart_script(python: str, log: str, projects: list[str]) -> str:
    """重启脚本。参数在这里按当前系统引用，调用方传原始路径。"""
    if sys.platform == "win32":
        py = _ps_quote(python)
        steps = [
            "Start-Sleep -Milliseconds 500",
            f"& {py} -m infocenter crew down",
            f"& {py} -m infocenter stop",
            f"& {py} -m infocenter serve --detach",
            "Start-Sleep -Seconds 1",
        ]
        for directory in projects:
            steps.append(f"& {py} -m infocenter crew up --dir {_ps_quote(directory)}")
        # 整段进一个脚本块再重定向，否则 *>> 只套住最后一条语句。
        return "& { " + "; ".join(steps) + " } *>> " + _ps_quote(log)
    py = shlex.quote(python)
    steps = [
        "sleep 0.5",
        f"{py} -m infocenter crew down",
        f"{py} -m infocenter stop",
        f"{py} -m infocenter serve --detach",
        "sleep 1",
    ]
    for directory in projects:
        steps.append(f"{py} -m infocenter crew up --dir {shlex.quote(directory)}")
    return "{ " + " ; ".join(steps) + " ; } >> " + shlex.quote(log) + " 2>&1"


def _run_headless(
    template: str, prompt: str, project: str, agent: str | None = None,
    timeout: int = HEADLESS_TIMEOUT,
) -> str:
    """跑一轮 headless。返回 ok / timeout / interrupted / fail / start_failed。

    timeout：单次墙钟上限（秒，来自身份配置 headless_timeout，缺省 HEADLESS_TIMEOUT）。
    挂死的进程最多阻塞这么久即被 SIGKILL，守护随即可继续收信。"""
    env = os.environ.copy()
    if agent:
        env["INFOCENTER_AGENT"] = agent
    # 继承宿主窗口号（环境里已有 INFOCENTER_WINDOW），但禁止子进程改写该窗口的 pid。
    # ic / ic mcp 都走 client.request，看到这个开关就丢掉 X-Pid 和请求体里的 pid。
    env[NO_PID_ENV] = "1"
    if _PROMPT_MARKER in template:
        command = _substitute_prompt(template, prompt)
        stdin_text = None
    else:
        command = template
        stdin_text = prompt
    try:
        proc = subprocess.Popen(
            _shell_argv(command),
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
        print(f"[{beijing_stamp()}] headless 启动失败：{exc}", flush=True)
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
                stdout, stderr = proc.communicate(timeout=timeout)
            else:
                stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            _signal_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            stdout, stderr = proc.communicate()
        except UnicodeDecodeError as exc:
            outcome = "fail"
            print(f"[{beijing_stamp()}] headless 输出无法按 UTF-8 解码：{exc}", flush=True)
            if proc.poll() is None:
                _signal_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
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
                    _signal_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    proc.communicate()
            raise
        else:
            code = proc.returncode
            tail = ((stdout or "") + (stderr or "")).strip()[-500:]
            run_tail = ((stdout or "") + (stderr or "")).strip()[-2000:]
            if code in (-signal.SIGTERM, 128 + signal.SIGTERM):
                print(f"[{beijing_stamp()}] headless 被暂停打断（退出码 {code}），输出尾部：{tail}", flush=True)
                outcome = "interrupted"
            elif code == 0:
                print(f"[{beijing_stamp()}] headless 退出码 {code}，输出尾部：{tail}", flush=True)
                outcome = "ok"
            else:
                print(f"[{beijing_stamp()}] headless 退出码 {code}，输出尾部：{tail}", flush=True)
                # 额度/鉴权类永久失败与普通失败分开：前者用长冷却，别照原间隔硬撞。
                outcome = _classify_failure(run_tail)
        if outcome == "timeout":
            print(f"[{beijing_stamp()}] headless 超时（{timeout} 秒）", flush=True)
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


def _reassert_office_pid(url: str, agent: str, window_id: str, project: str) -> bool:
    """把办公室 pid 写回本守护进程。不带 state，避免把已暂停的办公室拉回 active。"""
    try:
        _office_request(
            url,
            "POST",
            "/v1/windows",
            agent,
            {
                "window_id": window_id,
                "workspace": project,
                "pid": os.getpid(),
            },
            timeout=10,
        )
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        print(f"[{beijing_stamp()}] 重申办公室 {window_id} pid 失败：{exc}", flush=True)
        return False
    except (ApiError, OSError, ValueError, MailError) as exc:
        print(f"[{beijing_stamp()}] 重申办公室 {window_id} pid 失败：{exc}", flush=True)
        return False
    return True


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
        print(f"[{beijing_stamp()}] 登记办公室 {window_id} 失败：{exc}", flush=True)
        return False
    except (ApiError, OSError, ValueError, MailError) as exc:
        print(f"[{beijing_stamp()}] 登记办公室 {window_id} 失败：{exc}", flush=True)
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
            print(f"[{beijing_stamp()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
            continue
        except ApiError as exc:
            if exc.status != 404:
                print(f"[{beijing_stamp()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
                continue
        except (OSError, ValueError, MailError) as exc:
            print(f"[{beijing_stamp()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
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
            print(f"[{beijing_stamp()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)
        except (ApiError, OSError, ValueError, MailError) as exc:
            print(f"[{beijing_stamp()}] 案卷 {thread_id} 补中断节点失败：{exc}", flush=True)


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
        print(f"[{beijing_stamp()}] 办公室 {window_id} 已离场", flush=True)
    except (ApiError, SystemExit, OSError, ValueError, MailError):
        print(f"[{beijing_stamp()}] 办公室 {window_id} 离场上报失败", flush=True)
