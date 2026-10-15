"""CLI 和 MCP 共用的 HTTP 客户端。"""

import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from infocenter.paths import ROOT, home_dir
from infocenter.server import discover_url, healthy, owned_url, read_meta
from infocenter.store import MailError

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
    target = url.rstrip("/") + path
    if query:
        from urllib.parse import urlencode

        pairs = {key: value for key, value in query.items() if value is not None}
        if pairs:
            target += "?" + urlencode(pairs)
    send_body = body
    send_pid = pid
    if window_pid_suppressed():
        send_pid = None
        if isinstance(send_body, dict) and "pid" in send_body:
            send_body = {key: value for key, value in send_body.items() if key != "pid"}
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


def call(explicit, agent: str, method: str, path: str, body=None, query=None, home=None, window: str | None = None, pid: int | None = None):
    url = ensure_server(explicit, home)
    try:
        return request(url, method, path, agent, body, query, window=window, pid=pid)
    except ApiError as exc:
        raise MailError(exc.code, str(exc), exc.status) from exc


def _window_header(window: str | None) -> str:
    """显式窗口号优先。没传时用 INFOCENTER_WINDOW；都没有则匿名，不发 X-Window。"""
    if window is None:
        window = os.environ.get("INFOCENTER_WINDOW", "")
    return str(window or "").strip()


def running_meta(home=None):
    return read_meta(home or home_dir())
