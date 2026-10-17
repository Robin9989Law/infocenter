# Windows 环境下测试运行卡死及会话中断问题诊断报告

## 1. 现象描述 (Symptoms)

* **表面现象**：在 Agent 会话中只要运行 `tests.test_quality_mcp`（特别是 `test_lsp_framing_initial_watch_backlog_and_reminder_tail`），命令执行即刻永久无响应，随后 IDE 宿主/扩展服务因超时或看门狗触发重启（出现系统提示：`All your subagents and background tasks have been stopped due to server restart`），导致前端 UI 卡死、历史会话中断。
* **上一会话现场**：上一会话（ID: `a674a5fe-e6ca-44c7-a7fb-14cf45b209e2`）于第 1062 步执行以下命令后停止写入：
  ```bash
  python -m unittest tests.test_quality_mcp.McpQualityTest.test_lsp_framing_initial_watch_backlog_and_reminder_tail
  ```

---

## 2. 根因分析 (Root Cause Analysis)

### 根因 1：`tests/test_quality_mcp.py` 中的管道读取在 Windows 下为死等阻塞，超时完全失效

在文件 [tests/test_quality_mcp.py](file:///h:/我的云端硬盘/信息中心/tests/test_quality_mcp.py#L32-L80) 中，`read_lsp` 与 `read_line` 的实现如下：

```python
def read_lsp(stream, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError(buf)
        if sys.platform != "win32":
            ready, _, _ = select.select([stream], [], [], 0.1)
            if not ready:
                continue
        ch = stream.read(1)  # <--- 致命阻塞点
        if not ch:
            raise EOFError(buf)
        buf += ch
    ...
```

* **Linux / macOS 行为**：使用 `select.select([stream], ...)` 每次等待 0.1s，无数据就循环，到期触发 `TimeoutError`。
* **Windows 行为**：因为 Windows 的 `select.select` 只支持 Socket，不支持文件/匿名管道，因此原作者加了 `if sys.platform != "win32":` 绕过。
* **后果**：Windows 直接进入 `stream.read(1)`。在 Windows 匿名管道上，`read(1)` 是底层的同步 `ReadFile` 系统调用。**如果管道中当前没有数据，线程会被操作系统直接挂起，直到有数据或管道关闭**。
* **超时失效**：外层的 `while time.time() > deadline:` 根本没有机会被执行，`timeout` 参数形同虚设，测试进程直接永久挂死（Deadlock / Hang）。

---

### 根因 2：`test_lsp_framing_initial_watch_backlog_and_reminder_tail` 子进程未及时产出通知帧

在该用例中：
1. 启动了子进程 `bin/ic mcp --agent cursor`。
2. 父进程向子进程写入 `initialize` 帧，读取返回。
3. 接着立即执行 `note = read_lsp(proc.stdout)` 试图等待 `_Watch` 后台线程推送 `notifications/message`。
4. `_Watch` 线程在初始化后需要向 `ThreadingHTTPServer` 发送 HTTP GET `/v1/events` 长轮询请求。
5. 在 Windows 下，由于 `stream.read(1)` 立即阻塞，而子进程的 HTTP 请求或事件比对若有任何延迟、缓冲（Python 缓冲输出）或长轮询挂起，父进程已经处于底层不可中断阻塞状态。
6. `run_command` 的执行进程挂起，IDE 无法在规定时间内收到回报，触发前端卡死和后端守护服务重置。

---

## 3. 涉及的关键代码与文件

| 文件 | 位置 | 说明 |
| :--- | :--- | :--- |
| `tests/test_quality_mcp.py` | Line 32 - 80 (`read_lsp`, `read_line`) | 缺少 Windows 下非阻塞检查机制，`stream.read(1)` 死等 |
| `tests/test_quality_mcp.py` | Line 139 - 190 (`test_lsp_framing_...`) | 该测试会启动 `_Watch` 并等待通知，极易触发该阻塞 |
| `infocenter/mcp_server.py` | Line 79 - 82, Line 130 - 189 (`_Watch`) | MCP 后台长轮询通知线程 |

---

## 4. 解决方案与修复建议 (Recommended Fix)

派发给后续 Agent 时，可直接采用以下两步修复：

### 修复方案 1：在 `tests/test_quality_mcp.py` 中引入 Windows 兼容的非阻塞/就绪检测

Windows 的匿名管道底层是 Named Pipe，可以使用 Win32 API `PeekNamedPipe` 查询是否有字节可读，避免直接进入 `read(1)` 阻塞：

```python
if sys.platform == "win32":
    import msvcrt
    from ctypes import windll, byref, c_ulong

    def _wait_readable(stream, timeout: float) -> bool:
        """Windows 下利用 PeekNamedPipe 安全等待管道可读，超时返回 False"""
        handle = msvcrt.get_osfhandle(stream.fileno())
        avail = c_ulong()
        deadline = time.time() + timeout
        while time.time() <= deadline:
            success = windll.kernel32.PeekNamedPipe(
                handle, None, 0, None, byref(avail), None
            )
            if not success:
                return False  # 管道已破裂或关闭
            if avail.value > 0:
                return True
            time.sleep(0.02)
        return False
else:
    def _wait_readable(stream, timeout: float) -> bool:
        ready, _, _ = select.select([stream], [], [], max(0.0, timeout))
        return bool(ready)
```

然后在 `read_lsp` 和 `read_line` 中，每次读字节前必须先调用 `_wait_readable`：

```python
def read_lsp(stream, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        remaining = deadline - time.time()
        if remaining <= 0 or not _wait_readable(stream, remaining):
            raise TimeoutError(f"read_lsp header timed out, received: {buf!r}")
        ch = stream.read(1)
        if not ch:
            raise EOFError(buf)
        buf += ch
    ...
```

> **效果**：无论子进程是否写入、何时写入，只要超过 `timeout` 秒，立即抛出标准 `TimeoutError` 异常退出单测，绝对不会把 Python 主进程及 IDE runner 卡死。

### 修复方案 2：检查子进程子线程 `_Watch` 的通知写入时机
* 确认 `infocenter/mcp_server.py` 中 `_Watch._run()` 内部捕获异常时的重试机制（`time.sleep(2)`）。
* 确保子进程的标准输出在写入 `notifications/message` 后显式 `sys.stdout.buffer.flush()`（现有代码已有，但若子进程卡在长轮询中则无法到达）。
* 单测中可在 `proc.stdin.write` 之后加入微小的缓冲或确保 `self.httpd` 及时响应。
