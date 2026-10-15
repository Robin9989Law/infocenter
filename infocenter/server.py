"""常驻 HTTP 服务。SQLite 只由这个进程打开。"""

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from infocenter import DEFAULT_HOST, DEFAULT_PORT, config
from infocenter.httpapi import MAX_BODY, handle
from infocenter.store import MailError, Store

_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

_LOCK = None


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._go()

        def do_POST(self):
            self._go()

        def do_PUT(self):
            self._go()

        def do_DELETE(self):
            self._go()

        def log_message(self, fmt, *args):
            if os.environ.get("INFOCENTER_QUIET"):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _go(self):
            # /ui 静态文件：直接返回，不走 JSON 路由
            if self.command == "GET" and self.path.startswith("/ui"):
                self._serve_ui()
                return
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not str(raw_len).strip():
                length = 0
            else:
                try:
                    length = int(raw_len)
                except ValueError:
                    self._json(400, {"error": "invalid_content_length", "message": "Content-Length 必须是非负整数"})
                    return
            if length < 0:
                self._json(400, {"error": "invalid_content_length", "message": "Content-Length 不能为负数"})
                return
            if length > MAX_BODY:
                self._json(413, {"error": "too_large", "message": "请求体太大"})
                return
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                status, payload = handle(self.command, self.path, self.headers, raw, store)
            except MailError as exc:
                status, payload = exc.status, {"error": exc.code, "message": exc.message}
            except Exception as exc:
                sys.stderr.write(f"infocenter: {exc}\n")
                status, payload = 500, {"error": "internal", "message": "服务器内部错误"}
            self._json(status, payload)
        def _serve_ui(self):
            # /ui → index.html；/ui/xxx → web/xxx
            from urllib.parse import unquote, urlsplit
            target = unquote(urlsplit(self.path).path)
            if target == "/ui" or target == "/ui/":
                rel = "index.html"
            elif target.startswith("/ui/"):
                rel = target[4:]
            else:
                self._text(404, "Not Found")
                return
            # 防止路径穿越
            if ".." in rel or rel.startswith("/"):
                self._text(400, "Bad Request")
                return
            full = os.path.join(_WEB_DIR, rel)
            if not os.path.isfile(full):
                self._text(404, "Not Found")
                return
            ext = os.path.splitext(full)[1].lower()
            ctype = _MIME.get(ext, "application/octet-stream")
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                self._text(500, "read error")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _text(self, status: int, text: str):
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def server_json(home):
    return home / "server.json"


def read_meta(home) -> dict | None:
    path = server_json(home)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def owned_url(home) -> str | None:
    """这个 home 自己的 server.json。没有记录就不算它的服务。"""
    meta = read_meta(home)
    if meta and meta.get("url"):
        return str(meta["url"]).rstrip("/")
    return None


def discover_url(home) -> str:
    return owned_url(home) or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def healthy(url: str, timeout: float = 0.4) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def claim(home):
    """返回 running，或在持有锁时返回 claimed。忙则抛错。"""
    global _LOCK
    home.mkdir(parents=True, exist_ok=True)
    url = owned_url(home)
    meta = read_meta(home)
    if url and meta and pid_alive(int(meta.get("pid") or 0)) and healthy(url):
        return "running", url
    lock_path = home / "server.lock"
    fp = open(lock_path, "a+")
    try:
        import fcntl

        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        for _ in range(50):
            url = owned_url(home)
            if url and healthy(url):
                return "running", url
            time.sleep(0.1)
        raise SystemExit("信息中心正在启动，但端口还没有应答")
    url = owned_url(home)
    if url and healthy(url):
        import fcntl

        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()
        return "running", url
    _LOCK = fp
    return "claimed", url


SWEEP_INTERVAL = 60.0


