"""stdio MCP。身份由启动参数固定，工具本身只调用 HTTP。"""

import json
import sys
import threading
import time

from infocenter import __version__
from infocenter.client import ApiError, ensure_server, request

PROTOCOL = "2024-11-05"
# None 直到读到第一条消息。现行客户端（OpenCode、Cursor、Grok）用换行 JSON；
# 测试和旧客户端用 LSP 式 Content-Length。回包跟客户端走。
_mode: str | None = None
_WRITE_LOCK = threading.Lock()


def run_mcp(agent: str | None, explicit_url, home, notify: bool = True) -> None:
    url = ensure_server(explicit_url, home)
    if agent:
        request(url, "PUT", f"/v1/agents/{agent}", agent, {})
    watch = None
    while True:
        message = _read()
        if message is None:
            return
        response = _dispatch(message, url, agent, watch)
        if response is not None:
            _write(response)
        if watch is None and notify and agent and _mode is not None:
            # 首帧回包后才启动：通知的帧格式要跟客户端一致
            watch = _Watch(url, agent)
            watch.start()


def _dispatch(message: dict, url: str, locked: str | None, watch):
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or PROTOCOL
        if not str(version).startswith("202"):
            version = PROTOCOL
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}, "logging": {}},
            "serverInfo": {"name": "infocenter", "version": __version__},
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": _tools(locked)})
    if method == "tools/call":
        params = message.get("params") or {}
        return _result(msg_id, _call_tool(url, locked, params.get("name"), params.get("arguments") or {}, watch))
    return _error(msg_id, -32601, f"未知方法 {method}")


class _Watch:
    """后台长轮询 /v1/events。新信到达时推 logging 通知，提醒 agent 读信回帖。

    首轮 since=0：启动前积压的未读也会立刻提醒一次。
    """

    def __init__(self, url: str, agent: str):
        self.url = url
        self.agent = agent
        self.unread = 0
        self.cursor = 0

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"ic-watch-{self.agent}", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                payload = request(
                    self.url,
                    "GET",
                    "/v1/events",
                    self.agent,
                    query={"since": str(self.cursor), "wait": "25", "limit": "10"},
                    timeout=40,
                )
                self.unread = int(payload.get("unread_total") or 0)
                self.cursor = max(self.cursor, int(payload.get("cursor") or 0))
                fresh = payload.get("new") or []
            except (ApiError, SystemExit, OSError, ValueError, KeyError):
                time.sleep(2)
                continue
            if not fresh:
                continue
            latest = fresh[-1]
            extra = f" 等 {len(fresh)} 封" if len(fresh) > 1 else ""
            workspace = latest.get("workspace")
            where = f"（workspace：{workspace}）" if workspace else ""
            _write({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {
                    "level": "warning",
                    "logger": "infocenter",
                    "data": (
                        f"📬 信息中心：{self.unread} 封未读。最新一封来自 {latest.get('from')}，"
                        f"《{latest.get('subject')}》{extra}{where}。请调用 ic_inbox 阅读，并用 ic_reply 回复。"
                    ),
                },
            })


def _tools(locked: str | None) -> list[dict]:
    who = f"你的身份固定为 {locked}，不要在参数里换人。" if locked else "每次调用都要带 agent，那是你的名字。"
    agent_prop = {} if locked else {
        "agent": {"type": "string", "description": "你的 agent 名字"},
    }
    required_agent = [] if locked else ["agent"]

    def schema(properties, required):
        return {
            "type": "object",
            "properties": {**agent_prop, **properties},
            "required": required_agent + required,
            "additionalProperties": False,
        }

    return [
        {
            "name": "ic_agents",
            "description": "列出信息中心里的 agent：角色（role）、未读数（unread）、各自在做什么（activity）。" + who,
            "inputSchema": schema({}, []),
        },
        {
            "name": "ic_activity",
            "description": "自报当前状态（一句话：你在做什么）。开始/切换/完成任务时调用；coordinator 靠它调配资源，不用发信问询。" + who,
            "inputSchema": schema({
                "activity": {"type": "string", "description": "一句话状态，如：在重构 store.py 索引，已完成一半"},
            }, ["activity"]),
        },
        {
            "name": "ic_inbox",
            "description": "查看自己的收件箱，不签收。会话开始时先调用一次。默认只列未读。" + who,
            "inputSchema": schema({
                "status": {"type": "string", "enum": ["unread", "read", "archived", "all"]},
                "limit": {"type": "integer"},
                "full": {"type": "boolean", "description": "为 true 时带正文"},
            }, []),
        },
        {
            "name": "ic_peek",
            "description": "看一封信的正文，不签收。" + who,
            "inputSchema": schema({"id": {"type": "string"}}, ["id"]),
        },
        {
            "name": "ic_read",
            "description": "读取一封信，并把自己的那一份标成已读。别人的副本不受影响。" + who,
            "inputSchema": schema({"id": {"type": "string"}}, ["id"]),
        },
        {
            "name": "ic_send",
            "description": "给别的 agent 留言。to 是名字，多个用逗号分隔；* 表示除自己以外的所有人。" + who,
            "inputSchema": schema({
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "priority": {"type": "string", "enum": ["normal", "high"]},
                "workspace": {"type": "string", "description": "这封信关于哪个目录，可选"},
                "reply_to": {"type": "string"},
            }, ["to", "subject", "body"]),
        },
        {
            "name": "ic_reply",
            "description": "回复一封信，沿用原来的线程。默认回给发信人。" + who,
            "inputSchema": schema({
                "id": {"type": "string"},
                "body": {"type": "string"},
                "subject": {"type": "string"},
                "to": {"type": "string"},
            }, ["id", "body"]),
        },
        {
            "name": "ic_thread",
            "description": "查看一个线程里你参与过的回合。看不到你没参与的私信。" + who,
            "inputSchema": schema({"id": {"type": "string", "description": "thread_id"}}, ["id"]),
        },
        {
            "name": "ic_archive",
            "description": "把自己的那一份归档。" + who,
            "inputSchema": schema({"id": {"type": "string"}}, ["id"]),
        },
    ]


