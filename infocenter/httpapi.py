"""HTTP 路由。只有这一层会调用 Store。"""

import json
from urllib.parse import parse_qs, unquote, urlparse

from infocenter import __version__, config
from infocenter.store import (
    HUMAN,
    LOCK_TTL,
    MailError,
    Store,
    beijing_time,
    is_person,
    iso_ago,
    normalize_name,
    normalize_window,
    utcnow,
)

MAX_BODY = 1_000_000


def _probe_payload(store: Store, minutes: float) -> dict:
    """**空转采样**（只读，不改任何状态）：把"触发"和"产出"并排摆出来。

    触发数从守护的运行记录里数（`crew.run_stats`），产出从库里数（`store.probe_counts`）。
    两个都要看：只看触发会把"每分钟 36 次、0 产出"看成"很忙"；只看产出又看不出它在原地踏步。
    """
    from infocenter import crew

    home = store.path.parent
    since = iso_ago(minutes)
    counts = store.probe_counts(since)
    runs = crew.run_stats(home, since)
    # 谁在冷却里（守护 meta）：冷却中不触发是**正常**的，别当成"死了"。
    cooldowns: dict[str, dict] = {}
    for state_file in sorted(crew.state_dir(home).glob("*.json")):
        try:
            meta = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not meta.get("cooldown_kind"):
            continue
        agent = str(meta.get("agent") or state_file.stem.split("--", 1)[0])
        cooldowns[agent] = {
            "kind": meta.get("cooldown_kind"),
            "until_ts": meta.get("cooldown_until_ts"),
        }
    alive = {row["agent"] for row in crew.running_daemons(home) if row.get("alive")}
    # 谁"本该有个守护在跑"（config 里配了 headless/launch）——没配的（例如 human、前台 coordinator），
    # 守护不在是正常的，别当成故障喊。
    try:
        roster = config.load(home).get("agents", []) or []
    except Exception:  # 配置读不到也不该让采样失败
        roster = []
    autonomous = {
        str(item.get("name") or "")
        for item in roster
        if str(item.get("headless") or "").strip() or str(item.get("launch") or "").strip()
    }
    agents: list[dict] = []
    for name in sorted(set(counts) | set(runs)):
        item = dict(
            counts.get(name)
            or {
                "agent": name,
                "traces": 0,
                "machinery": 0,
                "messages": 0,
                "case_nodes": 0,
                "activity": "",
                "activity_at": "",
            }
        )
        item.setdefault("produced", 0)
        run = runs.get(name) or {}
        item["triggers"] = int(run.get("triggers") or 0)
        item["outcomes"] = run.get("outcomes") or {}
        item["truncated"] = bool(run.get("truncated"))
        item["alive"] = name in alive
        item["autonomous"] = name in autonomous
        item["cooldown"] = cooldowns.get(name) or {}
        verdict, why = crew.idle_verdict(item["triggers"], item["produced"], item["outcomes"])
        item["verdict"] = verdict
        item["why"] = why
        agents.append(item)
    moment = utcnow()
    return {
        "at": moment,
        "at_beijing": beijing_time(moment),
        "minutes": minutes,
        "since": since,
        "agents": agents,
        "totals": {
            "triggers": sum(item["triggers"] for item in agents),
            "produced": sum(item["produced"] for item in agents),
            "machinery": sum(item["machinery"] for item in agents),
        },
    }

OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "信息中心",
        "version": __version__,
        "description": (
            "本地异步留言。调用方在请求头 X-Agent 里写自己的名字，可选带 X-Window 标识窗口（临时办公室）。"
            "任何 agent 都可以注册、发信和收信。名字是全局的，不按仓库切开。"
            "GET 收件箱和 GET 单封信都不消费；POST .../read 才把自己的那一份标成已读并认领。"
        ),
    },
    "components": {
        "parameters": {
            "Agent": {
                "name": "X-Agent",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": "调用方的 agent 名字，例如 grok、kimi、opencode、cursor、human。",
            },
            "Window": {
                "name": "X-Window",
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "可选窗口号（临时办公室编号，例如 w-3f2a91）。不传则保持单窗口兼容行为。",
            },
        }
    },
    "paths": {
        "/health": {"get": {"summary": "存活检查", "responses": {"200": {"description": "ok"}}}},
        "/openapi.json": {"get": {"summary": "这份说明", "responses": {"200": {"description": "OpenAPI"}}}},
        "/": {"get": {"summary": "与 GET /v1 同一份入口说明", "responses": {"200": {"description": "how to join"}}}},
        "/v1": {"get": {"summary": "给新 agent 的入口说明", "responses": {"200": {"description": "how to join"}}}},
        "/v1/agents": {
            "get": {
                "summary": "列出 agent",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"name": "probe_minutes", "in": "query", "schema": {"type": "number"}, "description": "给每家附上这段时间的空转采样"},
                ],
                "responses": {"200": {"description": "agents"}},
            }
        },
        "/v1/agents/{name}": {
            "put": {
                "summary": "注册或更新自己。body 可带 description、role、activity（一句话状态自报）",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"$ref": "#/components/parameters/Window"},
                    {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "agent"}},
            },
            "delete": {
                "summary": "从接入名单移除。只能删自己，human 身份可以删任何 agent",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "removed"}},
            },
        },
        "/v1/agents/{name}/messages": {
            "get": {
                "summary": "某个身份的往来信件（收发方向、对方、主题、状态）。name 是人时合并 manager / human 两名的往来，不改 deliveries",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "messages"}},
            }
        },
        "/v1/messages": {
            "post": {
                "summary": "发信。to 为 *、all 或 everyone 时发给除自己以外的所有人。",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"$ref": "#/components/parameters/Window"},
                ],
                "responses": {"201": {"description": "message"}},
            }
        },
        "/v1/inbox": {
            "get": {
                "summary": "收件箱。默认未读，不消费。",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"$ref": "#/components/parameters/Window"},
                    {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["unread", "read", "archived", "all"]}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "full", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "workspace", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "inbox"}},
            }
        },
        "/v1/events": {
            "get": {
                "summary": "等新信长轮询。since 是上次返回的 cursor，wait 是最多等待秒数；workspace 给定时只返回属于该项目范围的新信",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"$ref": "#/components/parameters/Window"},
                    {"name": "since", "in": "query", "schema": {"type": "integer"}},
                    {"name": "wait", "in": "query", "schema": {"type": "number"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "workspace", "in": "query", "schema": {"type": "string"}, "description": "只返回属于该项目范围的新信（全路径或目录名）"},
                ],
                "responses": {"200": {"description": "events"}},
            }
        },
        "/v1/messages/{id}": {
            "get": {"summary": "看一封信，不消费", "responses": {"200": {"description": "message"}}}
        },
        "/v1/messages/{id}/read": {
            "post": {
                "summary": "签收自己的那一份。未封卷线程带 X-Window 时按线程软认领并立案；已封卷线程只标已读，不认领、不动案卷",
                "responses": {"200": {"description": "message"}},
            }
        },
        "/v1/messages/{id}/archive": {
            "post": {"summary": "归档自己的那一份", "responses": {"200": {"description": "message"}}}
        },
        "/v1/messages/{id}/reply": {
            "post": {"summary": "回复。默认回给发信人。", "responses": {"201": {"description": "message"}}}
        },
        "/v1/threads/{id}": {
            "get": {"summary": "只返回调用方参与过的回合", "responses": {"200": {"description": "thread"}}}
        },
        "/v1/cases": {
            "get": {
                "summary": "列出案卷（办事追踪）。每条 unread_replies 按请求者自己算",
                "responses": {"200": {"description": "cases"}},
            },
            "post": {"summary": "立案（创建案卷）", "responses": {"201": {"description": "case"}}},
        },
        "/v1/cases/{id}": {
            "get": {
                "summary": "查看单个案卷与全部节点流水。unread_replies 按请求者自己算",
                "responses": {"200": {"description": "case"}},
            }
        },
        "/v1/cases/{id}/pause": {
            "post": {"summary": "暂停案卷（不封卷，保留归属）", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/resume": {
            "post": {"summary": "恢复案卷为在办", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/abort": {
            "post": {"summary": "中止案卷（封卷只读）", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/close": {
            "post": {
                "summary": "办结案卷（结案封档只读，释放归属）。没写怎样算完 409 no_acceptance；发给自己的未签收信 409 unread_replies",
                "responses": {"200": {"description": "case"}},
            }
        },
        "/v1/cases/{id}/acceptance": {
            "post": {
                "summary": "写案卷的「怎样算完」（owner 或本项目的 coordinator；空串 = 撤掉）",
                "responses": {"200": {"description": "case"}},
            }
        },
        "/v1/cases/{id}/reopen": {
            "post": {"summary": "重开已封卷案卷（状态回 open，节点只增不删）", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/claim": {
            "post": {"summary": "认领到当前窗口。query 或 body 的 force=1 可从其他窗口接手", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/transfer": {
            "post": {"summary": "转办。body.to_window 必填，可带 to_agent", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/nodes": {
            "post": {"summary": "写回一行节点。body.note 必填；可带 status、waiting_for", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/note": {
            "post": {"summary": "与 /nodes 相同（别名）", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/wbs": {
            "post": {"summary": "打 WBS 路径。body.wbs_path 必填（空串 = 未归类）；仅 coordinator", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/milestone": {
            "post": {"summary": "加一条里程碑。body.name 必填；仅 coordinator", "responses": {"200": {"description": "case"}}}
        },
        "/v1/cases/{id}/milestone-done": {
            "post": {"summary": "勾掉第 N 条里程碑。body.index 从 0 起；done=false 撤回；仅 coordinator", "responses": {"200": {"description": "case"}}}
        },
        "/v1/probe": {
            "get": {
                "summary": "空转采样（只读）：每家的 headless 触发数与产出并排看",
                "parameters": [
                    {"name": "minutes", "in": "query", "schema": {"type": "number"}, "description": "窗口（分钟，默认 60）"}
                ],
                "responses": {"200": {"description": "probe"}},
            }
        },
        "/v1/windows": {
            "get": {"summary": "列出办公室（窗口）", "responses": {"200": {"description": "windows"}}},
            "post": {"summary": "登记或更新办公室（窗口）", "responses": {"200": {"description": "window"}}},
            "put": {"summary": "登记或更新办公室（与 POST 相同）", "responses": {"200": {"description": "window"}}},
        },
        "/v1/windows/{id}": {
            "get": {"summary": "查看一个办公室", "responses": {"200": {"description": "window"}}},
            "put": {"summary": "更新办公室（workspace / activity / state）", "responses": {"200": {"description": "window"}}},
            "post": {"summary": "更新办公室（与 PUT 相同）", "responses": {"200": {"description": "window"}}},
            "delete": {"summary": "办公室离场", "responses": {"200": {"description": "window"}}},
        },
        "/v1/windows/{id}/leave": {
            "post": {"summary": "办公室离场，并封掉名下案卷", "responses": {"200": {"description": "window"}}}
        },
        "/v1/windows/{id}/exit": {
            "post": {"summary": "与 leave 相同（别名）", "responses": {"200": {"description": "window"}}}
        },
        "/v1/windows/{id}/stop": {
            "post": {"summary": "与 leave 相同（别名）", "responses": {"200": {"description": "window"}}}
        },
        "/v1/windows/{id}/abort": {
            "post": {"summary": "与 leave 相同（别名）", "responses": {"200": {"description": "window"}}}
        },
        "/v1/windows/{id}/pause": {
            "post": {"summary": "暂停办公室", "responses": {"200": {"description": "window"}}}
        },
        "/v1/windows/{id}/resume": {
            "post": {"summary": "恢复办公室", "responses": {"200": {"description": "window"}}}
        },
        "/v1/projects": {
            "get": {"summary": "列出项目及案卷统计", "responses": {"200": {"description": "projects"}}},
            "post": {"summary": "设定项目地址（纳入管理）", "responses": {"201": {"description": "project"}}},
            "put": {"summary": "更新项目地址或 coordinator（与 POST 同一处理）", "responses": {"200": {"description": "project"}}},
        },
        "/v1/activity": {
            "get": {
                "summary": "全局工作痕迹流",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}, "description": "默认 50"},
                ],
                "responses": {"200": {"description": "activity"}},
            }
        },
        "/v1/audit": {
            "get": {
                "summary": "留痕机器验证：哈希链重放是否与当前状态一致",
                "parameters": [
                    {"name": "task", "in": "query", "schema": {"type": "string"}},
                    {"name": "anchor_seq", "in": "query", "schema": {"type": "string"}, "description": "库外锚；与 anchor_hash 一起给才核对"},
                    {"name": "anchor_hash", "in": "query", "schema": {"type": "string"}},
                    {"name": "anchor_at", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "audit"}},
            }
        },
        "/v1/crew": {
            "get": {
                "summary": "收信守护状态",
                "parameters": [
                    {"name": "dir", "in": "query", "schema": {"type": "string"}, "description": "只看这个项目目录"},
                ],
                "responses": {"200": {"description": "crew status"}},
            },
            "post": {
                "summary": "起停收信守护。body.action 为 up 或 down；dir 必填；only 可选",
                "responses": {"200": {"description": "crew"}},
            },
        },
        "/v1/headless": {
            "get": {
                "summary": "在跑的 headless",
                "parameters": [
                    {"name": "dir", "in": "query", "schema": {"type": "string"}, "description": "只看这个项目目录"},
                ],
                "responses": {"200": {"description": "headless"}},
            }
        },
        "/v1/headless/runs": {
            "get": {
                "summary": "headless 运行上下文（提示词与输出尾部）",
                "parameters": [
                    {"name": "dir", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}, "description": "默认 30"},
                ],
                "responses": {"200": {"description": "runs"}},
            }
        },
        "/v1/daemons": {
            "get": {"summary": "守护进程实况", "responses": {"200": {"description": "daemons"}}}
        },
        "/v1/restart": {
            "post": {
                "summary": "一键重启服务端与在跑的项目守护。body.dry_run=true 只预览脚本",
                "responses": {"200": {"description": "restart"}},
            }
        },
        "/v1/open": {
            "post": {
                "summary": "在访达 / 文件管理器里打开已登记的项目目录。body.path（或 dir）必填；未登记 403",
                "responses": {"200": {"description": "opened"}},
            }
        },
        "/v1/tasks": {
            "get": {
                "summary": "列出任务队列",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"name": "owner", "in": "query", "schema": {"type": "string"}},
                    {"name": "workspace", "in": "query", "schema": {"type": "string"}},
                    {"name": "status", "in": "query", "schema": {"type": "string"}},
                    {"name": "all", "in": "query", "schema": {"type": "boolean"}, "description": "连已被取代的也列出"},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "tasks"}},
            },
            "post": {
                "summary": "建任务。title、workspace 必填；仅 coordinator",
                "parameters": [{"$ref": "#/components/parameters/Agent"}],
                "responses": {"201": {"description": "task"}},
            },
        },
        "/v1/tasks/{id}": {
            "get": {"summary": "任务详情，含完整 supersedes 链", "responses": {"200": {"description": "task"}}}
        },
        "/v1/tasks/{id}/start": {
            "post": {"summary": "开工", "responses": {"200": {"description": "task"}}}
        },
        "/v1/tasks/{id}/status": {
            "post": {"summary": "改状态。body.status；blocked 带 reason，done 或 stopped 带 handoff", "responses": {"200": {"description": "task"}}}
        },
        "/v1/tasks/{id}/update": {
            "post": {"summary": "改规格（title / owner / deps / acceptance / case_id / priority）", "responses": {"200": {"description": "task"}}}
        },
        "/v1/tasks/{id}/bump": {
            "post": {"summary": "调优先级。body.by 默认 1", "responses": {"200": {"description": "task"}}}
        },
        "/v1/locks": {
            "get": {"summary": "列出写锁", "responses": {"200": {"description": "locks"}}},
            "post": {
                "summary": "拿写锁。body.paths 必填；可带 ttl、task_id、note、skip_locked、force、stay、client_cwd。相对路径仍收下，响应 path_hint 说明按服务端目录解析（不拒绝）。被他人锁着返回 409；自己的锁盘上没动再拿返回 409 no_progress",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
                    {"$ref": "#/components/parameters/Window"},
                ],
                "responses": {"201": {"description": "locks"}},
            },
        },
        "/v1/locks/release": {
            "post": {"summary": "放写锁。paths 不填＝放自己的全部；force 仅 coordinator", "responses": {"200": {"description": "locks"}}}
        },
        "/v1/locks/queue": {
            "get": {
                "summary": "列出拿不到钥匙时记下的改动",
                "parameters": [{"name": "path", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "queue"}},
            },
            "post": {
                "summary": "记下要改的路径。body.path 必填；可带 note、patch、task_id",
                "responses": {"201": {"description": "queued"}},
            },
        },
        "/v1/locks/queue/merge": {
            "post": {"summary": "合并排队的改动（要拿着该路径的钥匙）。body.path 必填；ids 可选", "responses": {"200": {"description": "merged"}}}
        },
        "/v1/guard": {
            "post": {
                "summary": "提交闸报告。body.paths 必填；require_mine 默认 true。始终 200，由调用方看 ok",
                "responses": {"200": {"description": "guard"}},
            }
        },
    },
}


