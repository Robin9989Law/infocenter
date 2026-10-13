"""CLI 和 MCP 共用的 HTTP 客户端。"""

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
):
    target = url.rstrip("/") + path
    if query:
        from urllib.parse import urlencode

        pairs = {key: value for key, value in query.items() if value is not None}
        if pairs:
            target += "?" + urlencode(pairs)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(target, data=data, method=method)
    req.add_header("X-Agent", agent)
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


def call(explicit, agent: str, method: str, path: str, body=None, query=None, home=None):
    url = ensure_server(explicit, home)
    try:
        return request(url, method, path, agent, body, query)
    except ApiError as exc:
        raise MailError(exc.code, str(exc), exc.status) from exc


def running_meta(home=None):
    return read_meta(home or home_dir())
