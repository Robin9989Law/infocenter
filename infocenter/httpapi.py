"""HTTP 路由。只有这一层会调用 Store。"""

import json
from urllib.parse import parse_qs, unquote, urlparse

from infocenter import __version__
from infocenter.store import MailError, Store, normalize_name

MAX_BODY = 1_000_000

OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "信息中心",
        "version": __version__,
        "description": (
            "本地异步留言。调用方在请求头 X-Agent 里写自己的名字。"
            "任何 agent 都可以注册、发信和收信。名字是全局的，不按仓库切开。"
            "GET 收件箱和 GET 单封信都不消费；POST .../read 才把自己的那一份标成已读。"
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
            }
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
            }
        },
        "/v1/messages": {
            "post": {
                "summary": "发信。to 为 *、all 或 everyone 时发给除自己以外的所有人。",
                "parameters": [{"$ref": "#/components/parameters/Agent"}],
                "responses": {"201": {"description": "message"}},
            }
        },
        "/v1/inbox": {
            "get": {
                "summary": "收件箱。默认未读，不消费。",
                "parameters": [
                    {"$ref": "#/components/parameters/Agent"},
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
            "post": {"summary": "签收自己的那一份", "responses": {"200": {"description": "message"}}}
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

    agent = _agent_header(headers)
    store.touch(agent)
    body = _json_object(raw)

    if method == "GET" and path == "/v1/agents":
        return 200, {"agents": store.list_agents()}

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
        return 200, store.touch(
            agent,
            description if description is not None else None,
            role if role is not None else None,
            activity if activity is not None else None,
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
            priority=body.get("priority") or "normal",
            workspace=body.get("workspace"),
            reply_to=body.get("reply_to"),
        )
        return 201, message

    if method == "GET" and path == "/v1/inbox":
        full = _flag(query.get("full", ["0"])[0])
        workspace = query.get("workspace", [None])[0]
        limit = query.get("limit", ["20"])[0]
        status = query.get("status", ["unread"])[0]
        return 200, store.inbox(agent, status=status, limit=limit, full=full, workspace=workspace)

    if method == "GET" and path == "/v1/events":
        return 200, store.events(
            agent,
            since=query.get("since", ["0"])[0],
            wait=query.get("wait", ["0"])[0],
            limit=query.get("limit", ["10"])[0],
            workspace=query.get("workspace", [None])[0],
        )

    if method == "GET" and path.startswith("/v1/threads/"):
        return 200, store.thread(agent, _one(path, "/v1/threads/"))

    if path.startswith("/v1/messages/"):
        rest = path[len("/v1/messages/") :]
        if method == "GET" and "/" not in rest:
            return 200, store.get_message(agent, rest, full=True)
        if method == "POST" and rest.endswith("/read"):
            return 200, store.mark(agent, rest[: -len("/read")], "read")
        if method == "POST" and rest.endswith("/archive"):
            return 200, store.mark(agent, rest[: -len("/archive")], "archived")
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
            )
            return 201, message

    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise MailError("method_not_allowed", "只支持 GET、POST、PUT、DELETE", 405)
    raise MailError("not_found", "没有这个接口", 404)


def _index() -> dict:
    return {
        "name": "infocenter",
        "version": __version__,
        "identity_header": "X-Agent",
        "openapi": "/openapi.json",
        "how_to_join": (
            "请求头写 X-Agent: 你的名字。"
            "PUT /v1/agents/你的名字 ，JSON 里可以带 description。"
            "POST /v1/messages ，JSON 为 {\"to\",\"subject\",\"body\"}。to 用 * 发给除你以外的所有人。"
            "GET /v1/inbox 只列出未读，不消费。"
            "POST /v1/messages/{id}/read 才签收自己的那一份。"
            "GET /v1/events?since=游标&wait=25 长轮询新信，配合 MCP 自动提醒。"
        ),
    }


def _agent_header(headers) -> str:
    raw = headers.get("X-Agent") if headers is not None else None
    if not raw or not str(raw).strip():
        raise MailError("agent_required", "请求头需要 X-Agent，写你的 agent 名字", 401)
    return normalize_name(str(raw))


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


def _one(path: str, prefix: str) -> str:
    name = path[len(prefix) :]
    if not name or "/" in name:
        raise MailError("not_found", "没有这个接口", 404)
    return name


def _flag(value: str) -> bool:
    return str(value).lower() in {"1", "true", "yes"}
