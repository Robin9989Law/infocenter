# A1 · `store.py` / `httpapi.py` 并发与事务审查报告

> 审查人：**cursor**（A 线 owner）
> 日期：2026-10-14
> 范围：`infocenter/store.py`、`infocenter/httpapi.py`、`infocenter/server.py`

---

## 一、架构与并发模型概览

1. **HTTP 服务层（`server.py`）**：
   - 使用 `ThreadingHTTPServer`（`daemon_threads = True`），每个 HTTP 请求在独立线程中调用 `httpapi.handle(..., store)`，单进程内共享同一个 `Store` 实例。
2. **连接与锁配置（`store.py`）**：
   - `_connect()` 为每次 `_session()` 新建独立 `sqlite3.Connection`（不存在跨线程共用 `Connection` 的风险），设置 `timeout=5`、`PRAGMA foreign_keys = ON`、`PRAGMA busy_timeout = 5000`，数据库初始化启用 `PRAGMA journal_mode = WAL`。
3. **长轮询通知（`events` / `_signal`）**：
   - `Store` 持有进程内 `self._cond = threading.Condition()`；`send()` 在事务提交成功后调用 `self._signal()`（`notify_all()`），唤醒所有阻塞在 `events()` 的长轮询线程。

---

## 二、审查发现与已合入修复

### 1. 写事务未开启 `BEGIN IMMEDIATE` 导致的并发写升级冲突与 TOCTOU 竞争（已修复）

- **现象**：
  - `_session(self, immediate: bool = False)` 虽然预留了 `immediate` 参数，但全文件没有任何调用方传入 `immediate=True`。
  - 在 Python `sqlite3` 默认 `isolation_level=""` 下，`SELECT` 不会开启事务；`touch()` 先执行 `SELECT 1 FROM agents WHERE name = ?` 再执行 `INSERT INTO agents`，而 `httpapi.handle()` 对**每一个请求**都会先调用 `store.touch(agent)`。当同一新 agent 并发发起请求时，两个线程可同时看到 `exists is None`，第二个线程触发 `sqlite3.IntegrityError: UNIQUE constraint failed: agents.name`（HTTP 500）。
  - 同理，`mark()` 等先 `SELECT` 后 `UPDATE` 的写路径在 `DEFERRED` 事务下从读锁升级写锁时，一旦遇到并发写，SQLite 会为避免死锁直接返回 `SQLITE_BUSY`（绕过 `busy_timeout=5000`）。
- **修复**：
  - 所有写路径（`seed`、`touch`、`remove_agent`、`send`、`inbox`、`mark`）统一使用 `with self._session(immediate=True) as conn:`（执行 `BEGIN IMMEDIATE`），在事务入口直接排队获取写锁，完整受 `busy_timeout = 5000` 保护。

### 2. `_events_snapshot` 多条 `SELECT` 未处同一读快照导致的游标跳号风险（已修复）

- **现象**：
  - `_events_snapshot()` 先查 `SELECT m.*, d.rowid AS delivery_seq ... FROM deliveries d ...`，随后再查 `SELECT COALESCE(MAX(rowid), 0) FROM deliveries` 作为返回的 `cursor`。
  - 由于默认 `isolation_level=""` 下单条 `SELECT` 处于 autocommit 模式，若另一线程的 `send()` 恰好在两条 `SELECT` 之间提交，第一条查询未见新投递，而第二条查询已返回新 `rowid` 作为 `cursor`，客户端推进 `since=cursor` 后将永久漏掉该条消息通知。
- **修复**：
  - 为 `_session()` 增加 `readonly: bool = False`（执行 `BEGIN DEFERRED`），在 `list_agents`、`_events_snapshot`、`reply`（父消息读取）、`get_message`、`thread` 中启用，确保同一读会话内的多条 `SELECT` 在 WAL 模式下绑定同一个一致性读快照。

### 3. `events()` 长轮询唤醒、锁饥饿与生命周期退出审查（已修复）

- **审查结论**：
  - **无丢唤醒（No Lost Wakeup）**：`send()` 在 `with self._session(immediate=True)` 块退出（即 `conn.commit()` 完成）后调用 `_signal()`；`events()` 在 `with self._cond:` 内执行 `_events_snapshot()`，若无新信才进入 `self._cond.wait(remaining)`。因此不存在「刚查完库尚未 `wait` 时 `_signal` 已经触发」的窗口。
  - **无长轮询期间锁饥饿（No Lock Starvation）**：`Condition.wait(remaining)` 在阻塞等待期间会释放底层锁，其他请求线程及 `_signal()` 可随时获取 `self._cond`。
  - **超时与测试拆卸（Teardown）尾查询问题**：原实现中当 `self._cond.wait(remaining)` 超时返回后，循环无条件再次调用 `_events_snapshot()`：
    1. 若等待期间没有任何 `_signal()` 发生且已超时，二次查库属于冗余 I/O；
    2. 在单元测试 `tearDown()` 关闭 HTTP 服务并清理临时目录时，仍在 `wait()` 中的后台请求线程唤醒后会尝试打开已被删除的 `mailbox.db`，导致 `stderr` 打印 `infocenter: unable to open database file`。
- **修复**：
  - 引入受 `self._cond` 保护的单调计数 `self._seq`（每次 `_signal()` 自增）；`wait()` 返回后若数据库文件已移除，或已到 `deadline` 且 `self._seq == seen`，直接返回上一轮快照，消除冗余查库与拆卸期报错。

### 4. `parse_recipients` 广播别名与字段类型校验漏洞（已修复）

- **现象**：
  - `parse_recipients("all")` 与 `parse_recipients("everyone")` 在字符串分支提前返回了 `["all"]` / `["everyone"]`，而 `send()` 仅判断 `if recipients_in == ["*"]:`，导致 `to="all"` / `"everyone"` 未触发广播，反而在 `agents` 表中插入名为 `all` / `everyone` 的非法记录。
  - `POST /v1/messages` 中若传入非字符串的 `workspace`、`reply_to` 或布尔/数字型 `priority`（如 `false`、`123`、`["bad"]`），原代码会静默转字符串、退化为 `"normal"`，或在 SQLite 绑定参数时抛 `ProgrammingError`（HTTP 500）。
- **修复**：
  - `parse_recipients` 统一归一化所有 `BROADCAST`（`*`、`all`、`everyone`）为 `["*"]`，混投一律报 `invalid_recipient`（400）。
  - `_workspace`、`_reply_to` 与 `httpapi.py` 的 `priority` 透传严格校验类型，非法类型统一返回 400 `MailError`。

---

## 三、关联议题记录（A5-3）

- **`normalize_name` 中的 `.` 语义**：当前 `NAME_RE = r"^[\w][\w.-]{0,63}$"` 且禁止 `".." in name`，已天然支持形如 `node1.grok` 的单点命名并拒绝 `..` 与首尾非法字符。按 coordinator 裁定，此处保持现行行为不改，交由 B 线在 R10 联邦设计中统一约定 `节点.名字` 命名空间规则。