def _route_ops(method: str, path: str, headers, query: dict, body: dict, store: Store):
    if path == "/v1/crew":
        from pathlib import Path as _Path
        from infocenter import crew as _crew

        home = store.path.parent
        host = (headers.get("Host") if headers is not None else None) or "127.0.0.1:8787"
        url = "http://" + str(host)
        if method == "GET":
            q_dir = query.get("dir", [None])[0]
            return 200, _crew.status(home, str(_Path(q_dir).expanduser().resolve()) if q_dir else None)
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 GET / POST", 405)
        action = (_opt_str(body, "action") or "").strip()
        project = _opt_str(body, "dir")
        only = _opt_str(body, "only") or ""
        if not project:
            raise MailError("invalid_dir", "需要 dir（项目目录）", 400)
        project_path = str(_Path(project).expanduser().resolve())
        if action == "up" and not _Path(project_path).is_dir():
            raise MailError("invalid_dir", f"项目目录不存在：{project_path}", 400)
        if action == "up":
            return 200, _crew.up(home, url, project_path, only)
        if action == "down":
            return 200, _crew.down(home, project_path, only)
        raise MailError("invalid_action", "action 需为 up 或 down", 400)

    if path == "/v1/activity":
        if method != "GET":
            raise MailError("method_not_allowed", "只支持 GET", 405)
        try:
            limit = int(query.get("limit", ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        return 200, {"activity": store.recent_activity(limit)}

    if path == "/v1/headless":
        if method != "GET":
            raise MailError("method_not_allowed", "只支持 GET", 405)
        from pathlib import Path as _Path
        from infocenter import crew as _crew2

        q_dir = query.get("dir", [None])[0]
        project = str(_Path(q_dir).expanduser().resolve()) if q_dir else None
        return 200, {"headless": _crew2.running_headless(store.path.parent, project)}

    if path == "/v1/headless/runs":
        if method != "GET":
            raise MailError("method_not_allowed", "只支持 GET", 405)
        from pathlib import Path as _Path2
        from infocenter import crew as _crew3

        q_dir = query.get("dir", [None])[0]
        project = str(_Path2(q_dir).expanduser().resolve()) if q_dir else None
        try:
            limit = int(query.get("limit", ["30"])[0])
        except (TypeError, ValueError):
            limit = 30
        return 200, {"runs": _crew3.headless_runs(store.path.parent, project, limit)}

    if path == "/v1/daemons":
        if method != "GET":
            raise MailError("method_not_allowed", "只支持 GET", 405)
        from infocenter import crew as _crew4

        return 200, {"daemons": _crew4.running_daemons(store.path.parent)}

    if path == "/v1/restart":
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 POST", 405)
        import os as _os
        import subprocess as _subprocess
        import sys as _sys
        from infocenter import __file__ as _initfile
        from infocenter import crew as _crew5

        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(_initfile)))
        env = _os.environ.copy()
        pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = root + (_os.pathsep + pp if pp else "")
        dirs = sorted({d.get("dir") for d in _crew5.running_daemons(store.path.parent) if d.get("dir")})
        py = _sys.executable
        log = store.path.parent / "restart.log"
        from infocenter.crew import _restart_script

        script = _restart_script(py, str(log), dirs)
        if body.get("dry_run"):
            return 200, {"ok": True, "dry_run": True, "projects": dirs, "log": str(log), "script": script}
        try:
            from infocenter.crew import _shell_argv

            _subprocess.Popen(
                _shell_argv(script),
                cwd=root,
                env=env,
                start_new_session=True,
                stdin=_subprocess.DEVNULL,
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MailError("restart_failed", f"无法启动重启脚本：{exc}", 500)
        return 200, {"ok": True, "projects": dirs, "log": str(log)}

    if path == "/v1/open":
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 POST", 405)
        import os as _os
        import subprocess as _subprocess
        import sys as _sys

        target = _opt_str(body, "path") or _opt_str(body, "dir")
        if not target:
            raise MailError("invalid_path", "需要 path", 400)
        d = _os.path.expanduser(target)
        if not _os.path.isdir(d):
            raise MailError("invalid_path", "目录不存在", 400)
        allowed: set[str] = set()
        for item in store.list_projects():
            ws = item.get("workspace") if isinstance(item, dict) else None
            if not isinstance(ws, str) or not ws.strip():
                continue
            expanded = _os.path.expanduser(ws.strip())
            allowed.add(_os.path.normpath(expanded))
            try:
                allowed.add(_os.path.normpath(_os.path.realpath(expanded)))
            except OSError:
                pass
        norm = _os.path.normpath(d)
        try:
            real = _os.path.normpath(_os.path.realpath(d))
        except OSError:
            real = norm
        if norm not in allowed and real not in allowed:
            raise MailError("not_a_project", "只能打开已登记的项目目录", 403)
        opener = "open" if _sys.platform == "darwin" else ("explorer" if _os.name == "nt" else "xdg-open")
        try:
            _subprocess.Popen(
                [opener, d],
                start_new_session=True,
                stdin=_subprocess.DEVNULL,
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MailError("open_failed", f"打开失败：{exc}", 500)
        return 200, {"ok": True, "path": d}
    return None


def _route_agents_and_projects(
    method: str, path: str, query: dict, body: dict, store: Store, agent: str, window_id: str | None, req_pid: int | None
):
    if method == "GET" and path == "/v1/agents":
        agents = store.list_agents(window_id=window_id)
        roster = {a.get("name") for a in config.load(store.path.parent).get("agents", [])}
        for a in agents:
            a["roster"] = a.get("name") in roster
        probe_minutes = query.get("probe_minutes", [None])[0]
        if probe_minutes:
            try:
                minutes_f = float(probe_minutes)
            except (TypeError, ValueError):
                minutes_f = 60.0
            probe = _probe_payload(store, minutes_f)
            by_agent = {item["agent"]: item for item in probe["agents"]}
            for a in agents:
                spot = by_agent.get(a.get("name") or "")
                if spot:
                    a["probe"] = {
                        "minutes": probe["minutes"],
                        "triggers": spot["triggers"],
                        "produced": spot["produced"],
                        "machinery": spot["machinery"],
                        "truncated": spot["truncated"],
                        "verdict": spot["verdict"],
                        "why": spot["why"],
                    }
        return 200, {"agents": agents}

    if method == "GET" and path == "/v1/probe":
        try:
            minutes = float(query.get("minutes", ["60"])[0])
        except (TypeError, ValueError):
            minutes = 60.0
        return 200, {"probe": _probe_payload(store, max(0.0, minutes))}

    if method == "GET" and path.startswith("/v1/agents/") and path.endswith("/messages"):
        name = normalize_name(path[len("/v1/agents/") : -len("/messages")].strip("/"))
        try:
            limit = int(query.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        return 200, {"agent": name, "messages": store.agent_messages(name, limit)}

    if method == "PUT" and path.startswith("/v1/agents/"):
        name = normalize_name(_one(path, "/v1/agents/"))
        if name != agent:
            raise MailError("not_you", "只能注册或更新自己的名字", 403)
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            raise MailError("invalid_description", "description 必须是字符串")
        role = body.get("role")
        if role is not None and not isinstance(role, str):
            raise MailError("invalid_role", "role 必须是字符串")
        activity = body.get("activity")
        if activity is not None and not isinstance(activity, str):
            raise MailError("invalid_activity", "activity 必须是字符串")
        workspace = body.get("workspace")
        if workspace is not None and not isinstance(workspace, str):
            raise MailError("invalid_workspace", "workspace 必须是字符串")
        return 200, store.touch(
            agent,
            description if description is not None else None,
            role if role is not None else None,
            activity if activity is not None else None,
            window_id=window_id,
            workspace=workspace if workspace is not None else None,
            pid=req_pid,
        )

    if method == "DELETE" and path.startswith("/v1/agents/"):
        name = normalize_name(_one(path, "/v1/agents/"))
        if name != agent and not is_person(agent):
            raise MailError("not_you", "只能移除你自己；用户请用 manager 身份移除别人（旧名 human 仍认）", 403)
        return 200, store.remove_agent(agent, name)

    if path == "/v1/projects":
        if method == "GET":
            projects = store.list_projects()
            return 200, {"projects": projects, "count": len(projects)}
        if method in {"POST", "PUT"}:
            ws = body.get("workspace") or body.get("dir") or body.get("path")
            if not isinstance(ws, str) or not ws.strip():
                raise MailError("invalid_workspace", "需要提供项目路径 workspace")
            archived = body.get("archived")
            if archived is not None and not isinstance(archived, bool):
                raise MailError("invalid_archived", "archived 必须是布尔值", 400)
            project = store.add_project(
                ws,
                name=_opt_str(body, "name"),
                archived=archived,
                coordinator=body.get("coordinator"),
                actor=agent,
            )
            return (201 if method == "POST" else 200), project
    return None


def _route_messages(
    method: str, path: str, query: dict, body: dict, store: Store, agent: str, window_id: str | None, req_pid: int | None
):
    if method == "POST" and path == "/v1/messages":
        message = store.send(
            agent,
            body.get("to"),
            _need_str(body, "subject"),
            _need_str(body, "body"),
            priority=body.get("priority"),
            workspace=body.get("workspace"),
            reply_to=body.get("reply_to"),
            local_node=_local_node(store),
            window_id=window_id,
            pid=req_pid,
            client_cwd=_opt_str(body, "client_cwd"),
        )
        return 201, message

    if method == "GET" and path == "/v1/inbox":
        full = _flag(query.get("full", ["0"])[0])
        workspace = query.get("workspace", [None])[0]
        limit = query.get("limit", ["20"])[0]
        status = query.get("status", ["unread"])[0]
        return 200, store.inbox(
            agent,
            status=status,
            limit=limit,
            full=full,
            workspace=workspace,
            window_id=window_id,
            pid=req_pid,
        )

    if method == "GET" and path == "/v1/events":
        return 200, store.events(
            agent,
            since=query.get("since", ["0"])[0],
            wait=query.get("wait", ["0"])[0],
            limit=query.get("limit", ["50"])[0],
            workspace=query.get("workspace", [None])[0],
            window_id=window_id,
            pid=req_pid,
        )

    if method == "GET" and path.startswith("/v1/threads/"):
        return 200, store.thread(agent, _one(path, "/v1/threads/"))

    if path.startswith("/v1/messages/"):
        rest = path[len("/v1/messages/") :]
        if method == "GET" and "/" not in rest:
            return 200, store.get_message(agent, rest, full=True)
        if method == "POST" and rest.endswith("/read"):
            force = _flag(query.get("force", ["0"])[0]) or bool(body.get("force"))
            return 200, store.mark(
                agent,
                rest[: -len("/read")],
                "read",
                window_id=window_id,
                force=force,
                pid=req_pid,
            )
        if method == "POST" and rest.endswith("/archive"):
            return 200, store.mark(
                agent,
                rest[: -len("/archive")],
                "archived",
                window_id=window_id,
                pid=req_pid,
            )
        if method == "POST" and rest.endswith("/reply"):
            message_id = rest[: -len("/reply")]
            subject = body.get("subject")
            if subject is not None and not isinstance(subject, str):
                raise MailError("invalid_subject", "主题必须是字符串")
            message = store.reply(
                agent,
                message_id,
                _need_str(body, "body"),
                subject=subject,
                to=body.get("to"),
                local_node=_local_node(store),
                window_id=window_id,
                pid=req_pid,
                client_cwd=_opt_str(body, "client_cwd"),
            )
            return 201, message
    return None


def _route_governance(
    method: str, path: str, query: dict, body: dict, store: Store, agent: str, window_id: str | None
):
    if path == "/v1/audit":
        if method != "GET":
            raise MailError("method_not_allowed", "只支持 GET", 405)
        task_id = query.get("task", [None])[0]
        anchor_seq = query.get("anchor_seq", [None])[0]
        anchor_hash = query.get("anchor_hash", [None])[0]
        anchor = (
            {"seq": anchor_seq, "hash": anchor_hash, "at": query.get("anchor_at", [""])[0]}
            if anchor_seq and anchor_hash
            else None
        )
        return 200, store.audit_events(task_id, anchor=anchor)

    if path == "/v1/locks/queue":
        if method == "GET":
            return 200, store.list_queue(query.get("path", [None])[0])
        if method == "POST":
            patch = _opt_str(body, "patch")
            if patch is None and _opt_str(body, "patch_file"):
                try:
                    patch = open(_opt_str(body, "patch_file"), encoding="utf-8").read()
                except OSError as exc:
                    raise MailError("invalid_patch", f"读不到补丁文件：{exc}", 400)
            return 201, store.queue_change(
                agent,
                path=_opt_str(body, "path") or "",
                note=_opt_str(body, "note"),
                patch=patch,
                task_id=_opt_str(body, "task_id") or _opt_str(body, "task"),
            )

    if path == "/v1/locks/queue/merge":
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 POST", 405)
        return 200, store.merge_queue(
            agent,
            path=_opt_str(body, "path") or "",
            ids=body.get("ids"),
            note=_opt_str(body, "note"),
        )

    if path == "/v1/locks":
        if method == "GET":
            return 200, store.lock_list()
        if method == "POST":
            paths = body.get("paths", body.get("path"))
            if paths is None:
                raise MailError("path_required", "至少要给一个路径（paths）", 400)
            return 201, store.lock_acquire(
                agent,
                paths,
                window_id=window_id,
                task_id=_opt_str(body, "task_id") or _opt_str(body, "task"),
                note=_opt_str(body, "note"),
                skip_locked=bool(body.get("skip_locked")),
                ttl=body.get("ttl", LOCK_TTL),
                force=_flag(query.get("force", ["0"])[0]) or bool(body.get("force")),
                dirty=body.get("dirty") if isinstance(body.get("dirty"), dict) else None,
                stay=_stay_flag(body.get("stay")),
                client_cwd=_opt_str(body, "client_cwd"),
            )

    if path == "/v1/guard":
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 POST", 405)
        paths = body.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise MailError("path_required", "需要 paths（要落地检查的路径列表）", 400)
        result = store.guard_changes(agent, list(paths), require_mine=body.get("require_mine", True))
        return 200, result

    if path == "/v1/locks/release":
        if method != "POST":
            raise MailError("method_not_allowed", "只支持 POST", 405)
        return 200, store.lock_release(
            agent,
            paths=body.get("paths", body.get("path")),
            force=_flag(query.get("force", ["0"])[0]) or bool(body.get("force")),
        )

    if path == "/v1/tasks":
        if method == "GET":
            return 200, store.task_list(
                agent,
                owner=query.get("owner", [None])[0],
                workspace=query.get("workspace", [None])[0],
                status=query.get("status", [None])[0],
                include_superseded=_flag(query.get("all", ["0"])[0]),
                limit=query.get("limit", ["50"])[0],
            )
        if method == "POST":
            title = _opt_str(body, "title")
            if not isinstance(title, str) or not title.strip():
                raise MailError("invalid_subject", "建任务需要提供 title")
            return 201, store.task_add(
                agent,
                title=title,
                owner=_opt_str(body, "owner"),
                workspace=_opt_str(body, "workspace"),
                deps=_opt_str(body, "deps"),
                acceptance=_opt_str(body, "acceptance"),
                case_id=_opt_str(body, "case_id"),
                supersedes=_opt_str(body, "supersedes"),
                priority=body.get("priority", 0),
            )

    if path.startswith("/v1/tasks/"):
        rest = path[len("/v1/tasks/") :]
        if method == "GET" and "/" not in rest and rest:
            return 200, store.task_get(agent, rest)
        if method == "POST" and "/" in rest:
            task_id, action = rest.rsplit("/", 1)
            if not task_id or "/" in task_id:
                raise MailError("not_found", "没有这个接口", 404)
            if action == "start":
                return 200, store.task_start(agent, task_id)
            if action == "status":
                return 200, store.task_set_status(
                    agent,
                    task_id,
                    _need_str(body, "status"),
                    reason=_opt_str(body, "reason"),
                    handoff=_opt_str(body, "handoff"),
                )
            if action == "update":
                fields = {
                    key: body[key]
                    for key in ("title", "owner", "deps", "acceptance", "case_id", "priority")
                    if key in body
                }
                return 200, store.task_update(agent, task_id, **fields)
            if action == "bump":
                return 200, store.task_bump(agent, task_id, by=body.get("by", 1))
            raise MailError("not_found", "没有这个接口", 404)
    return None


def _route_cases_and_windows(
    method: str, path: str, query: dict, body: dict, store: Store, agent: str, window_id: str | None, req_pid: int | None
):
    if path == "/v1/cases":
        if method == "GET":
            workspace = query.get("workspace", [None])[0]
            status = query.get("status", [None])[0]
            owner_win = query.get("owner_window", [None])[0] or query.get("window", [None])[0]
            owner_ag = query.get("owner_agent", [None])[0] or query.get("owner", [None])[0]
            limit = query.get("limit", ["100"])[0]
            cases = store.list_cases(
                workspace=workspace,
                status=status,
                owner_window=owner_win,
                owner_agent=owner_ag,
                waiting_for=query.get("waiting_for", [None])[0] or query.get("wait_for", [None])[0],
                limit=limit,
                wbs=query.get("wbs", [None])[0],
                viewer=agent,
            )
            return 200, {"cases": cases, "count": len(cases)}
        if method == "POST":
            title = _opt_str(body, "title") if "title" in body and body["title"] is not None else _opt_str(body, "subject")
            if not isinstance(title, str) or not title.strip():
                raise MailError("invalid_subject", "立案需要提供 title（或 subject）")
            owner_ag = _opt_str(body, "owner_agent") if "owner_agent" in body and body["owner_agent"] is not None else _opt_str(body, "owner")
            owner_win = (
                _opt_str(body, "owner_window")
                if "owner_window" in body and body["owner_window"] is not None
                else (_opt_str(body, "window_id") if "window_id" in body and body["window_id"] is not None else window_id)
            )
            tid = _opt_str(body, "thread_id") if "thread_id" in body and body["thread_id"] is not None else _opt_str(body, "id")
            created = store.create_case(
                agent=agent,
                title=title,
                workspace=_opt_str(body, "workspace"),
                owner_agent=owner_ag,
                owner_window=owner_win,
                status=_opt_str(body, "status"),
                note=_opt_str(body, "note"),
                thread_id=tid,
                to=body.get("to"),
                body=_opt_str(body, "body"),
                pid=req_pid,
                wbs_path=body.get("wbs_path", body.get("wbs")),
                milestones=body.get("milestones"),
                acceptance=_opt_str(body, "acceptance"),
            )
            return 201, created

    if path.startswith("/v1/cases/"):
        rest = path[len("/v1/cases/") :]
        if method == "GET" and "/" not in rest and rest:
            return 200, store.get_case(rest, viewer=agent)
        if method == "POST" and "/" in rest:
            case_id, action = rest.rsplit("/", 1)
            if not case_id or "/" in case_id:
                raise MailError("not_found", "没有这个接口", 404)
            note = _opt_str(body, "note") or _opt_str(body, "summary") or _opt_str(body, "reason")
            if action == "pause":
                interrupted = _flag(query.get("interrupted", ["0"])[0]) or bool(body.get("interrupted"))
                return 200, store.pause_case(case_id, agent=agent, window_id=window_id, note=note, interrupted=interrupted)
            if action == "resume":
                return 200, store.resume_case(case_id, agent=agent, window_id=window_id, note=note)
            if action == "abort":
                auto = _flag(query.get("auto", ["0"])[0]) or bool(body.get("auto"))
                return 200, store.abort_case(case_id, agent=agent, window_id=window_id, note=note, auto=auto)
            if action == "close":
                auto = _flag(query.get("auto", ["0"])[0]) or bool(body.get("auto"))
                return 200, store.close_case(case_id, agent=agent, window_id=window_id, note=note, auto=auto)
            if action == "reopen":
                return 200, store.reopen_case(case_id, agent=agent, window_id=window_id, note=note, pid=req_pid)
            if action == "claim":
                force = _flag(query.get("force", ["0"])[0]) or bool(body.get("force"))
                claim_win = window_id or _opt_str(body, "window_id") or _opt_str(body, "window")
                return 200, store.claim_case(case_id, agent=agent, window_id=claim_win, force=force, note=note, pid=req_pid)
            if action == "transfer":
                to_win = _opt_str(body, "to_window") or _opt_str(body, "window_id") or _opt_str(body, "to")
                if not isinstance(to_win, str) or not to_win.strip():
                    raise MailError("invalid_window", "transfer 需要提供目标窗口 to_window")
                return 200, store.transfer_case(
                    case_id,
                    agent=agent,
                    to_window=to_win,
                    to_agent=_opt_str(body, "to_agent") or _opt_str(body, "agent"),
                    window_id=window_id,
                    note=note,
                    pid=req_pid,
                )
            if action == "wbs":
                if "wbs_path" not in body and "wbs" not in body:
                    raise MailError("invalid_wbs", "需要提供 wbs_path（空串 = 未归类）", 400)
                return 200, store.case_wbs_set(
                    case_id,
                    agent=agent,
                    wbs_path=body.get("wbs_path", body.get("wbs")),
                    window_id=window_id,
                )
            if action == "acceptance":
                if "acceptance" not in body:
                    raise MailError("invalid_acceptance", "需要提供 acceptance（空串 = 撤掉）", 400)
                return 200, store.case_acceptance_set(
                    case_id,
                    agent=agent,
                    acceptance=body.get("acceptance"),
                    window_id=window_id,
                )
            if action == "milestone":
                return 200, store.case_milestone_add(
                    case_id,
                    agent=agent,
                    name=_opt_str(body, "name") or _opt_str(body, "milestone") or "",
                    owner=_opt_str(body, "owner"),
                    window_id=window_id,
                )
            if action == "milestone-done":
                raw = body.get("index", query.get("index", [None])[0])
                done = body.get("done", True)
                if isinstance(done, str):
                    done = _flag(done)
                return 200, store.case_milestone_done(
                    case_id,
                    agent=agent,
                    index=raw,
                    done=bool(done),
                    window_id=window_id,
                    note=note,
                )
            if action in {"nodes", "note"}:
                if not isinstance(note, str):
                    raise MailError("invalid_node", "需要提供单行节点内容 note")
                return 200, store.append_case_node(
                    case_id,
                    agent=agent,
                    note=note,
                    status=_opt_str(body, "status"),
                    window_id=window_id,
                    action=_opt_str(body, "action") or "note",
                    waiting_for=_opt_str(body, "waiting_for"),
                    auto=bool(body.get("auto")),
                    pid=req_pid,
                )

    if path == "/v1/windows":
        if method == "GET":
            ag = query.get("agent", [None])[0]
            ws = query.get("workspace", [None])[0]
            st = query.get("state", [None])[0]
            wins = store.list_windows(agent=ag, workspace=ws, state=st)
            return 200, {"windows": wins, "count": len(wins)}
        if method in {"POST", "PUT"}:
            wid = _opt_str(body, "window_id") or _opt_str(body, "id") or window_id
            if not wid:
                raise MailError("window_required", "需要提供 window_id（或请求头 X-Window）")
            win_agent = _opt_str(body, "agent") or agent
            return 200, store.touch_window(
                window_id=wid,
                agent=win_agent,
                workspace=_opt_str(body, "workspace"),
                activity=_opt_str(body, "activity"),
                state=_opt_str(body, "state"),
                pid=req_pid,
            )

    if path.startswith("/v1/windows/"):
        rest = path[len("/v1/windows/") :]
        if "/" not in rest and rest:
            if method == "GET":
                return 200, store.get_window(rest)
            if method in {"PUT", "POST"}:
                win_agent = _opt_str(body, "agent") or agent
                return 200, store.touch_window(
                    window_id=rest,
                    agent=win_agent,
                    workspace=_opt_str(body, "workspace"),
                    activity=_opt_str(body, "activity"),
                    state=_opt_str(body, "state"),
                    pid=req_pid,
                )
            if method == "DELETE":
                return 200, store.leave_window(rest, agent=agent)
        if method == "POST" and "/" in rest:
            wid, action = rest.rsplit("/", 1)
            if not wid or "/" in wid:
                raise MailError("not_found", "没有这个接口", 404)
            note = _opt_str(body, "note") or _opt_str(body, "reason")
            if action in {"leave", "exit", "stop", "abort"}:
                return 200, store.leave_window(wid, agent=agent, note=note, auto=True)
            if action == "pause":
                return 200, store.pause_window(wid, agent=agent, note=note)
            if action == "resume":
                return 200, store.resume_window(wid, agent=agent, note=note)
    return None


def handle(method: str, target: str, headers, raw: bytes, store: Store):
    parsed = urlparse(target)
    path = unquote(parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if method == "GET" and path == "/health":
        return 200, {"status": "ok", "version": __version__}
    if method == "GET" and path == "/openapi.json":
        return 200, OPENAPI
    if method == "GET" and path in ("/", "/v1"):
        return 200, _index()

    is_d_route = (
        path == "/v1/cases"
        or path.startswith("/v1/cases/")
        or path == "/v1/tasks"
        or path.startswith("/v1/tasks/")
        or path == "/v1/locks"
        or path.startswith("/v1/locks/")
        or path == "/v1/guard"
        or path == "/v1/windows"
        or path.startswith("/v1/windows/")
        or path == "/v1/projects"
        or path.startswith("/v1/projects/")
    )
    _check_write_security(method, headers, raw, is_d_route or path in ("/v1/crew", "/v1/restart", "/v1/open"))
    body = _json_object(raw)

    res = _route_ops(method, path, headers, query, body, store)
    if res is not None:
        return res

    window_id = _resolve_window(headers, query, body)
    req_pid = _resolve_pid(headers, body)
    raw_agent = headers.get("X-Agent") if headers is not None else None
    if is_d_route and (not raw_agent or not str(raw_agent).strip()):
        b_ag = _opt_str(body, "agent")
        if method == "GET":
            q_ag = query.get("agent", [None])[0]
            agent = normalize_name(b_ag or q_ag or HUMAN)
        else:
            agent = normalize_name(b_ag or HUMAN)
    else:
        agent = _agent_header(headers)
        if window_id is not None:
            _guard_window_agent(store, agent, window_id, req_pid)
        store.touch(agent, window_id=window_id, pid=req_pid)

    for router in (
        lambda: _route_agents_and_projects(method, path, query, body, store, agent, window_id, req_pid),
        lambda: _route_messages(method, path, query, body, store, agent, window_id, req_pid),
        lambda: _route_governance(method, path, query, body, store, agent, window_id),
        lambda: _route_cases_and_windows(method, path, query, body, store, agent, window_id, req_pid),
    ):
        res = router()
        if res is not None:
            return res

    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise MailError("method_not_allowed", "只支持 GET、POST、PUT、DELETE", 405)
    raise MailError("not_found", "没有这个接口", 404)


def _index() -> dict:
    return {
        "name": "infocenter",
        "version": __version__,
        "identity_header": "X-Agent",
        "window_header": "X-Window",
        "openapi": "/openapi.json",
        "how_to_join": (
            "请求头写 X-Agent: 你的名字（可选带 X-Window: 窗口号）。"
            "PUT /v1/agents/你的名字 ，JSON 里可以带 description。"
            "POST /v1/messages ，JSON 为 {\"to\",\"subject\",\"body\"}。to 用 * 发给除你以外的所有人。"
            "GET /v1/inbox 只列出未读，不消费。"
            "POST /v1/messages/{id}/read 才签收自己的那一份。"
            "GET /v1/events?since=游标&wait=25 长轮询新信，配合 MCP 自动提醒。"
        ),
    }


def _local_node(store: Store) -> str | None:
    try:
        node = config.federation_settings(store.path.parent).get("node")
    except Exception:
        return None
    return node if isinstance(node, str) and node else None


def _check_write_security(method: str, headers, raw: bytes, is_d_route: bool) -> None:
    """L1：状态变更端点统一防跨站（CSRF）：
    1) 校验 Sec-Fetch-Site（若存在，须为 same-origin 或 none）；
    2) 校验 Origin（若存在，拒绝 null 与跨源 Origin）；
    3) 校验 Content-Type（若存在，须为 application/json；无 X-Agent 的 D 路由写操作强制要求 application/json）。"""
    if method not in {"POST", "PUT", "DELETE"} or headers is None:
        return
    sfs = headers.get("Sec-Fetch-Site")
    if sfs is not None:
        sfs_val = str(sfs).strip().lower()
        if sfs_val and sfs_val not in {"same-origin", "none"}:
            raise MailError("csrf_rejected", "跨站请求被拒绝（Sec-Fetch-Site）", 403)
    origin = headers.get("Origin")
    if origin is not None:
        origin_val = str(origin).strip()
        if origin_val:
            if origin_val.lower() == "null":
                raise MailError("csrf_rejected", "跨站请求被拒绝（Origin: null）", 403)
            parsed_origin = urlparse(origin_val)
            origin_host = (parsed_origin.hostname or "").lower()
            req_host_raw = str(headers.get("Host") or "").strip()
            req_host = req_host_raw.split(":", 1)[0].strip().lower()
            loopbacks = {"127.0.0.1", "localhost", "::1"}
            same_host = bool(origin_host) and (
                origin_host == req_host
                or (origin_host in loopbacks and (not req_host or req_host in loopbacks))
            )
            if parsed_origin.scheme not in {"http", "https"} or not same_host:
                raise MailError("csrf_rejected", "跨站请求被拒绝（Origin 不匹配）", 403)
    raw_ct = headers.get("Content-Type")
    media_type = str(raw_ct).split(";", 1)[0].strip().lower() if raw_ct is not None else ""
    if media_type and media_type != "application/json":
        raise MailError("invalid_content_type", "写请求 Content-Type 必须是 application/json", 415)
    raw_agent = headers.get("X-Agent")
    if is_d_route and (not raw_agent or not str(raw_agent).strip()):
        if media_type != "application/json":
            raise MailError(
                "invalid_content_type",
                "无 X-Agent 的管理台写请求必须携带 Content-Type: application/json",
                415,
            )


def _agent_header(headers) -> str:
    raw = headers.get("X-Agent") if headers is not None else None
    if not raw or not str(raw).strip():
        raise MailError("agent_required", "请求头需要 X-Agent，写你的 agent 名字", 401)
    return normalize_name(str(raw))


def _resolve_window(headers, query: dict, body: dict) -> str | None:
    raw = headers.get("X-Window") if headers is not None else None
    if raw is not None and str(raw).strip():
        return normalize_window(str(raw))
    q_win = query.get("window", [None])[0] or query.get("window_id", [None])[0]
    if q_win is not None and str(q_win).strip():
        return normalize_window(str(q_win))
    if isinstance(body, dict):
        b_win = body["window"] if "window" in body and body["window"] is not None else body.get("window_id")
        if b_win is not None:
            return normalize_window(b_win)
    return None


def _json_object(raw: bytes) -> dict:
    if len(raw) > MAX_BODY:
        raise MailError("too_large", "请求体太大", 413)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MailError("invalid_json", "请求体不是 UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise MailError("invalid_json", "请求体必须是 JSON 对象")
    return data


def _need_str(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise MailError(f"invalid_{key}", f"{key} 必须是字符串")
    return value


def _guard_window_agent(store: Store, agent: str, window_id: str, pid: int | None = None) -> None:
    """D-8：请求头 X-Window 若指向他人的窗口，直接 409，防止冒用他人窗口号。

    H2：先把本次 pid 写回，再惰性对账，两者在同一事务里。旧顺序是 get_window
    （对账）在 touch（写 pid）之前；pid 已死时，这次重申自己会先封卷，随后才把窗口复活。
    """
    store.write_pid_then_reconcile(window_id, agent, pid)


def _opt_str(body: dict, key: str) -> str | None:
    """D-5：可选字符串字段入口统一类型校验，非字符串 → 400（而不是 AttributeError → 500）。"""
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError(f"invalid_{key}", f"{key} 必须是字符串")
    return value


def _resolve_pid(headers, body) -> int | None:
    """M2：窗口触达路径上的可选进程号——优先请求头 X-Pid，其次请求体 pid。"""
    raw = headers.get("X-Pid") if headers is not None else None
    if raw is not None and str(raw).strip():
        try:
            return int(str(raw).strip())
        except ValueError:
            raise MailError("invalid_pid", "X-Pid 必须是整数") from None
    if isinstance(body, dict) and body.get("pid") is not None:
        value = body.get("pid")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise MailError("invalid_pid", "pid 必须是整数")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise MailError("invalid_pid", "pid 必须是整数") from None
    return None


def _one(path: str, prefix: str) -> str:
    name = path[len(prefix) :]
    if not name or "/" in name:
        raise MailError("not_found", "没有这个接口", 404)
    return name


def _flag(value: str) -> bool:
    return str(value).lower() in {"1", "true", "yes"}


def _stay_flag(value) -> bool:
    """body.stay：JSON 布尔，或 1/true/yes。缺省＝普通续期。"""
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    return _flag(str(value))
