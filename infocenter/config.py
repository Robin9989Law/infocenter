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
"""

import json
import os
from pathlib import Path

from infocenter.store import MailError, normalize_name

DEFAULT_AGENTS = (
    {"name": "grok", "description": "Grok"},
    {"name": "kimi", "description": "Kimi Code"},
    {"name": "opencode", "description": "OpenCode"},
    {"name": "cursor", "description": "Cursor"},
    {"name": "human", "description": "用户"},
)

_KNOWN_KEYS = {"name", "description", "role", "launch", "app", "headless", "poll"}


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
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def add(home, name: str, description: str = "", role: str = "", **extra) -> dict:
    name = normalize_name(name)
    config = load(home)
    for item in config["agents"]:
        if item["name"] == name:
            if description:
                item["description"] = description
            if role:
                item["role"] = role
            for key, value in extra.items():
                if value not in (None, ""):
                    item[key] = value
            save(home, config)
            return item
    entry_dict = {"name": name, "description": description or ""}
    if role:
        entry_dict["role"] = role
    entry_dict.update({key: value for key, value in extra.items() if value not in (None, "")})
    config["agents"].append(entry_dict)
    save(home, config)
    return entry_dict


def remove(home, name: str) -> bool:
    name = normalize_name(name)
    config = load(home)
    kept = [item for item in config["agents"] if item["name"] != name]
    if len(kept) == len(config["agents"]):
        return False
    config["agents"] = kept
    save(home, config)
    return True