def _start_sweeper(store, interval: float = SWEEP_INTERVAL, stop_event=None) -> threading.Thread:
    """后台定时器：只跑**死流程回收**（过期锁 / 死进程窗口 / 僵尸认领）。

    为什么要有它（pi 2026-10-15 报的洞）：回收原来只在"别人 inbox 时顺手"触发，
    低活跃项目可能几天都不触发 ⇒ 僵尸认领把信对着团队隐藏很久。死流程该由机器自己兜。

    两个实现细节（都用血的教训换来的）：
      ① **用 `Event.wait` 而不是 `time.sleep`**：测试里会用 `patch.object(crew.time, "sleep")`
         打桩全局 `time.sleep`（idle guard 的假时钟就是这么做的）；若本线程调 `time.sleep`，
         就会被假时钟带着**空转冲刺**，把那条测试的时间轴冲飞（实测：心跳间隔被算成 10568s）。
      ② 给出 `stop_event`：调用方（测试 / 关服务）能干净地把它停掉。
    """
    stop = stop_event or threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                store.sweep()
            except Exception as exc:  # 后台兜底绝不能把服务搞崩
                sys.stderr.write(f"[sweep] 失败（忽略，下轮再来）：{exc}\n")

    thread = threading.Thread(target=loop, name="ic-sweep", daemon=True)
    thread.stop_event = stop  # 让调用方能停
    thread.start()
    return thread


def bind_and_serve(host: str, port: int, home, notify=None) -> int:
    state, url = claim(home)
    if state == "running":
        if notify is not None:
            os.write(notify, f"ok {url}\n".encode())
            os.close(notify)
            os._exit(0)
        print(url)
        return 0

    store = Store(home / "mailbox.db", seeds=config.seed_pairs(home))
    try:
        httpd = _Server((host, port), make_handler(store))
    except OSError as exc:
        message = f"err 无法监听 {host}:{port}：{exc}\n"
        if notify is not None:
            os.write(notify, message.encode())
            os.close(notify)
            os._exit(1)
        sys.stderr.write(message)
        return 1

    actual_port = httpd.server_address[1]
    advertise = DEFAULT_HOST if host in {"0.0.0.0", "::"} else host
    url = f"http://{advertise}:{actual_port}"
    meta = {"pid": os.getpid(), "host": host, "port": actual_port, "url": url}
    _write_meta(home, meta)

    def stop(signum, frame):
        server_json(home).unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if notify is not None:
        os.write(notify, f"ok {url}\n".encode())
        os.close(notify)
    else:
        print(url, flush=True)
    _start_sweeper(store)  # 死流程回收：过期锁 / 死进程窗口 / 僵尸认领（不靠人触发）
    httpd.serve_forever()
    return 0


def _write_meta(home, meta: dict) -> None:
    path = server_json(home)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, path)


def serve(host: str, port: int, home, detach: bool) -> int:
    home.mkdir(parents=True, exist_ok=True)
    if not detach:
        return bind_and_serve(host, port, home)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid:
        os.close(write_fd)
        with os.fdopen(read_fd) as pipe:
            line = pipe.readline().strip()
        os.waitpid(pid, 0)
        if line.startswith("ok "):
            print(line[3:])
            return 0
        sys.stderr.write((line or "err 启动失败") + "\n")
        return 1
    os.close(read_fd)
    os.setsid()
    if os.fork():
        os._exit(0)
    sys.stdin = open(os.devnull, "r")
    log = open(home / "server.log", "a", buffering=1)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    return bind_and_serve(host, port, home, notify=write_fd)


def stop(home) -> int:
    meta = read_meta(home)
    if not meta or not meta.get("pid"):
        print("信息中心没有在运行")
        return 0
    pid = int(meta["pid"])
    if not pid_alive(pid):
        server_json(home).unlink(missing_ok=True)
        print("信息中心没有在运行")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_alive(pid):
            server_json(home).unlink(missing_ok=True)
            print("已停止")
            return 0
        time.sleep(0.1)
    sys.stderr.write("进程还在，停止超时\n")
    return 1