def _call_tool(url: str, locked: str | None, name: str, arguments: dict, watch=None) -> dict:
    if not isinstance(arguments, dict):
        return _tool_text("参数必须是对象", True)
    agent = locked or str(arguments.get("agent") or "").strip()
    if not agent:
        return _tool_text("缺少 agent", True)
    try:
        if name == "ic_agents":
            payload = request(url, "GET", "/v1/agents", agent)
        elif name == "ic_activity":
            payload = request(url, "PUT", f"/v1/agents/{agent}", agent, {"activity": arguments.get("activity") or ""})
        elif name == "ic_inbox":
            payload = request(url, "GET", "/v1/inbox", agent, query={
                "status": arguments.get("status") or "unread",
                "limit": str(arguments.get("limit") or 20),
                "full": "1" if arguments.get("full") else "0",
            })
        elif name == "ic_peek":
            payload = request(url, "GET", f"/v1/messages/{arguments['id']}", agent)
        elif name == "ic_read":
            payload = request(url, "POST", f"/v1/messages/{arguments['id']}/read", agent, {})
        elif name == "ic_archive":
            payload = request(url, "POST", f"/v1/messages/{arguments['id']}/archive", agent, {})
        elif name == "ic_send":
            payload = request(url, "POST", "/v1/messages", agent, {
                "to": arguments.get("to"),
                "subject": arguments.get("subject"),
                "body": arguments.get("body"),
                "priority": arguments.get("priority") or "normal",
                "workspace": arguments.get("workspace"),
                "reply_to": arguments.get("reply_to"),
            })
        elif name == "ic_reply":
            body = {"body": arguments.get("body")}
            if arguments.get("subject"):
                body["subject"] = arguments["subject"]
            if arguments.get("to"):
                body["to"] = arguments["to"]
            payload = request(url, "POST", f"/v1/messages/{arguments['id']}/reply", agent, body)
        elif name == "ic_thread":
            payload = request(url, "GET", f"/v1/threads/{arguments['id']}", agent)
        else:
            return _tool_text(f"未知工具 {name}", True)
    except ApiError as exc:
        return _tool_text(f"{exc.code}: {exc}", True)
    except KeyError as exc:
        return _tool_text(f"缺少参数 {exc.args[0]}", True)
    except SystemExit as exc:
        return _tool_text(str(exc), True)
    if watch is not None:
        if name == "ic_inbox" and (arguments.get("status") or "unread") == "unread":
            watch.unread = int(payload.get("count") or 0)
        elif name in {"ic_read", "ic_archive"}:
            watch.unread = max(0, watch.unread - 1)
        if watch.unread > 0 and name != "ic_inbox":
            return _tool_text(
                json.dumps(payload, ensure_ascii=False, indent=2)
                + f"\n\n提醒：你还有 {watch.unread} 封未读留言，请调用 ic_inbox 阅读，并用 ic_reply 回复。",
                False,
            )
    return _tool_text(json.dumps(payload, ensure_ascii=False, indent=2), False)


def _tool_text(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _read():
    global _mode
    if _mode == "line":
        return _read_json_line()
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    if _mode is None:
        stripped = line.strip()
        if not stripped:
            return _read()
        if stripped[:1] in (b"{", b"["):
            _mode = "line"
            return json.loads(stripped.decode("utf-8"))
        _mode = "lsp"
    return _finish_lsp(line)


def _read_json_line():
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        return json.loads(stripped.decode("utf-8"))


def _finish_lsp(first_line: bytes):
    headers = {}
    pending = first_line
    while True:
        line = pending if pending is not None else sys.stdin.buffer.readline()
        pending = None
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if b":" in line:
            key, value = line.decode("ascii", "replace").split(":", 1)
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length < 0:
        return None
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def _write(message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    with _WRITE_LOCK:
        if _mode == "lsp":
            sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
            sys.stdout.buffer.write(data)
        else:
            sys.stdout.buffer.write(data + b"\n")
        sys.stdout.buffer.flush()
