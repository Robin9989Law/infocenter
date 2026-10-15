"""接入名单与团队编排在 ~/.infocenter/config.json，用户可自由增删，代码不固定任何 agent。

文件不存在时按 DEFAULT_AGENTS 生成一份，之后一切以文件为准：
直接编辑，或用 `ic agents add/remove`。

每个 agent 条目的可选字段：
  name         身份名（必填）
  description  描述
  role         "coordinator"（主协调）或空（成员）
  launch       交互启动命令，crew up 时在 tmux/Terminal 里于项目目录运行
  app          macOS 应用名，crew up 时 `open -a <app> <项目目录>`
  headless     无人值守命令模板，{prompt} 会被替换为收信处理指令；配了才会开守护
  poll         守护长轮询的等待上限（秒），默认 300
  heartbeat    无信心跳间隔（秒）：守护进程定期触发一次 headless 规划运行，推进 TODO；0 或不填=纯信件驱动
  heartbeat_mode    可选："idle"（默认，新信会重置心跳计时）或 "always"（严格按间隔调度，新信不重置）
  heartbeat_prompt  可选：替代内置的心跳规划提示词（支持 {agent} {project} 占位符）；不填用内置模板
  idle_backoff      可选，默认 true：心跳「空转」（进展信号文件未变）时，心跳间隔按 ×2 指数拉长
  idle_backoff_max  可选：空转退避的间隔上限（秒），默认 7200
  progress_files    可选：空转判定的「进展信号」文件（相对项目目录或绝对路径），默认 ["TODO-<名字>.md"]
  failure_threshold 可选：连续失败多少次后进入冷却，默认 3
  failure_cooldown  可选：普通失败冷却时长（秒），默认 3600
  permanent_cooldown 可选：额度/鉴权类永久失败的冷却时长（秒），默认 21600
"""

import json
from pathlib import Path

from infocenter.store import MailError, atomic_replace, normalize_name

DEFAULT_AGENTS = (
    {"name": "grok", "description": "Grok"},
    {"name": "kimi", "description": "Kimi Code"},
    {"name": "opencode", "description": "OpenCode"},
    {"name": "cursor", "description": "Cursor"},
    {"name": "human", "description": "用户"},
)

_KNOWN_KEYS = {
    "name", "description", "role", "launch", "app", "headless",
    "poll", "heartbeat", "heartbeat_mode", "heartbeat_prompt", "headless_timeout",
    "idle_backoff", "idle_backoff_max", "progress_files",
    "failure_threshold", "failure_cooldown", "permanent_cooldown",
}


def config_path(home) -> Path:
    return Path(home) / "config.json"


def load(home) -> dict:
    path = config_path(home)
    if not path.exists():
        config = {"agents": [dict(item) for item in DEFAULT_AGENTS]}
        save(home, config)
        return config
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailError("invalid_config", f"{path} 不是合法 JSON：{exc}") from exc
    entries = config.get("agents") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        raise MailError("invalid_config", f'{path} 需要形如 {{"agents": [...]}}')
    agents: list[dict] = []
    seen: set[str] = set()
    for item in entries:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        try:
            name = normalize_name(str(item.get("name") or ""))
        except MailError:
            continue
        if name in seen:
            continue
        seen.add(name)
        entry: dict = {"name": name}
        description = item.get("description")
        entry["description"] = str(description) if isinstance(description, str) else ""
        for key in _KNOWN_KEYS - {"name", "description"}:
            if key in item:
                entry[key] = item[key]
        agents.append(entry)
    return {"agents": agents}


def save(home, config: dict) -> None:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # indent / ensure_ascii / 末尾换行保持原样，只把落盘换成 atomic_replace。
    text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    atomic_replace(path, text.encode("utf-8"))


def _read_document(home) -> dict:
    path = config_path(home)
    if not path.exists():
        load(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailError("invalid_config", f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise MailError("invalid_config", f'{path} 需要形如 {{"agents": [...]}}')
    return raw


def federation_settings(home) -> dict:
    """读 node / peers / outbox_dir。不创建 config.json。

    node 会去掉空白并 lower；空和 null 变成 None。load() 仍然只返回 agents。
    """
    path = config_path(home)
    if not path.exists():
        return {"node": None, "peers": [], "outbox_dir": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailError("invalid_config", f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise MailError("invalid_config", f'{path} 需要形如 {{"agents": [...]}}')
    peers = raw.get("peers")
    return {
        "node": _federation_node(raw.get("node")),
        "peers": [] if peers is None else peers,
        "outbox_dir": _federation_path(raw.get("outbox_dir")),
    }


def _federation_node(value):
    if isinstance(value, str):
        text = value.strip().lower()
        return text or None
    return value


def _federation_path(value):
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def seed_pairs(home) -> list[tuple[str, str, str]]:
    return [
        (item["name"], item.get("description", ""), str(item.get("role") or ""))
        for item in load(home)["agents"]
    ]


def entry(home, name: str) -> dict | None:
    name = normalize_name(name)
    for item in load(home)["agents"]:
        if item["name"] == name:
            return item
    return None


def coordinator(home) -> str | None:
    for item in load(home)["agents"]:
        if str(item.get("role") or "") == "coordinator":
            return item["name"]
    return None


def _raw_agent_name(item) -> str | None:
    """从原始条目（字符串或对象）取出归一化名字，取不到返回 None。"""
    if isinstance(item, str):
        raw = item
    elif isinstance(item, dict):
        raw = str(item.get("name") or "")
    else:
        return None
    try:
        return normalize_name(raw)
    except MailError:
        return None


def _entry_view(target: dict) -> dict:
    """把补丁后的原始条目归一化成调用方要用的视图（name + description + 已知可选字段）。"""
    view: dict = {"name": target["name"], "description": str(target.get("description") or "")}
    for key in _KNOWN_KEYS - {"name", "description"}:
        if key in target:
            view[key] = target[key]
    return view


def add(home, name: str, description: str = "", role: str = "", **extra) -> dict:
    """C-14：按 name 对原始 config 做最小补丁，不整表归一化回写，保留其它条目的未知字段。"""
    name = normalize_name(name)
    raw = _read_document(home)
    entries = raw.get("agents")
    if not isinstance(entries, list):
        entries = []
    patch = {key: value for key, value in extra.items() if value not in (None, "")}
    if role:
        patch["role"] = role
    target: dict | None = None
    for index, item in enumerate(entries):
        if _raw_agent_name(item) != name:
            continue
        if isinstance(item, dict):
            target = item
        else:
            # 字符串条目升级为对象，仅补本次要写的字段，其余保持默认。
            target = {"name": name}
            entries[index] = target
        break
    if target is None:
        target = {"name": name, "description": description or ""}
        target.update(patch)
        entries.append(target)
    else:
        if description:
            target["description"] = description
        target.update(patch)
    raw["agents"] = entries
    save(home, raw)
    return _entry_view(target)


def remove(home, name: str) -> bool:
    """C-14：按 name 从原始 config 删除，保留其余条目的未知字段。"""
    name = normalize_name(name)
    raw = _read_document(home)
    entries = raw.get("agents")
    if not isinstance(entries, list):
        return False
    kept = [item for item in entries if _raw_agent_name(item) != name]
    if len(kept) == len(entries):
        return False
    raw["agents"] = kept
    save(home, raw)
    return True
