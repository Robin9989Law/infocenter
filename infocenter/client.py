"""CLI 和 MCP 共用的 HTTP 客户端。"""

import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from infocenter.common import MailError
from infocenter.paths import ROOT, home_dir
from infocenter.server import discover_url, healthy, owned_url

# 守护注入给 headless 子进程。子进程仍继承窗口号（认领算宿主办公室），
# 但请求里的 pid（X-Pid 和 JSON 体的 pid）一律丢掉，避免短命进程改写宿主窗口 pid。
NO_PID_ENV = "INFOCENTER_NO_PID"


def window_pid_suppressed() -> bool:
    raw = os.environ.get(NO_PID_ENV, "").strip().lower()
    return raw in {"1", "true", "yes"}


class ApiError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {"error": "http_error", "message": str(payload)}
        super().__init__(self.payload.get("message") or self.payload.get("error") or "请求失败")

    @property
    def code(self) -> str:
        return str(self.payload.get("error") or "http_error")


def resolve_url(explicit: str | None, home=None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("INFOCENTER_URL", "").strip()
    if env:
        return env.rstrip("/")
    return discover_url(home or home_dir())


def explicit_url(explicit: str | None) -> str | None:
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("INFOCENTER_URL", "").strip()
    return env.rstrip("/") if env else None


def ensure_server(explicit: str | None = None, home=None) -> str:
    home = home or home_dir()
    fixed = explicit_url(explicit)
    if fixed:
        if not healthy(fixed):
            raise SystemExit(f"信息中心没有在 {fixed} 上应答")
        return fixed
    url = owned_url(home)
    if url and healthy(url):
        return url
    launcher = ROOT / "bin" / "ic"
    result = subprocess.run(
        [sys.executable, str(launcher), "serve", "--detach", "--home", str(home)],
        capture_output=True,
        text=True,
    )
    url = owned_url(home)
    for _ in range(50):
        if url and healthy(url):
            return url
        time.sleep(0.1)
        url = owned_url(home)
    detail = (result.stderr or result.stdout or "").strip()
    log = home / "server.log"
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="replace")[-500:]
        detail = (detail + "\n" + tail).strip()
    raise SystemExit("信息中心没有启动" + (f"\n{detail}" if detail else ""))


def _ascii_path(path: str) -> str:
    """路径里的非 ASCII 先百分号编码，请求行才是 ASCII。

    不编码的话，http.client 在 `_encode_request` 里用 ascii 编请求行，
    `agents remove 中文名` 这类按名取路径的命令会直接 UnicodeEncodeError，
    CLI 甩出一整段 traceback。斜杠保留。调用方给的是原文，百分号也编码，避免二次理解。
    """
    return quote(str(path), safe="/")


def request(
    url: str,
    method: str,
    path: str,
    agent: str,
    body: dict | None = None,
    query: dict | None = None,
    timeout: float = 30,
    window: str | None = None,
    pid: int | None = None,
):
    target = url.rstrip("/") + _ascii_path(path)
    if query:
        pairs = {key: value for key, value in query.items() if value is not None}
        if pairs:
            target += "?" + urlencode(pairs)
    send_body = body
    send_pid = pid
    if window_pid_suppressed():
        send_pid = None
        if isinstance(send_body, dict) and "pid" in send_body:
            send_body = {key: value for key, value in send_body.items() if key != "pid"}
    send_body = _attach_client_cwd(method, path, send_body)
    data = None if send_body is None else json.dumps(send_body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(target, data=data, method=method)
    req.add_header("X-Agent", agent)
    office = _window_header(window)
    if office:
        req.add_header("X-Window", office)
    # M2：显式给出进程号时带上 X-Pid，补齐「窗口触达路径上报 pid」的客户端一环。
    # INFOCENTER_NO_PID 时连调用方显式传入的 pid 也不送，见 window_pid_suppressed。
    if send_pid is not None:
        req.add_header("X-Pid", str(send_pid))
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        exc.close()
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上信息中心：{exc.reason}") from exc
    except OSError as exc:
        # TimeoutError、RemoteDisconnected 等都是 OSError，不是 URLError。
        # 不包住的话每个 CLI 命令都会裸 traceback，退出码还是 1。
        raise ApiError(0, {"error": "transport", "message": f"连不上信息中心：{exc}"}) from exc
    except http.client.HTTPException as exc:
        # N1：IncompleteRead/BadStatusLine 等只继承 HTTPException（不是 OSError），
        # 之前会逃逸成裸 traceback；这里一并包成 ApiError。
        raise ApiError(0, {"error": "transport", "message": f"连不上信息中心：{exc}"}) from exc
    except UnicodeEncodeError as exc:
        # 路径已百分号编码。还炸，多半是服务地址或身份名（请求头）里有非 ASCII。
        # 不把 http.client 的栈甩给用户。
        raise ApiError(
            0,
            {
                "error": "bad_request_target",
                "message": (
                    "请求没送出去：地址或请求头里还有 HTTP 不收的非 ASCII。"
                    "路径上的名字已经做过百分号编码；请检查服务地址和身份名。"
                    "没有改任何东西。"
                ),
            },
        ) from exc
    if not raw:
        payload = {}
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {"error": "invalid_json", "message": raw.decode("utf-8", "replace")}
    if status >= 400:
        raise ApiError(status, payload)
    return payload


def request_mail(
    url: str,
    method: str,
    path: str,
    agent: str,
    body=None,
    query=None,
    timeout: float = 30,
    window: str | None = None,
    pid: int | None = None,
):
    """在已经确定的 url 上发请求，把 ApiError 译成 MailError。不拉起服务。"""
    try:
        return request(
            url, method, path, agent, body, query,
            timeout=timeout, window=window, pid=pid,
        )
    except ApiError as exc:
        raise MailError(exc.code, str(exc), exc.status) from exc


def call(explicit, agent: str, method: str, path: str, body=None, query=None, home=None, window: str | None = None, pid: int | None = None):
    url = ensure_server(explicit, home)
    return request_mail(url, method, path, agent, body, query, window=window, pid=pid)


def _wants_client_cwd(path: str) -> bool:
    """哪些写请求要带上敲命令时的目录。

    发信/回信用它补 workspace。拿写锁只把它交给服务端写人话：
    相对路径仍按服务端目录解析，**不**拿这个字段改锁键。
    """
    bare = path.split("?", 1)[0]
    return bare == "/v1/messages" or bare.endswith("/reply") or bare == "/v1/locks"


def _attach_client_cwd(method: str, path: str, body):
    """发信、回信、拿写锁时带上客户端 cwd。服务端不读自己的进程目录来猜这是哪。

    调用方已经放了 client_cwd（含显式 null）就不动。其它路径不改请求体。
    """
    if method not in {"POST", "PUT"} or not isinstance(body, dict):
        return body
    if not _wants_client_cwd(path):
        return body
    if "client_cwd" in body:
        return body
    return {**body, "client_cwd": os.getcwd()}


def _window_header(window: str | None) -> str:
    """显式窗口号优先。没传时用 INFOCENTER_WINDOW；都没有则匿名，不发 X-Window。"""
    if window is None:
        window = os.environ.get("INFOCENTER_WINDOW", "")
    return str(window or "").strip()
