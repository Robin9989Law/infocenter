"""HTTP 路由。只有这一层会调用 Store。"""

import json
from urllib.parse import parse_qs, unquote, urlparse

from infocenter import __version__, config
from infocenter.store import MailError, Store, normalize_name, normalize_window

MAX_BODY = 1_000_000

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
        "/v1": {"get": {"summary": "给新 agent 的入口说明", "responses": {"200": {"description": "how to join"}}}},
        "/v1/agents": {
            "get": {
                "summary": "列出 agent",
                "parameters": [{"$ref": "#/components/parameters/Agent"}],
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
            "post": {"summary": "签收自己的那一份（带 X-Window 时按线程软认领并立案）", "responses": {"200": {"description": "message"}}}
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
            "get": {"summary": "列出案卷（办事追踪）", "responses": {"200": {"description": "cases"}}},
            "post": {"summary": "立案（创建案卷）", "responses": {"201": {"description": "case"}}},
        },
        "/v1/cases/{id}": {
            "get": {"summary": "查看单个案卷与全部节点流水", "responses": {"200": {"description": "case"}}}
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
            "post": {"summary": "办结案卷（结案封档只读，释放归属）", "responses": {"200": {"description": "case"}}}
        },
        "/v1/windows": {
            "get": {"summary": "列出办公室（窗口）", "responses": {"200": {"description": "windows"}}},
            "post": {"summary": "登记或更新办公室（窗口）", "responses": {"200": {"description": "window"}}},
        },
        "/v1/projects": {
            "get": {"summary": "列出项目及案卷统计", "responses": {"200": {"description": "projects"}}},
            "post": {"summary": "设定项目地址（纳入管理）", "responses": {"201": {"description": "project"}}},
        },
    },
}


def handle(method: str, target: str, headers, raw: bytes, store: Store):
    parsed = urlparse(target)
    path = unquote(parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if method == "GET" and path == "/health":
        return 200, {"status": "ok", "version": __version__}
    if method == "GET" and path == "/openapi.json":
        return 200, OPENAPI
    if method == "GET" and path == "/":
        return 200, _index()
    if method == "GET" and path == "/v1":
        return 200, _index()

    is_d_route = (
        path == "/v1/cases"
        or path.startswith("/v1/cases/")
        or path == "/v1/windows"
        or path.startswith("/v1/windows/")
        or path == "/v1/projects"
        or path.startswith("/v1/projects/")
    )
    _check_write_security(method, headers, raw, is_d_route or path in ("/v1/crew", "/v1/restart", "/v1/open"))
    body = _json_object(raw)

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
            # 守护要以项目目录为 cwd 启动；目录不存在会在 Popen 时抛 OSError → 500。
            # 在路由层先判成客户端错误。
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
        import shlex as _shlex
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
        parts = [
            "sleep 0.5",
            f'"{py}" -m infocenter crew down',
            f'"{py}" -m infocenter stop',
            f'"{py}" -m infocenter serve --detach',
            "sleep 1",
        ]
        for d in dirs:
            parts.append(f'"{py}" -m infocenter crew up --dir {_shlex.quote(d)}')
        script = "{ " + " ; ".join(parts) + " ; } >> " + _shlex.quote(str(log)) + " 2>&1"
        if body.get("dry_run"):
            return 200, {"ok": True, "dry_run": True, "projects": dirs, "log": str(log), "script": script}
        try:
            _subprocess.Popen(
                ["/bin/sh", "-c", script],
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
    window_id = _resolve_window(headers, query, body)
    req_pid = _resolve_pid(headers, body)
    raw_agent = headers.get("X-Agent") if headers is not None else None
    if is_d_route and (not raw_agent or not str(raw_agent).strip()):
        b_ag = _opt_str(body, "agent")
        if method == "GET":
            q_ag = query.get("agent", [None])[0]
            agent = normalize_name(b_ag or q_ag or "human")
        else:
            agent = normalize_name(b_ag or "human")
    else:
        agent = _agent_header(headers)
        if window_id is not None:
            _guard_window_agent(store, agent, window_id, req_pid)
        store.touch(agent, window_id=window_id, pid=req_pid)

    if method == "GET" and path == "/v1/agents":
        agents = store.list_agents(window_id=window_id)
        roster = {a.get("name") for a in config.load(store.path.parent).get("agents", [])}
        for a in agents:
            a["roster"] = a.get("name") in roster
        return 200, {"agents": agents}

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
        if name != agent and agent != "human":
            raise MailError("not_you", "只能移除你自己；用户请用 human 身份移除别人", 403)
        return 200, store.remove_agent(name)

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
            )
            return 201, message

    # ── D1: /v1/cases 路由 ────────────────────────────────────────────────
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
                limit=limit,
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
            )
            return 201, created

    if path.startswith("/v1/cases/"):
        rest = path[len("/v1/cases/") :]
        if method == "GET" and "/" not in rest and rest:
            return 200, store.get_case(rest)
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

    # ── D1: /v1/windows 路由 ──────────────────────────────────────────────
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

    # ── D1: /v1/projects 路由 ─────────────────────────────────────────────
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
            project = store.add_project(ws, name=_opt_str(body, "name"), archived=archived)
            return (201 if method == "POST" else 200), project

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
