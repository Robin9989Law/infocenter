# M1（A1–A5）独立复核报告 — kimi

> 复核人：**kimi**（跨线独立复核，未写生产代码）
> 日期：2026-10-14
> 范围：PLAN.md §二 A1–A5 交付物，对照 `infocenter/store.py`、`infocenter/mcp_server.py`、`infocenter/crew.py`、`tests/test_quality_*.py`、`_work/a1-store-concurrency-audit.md` 逐项核对

---

## 一、测试实测结果

命令：

```
cd /Users/robinlaw/Downloads/信息中心 && python3 -m unittest tests.test_infocenter tests.test_quality_store tests.test_quality_mcp tests.test_quality_crew -v
```

结果：**Ran 31 tests / OK（31/31 全绿，耗时约 38s）**。单独复跑并以 `grep -ci "unable to open database file"` 检查全量输出：**0 次**，A1 拆卸期报错残留已消除。与 coordinator 验收口径一致。

## 二、逐项核对结论

### A1｜store 并发与事务（`infocenter/store.py`）

- ✅ 写事务 `BEGIN IMMEDIATE`：`_session(immediate=True)` 在 `store.py:106`；写路径 `seed`/`touch`/`remove_agent`/`send`/`inbox`/`mark` 全部传入（`:163, :183, :218, :299, :391, :425`）。
- ✅ 多 SELECT 读路径 `BEGIN DEFERRED`：`_session(readonly=True)` 在 `store.py:108`；`list_agents`/`_events_snapshot`/`reply` 读父信/`get_message`/`thread` 启用（`:209, :253, :356, :411, :442`）。
- ✅ `events()` 长轮询 `_seq` 检查与拆卸保护：`store.py:239`（`seen = self._seq`）、`:249`（超时且 `_seq` 未变直接返回）、`:247`（`self.path.exists()` 目录存在性检查）。
- ✅ `_session()` 每请求新建连接：`store.py:103`（`_connect()` 每次调用）；`busy_timeout=5000`（`:98`，另 `sqlite3.connect(timeout=5)` `:95`）；WAL 在 `_init`（`:119`）。
- ✅ `parse_recipients` 归一化与混投口径：`all`/`everyone`/`*`（含大小写与空白变体）统一归一为 `["*"]`（`store.py:60-65`）；广播与其他收件人混投一律 `invalid_recipient` 400（`:62-64`），与测试 `test_broadcast_variants_and_mixed_recipients_rejected` 一致。
- ✅ `workspace`/`reply_to`/`priority` 类型校验：`_workspace`（`store.py:621-631`，非字符串/超 1024 拒）、`_reply_to`（`:634-640`）、`_priority`（`:610-618`，非字符串及非 normal/high 拒）。
- ✅ `normalize_name` 单点支持、拒绝 `..`：`NAME_RE` 允许 `.` 且 `".." in name` 拒绝（`store.py:11, :41`）；`node1.grok` 通过、`a..b` 拒绝由 `test_invalid_agent_names` 覆盖。
- ✅ `_work/a1-store-concurrency-audit.md` 存在（63 行），含 §三「`normalize_name` 的 `.` 语义」记录（保持现行行为，恰好一点的命名空间细则交 B 线 R10 约定），与 PLAN A5-3 口径一致。

### A2/A3｜边界矩阵与 MCP 帧协议

- ✅ `tests/test_quality_store.py` 覆盖 PLAN §二 A2 矩阵：limit 越界（0/-1/101/abc/None）、subject 空/超长 201/换行、body 空/非字符串/超 100k（413）、广播归一化与混投拒绝、非法名字（含 `a..b`/`.lead`/65 字）、非收件人 mark（409 `not_a_recipient`）、局外人读/签收/插话/thread 均 404、thread 部分参与可见性隔离、HTTP 401/403/字段类型 400、events 非法 since/wait 400、并发长轮询唤醒+并发写无 IntegrityError。
- ✅ `tests/test_quality_mcp.py` 覆盖 PLAN §二 A3：LSP `Content-Length` 与 line-JSON 双模、`_Watch` 首帧 since=0 补报积压未读（且帧格式跟客户端）、工具尾巴未读提醒递减至消失、`--no-notify` 不推通知且无提醒尾巴、未锁定时工具要求显式 agent、非 202x 协议号回退、未知方法 -32601/未知工具报错。
- ✅ `run_mcp()` 重入复位 `_mode = None`：`mcp_server.py:19-20`。双模检测/回写 `:271-286`、`:325-333`；`_Watch` 初始 `cursor=0`（`:72`）；`notify=False` 时 `_Watch` 不启动（`:32`）、无 watch 时工具不带尾巴（`:245`）。

### A4/A5｜crew 守护纪律（`infocenter/crew.py`，`tests/test_quality_crew.py`）

- ✅ `poll` clamp 5–3600（`_poll_seconds` `crew.py:213-218`，`watch` 内二次 clamp `:467`）；`heartbeat` clamp 0–86400（`_heartbeat_seconds` `:221-226`，`:470`）；边界值（1→5、99999→3600、-5→0、100000→86400、坏值回退默认）由 `test_poll_and_heartbeat_clamp` 覆盖。
- ✅ workspace 过滤：守护轮询带 `workspace=project`（`crew.py:488`），store 侧 `_workspace_scope` 全路径/解析路径/basename 大小写不敏感命中（`store.py:479-497`），由 `test_workspace_scope_filtering` 覆盖。
- ✅ `down --dir` 只清本项目：按 `meta["dir"]` 过滤 pid 文件（`crew.py:366-374`），tmux 只关本项目会话（`:388`）；由 `test_multi_project_status_and_down_dir_isolation` 端到端验证（p1 清理后 p2 守护仍活）。
- ✅ `heartbeat_prompt` 展开 `{agent}`/`{project}`：`crew.py:424-430`；未配置（空/缺省）回退内置模板（`:431-454`），member/coordinator 两套内置文案；由 `test_heartbeat_prompt_custom_override_and_agent_env` 单元+端到端覆盖。
- ✅ `heartbeat_mode` 支持 `idle`/`always`：`crew.py:471` 读取，`idle` 时新信重置 `last_run`、`always` 不重置（`:502-503`）；由 `test_watch_elif_at_most_one_headless_per_round` 双模式对照覆盖。
- ✅ `_start_daemon` 注入 `env["INFOCENTER_AGENT"] = agent`（`crew.py:262`）；`_run_headless` 注入（`:519-520`）；端到端测试断言子进程实际看到 `AGENT=cursor`。

### 顺带复核（超出派单范围，A6 追加修正）

- ✅ A6-1（`elif` 回归）已修复：`crew.py:498-514` 新信与心跳互斥（`elif`），仅 `last_run` 重置按 mode 门控；`test_watch_elif_at_most_one_headless_per_round` 覆盖。
- ✅ A6-2（裸 `coordinator` 措辞）已修复：内置 member 模板改用「主协调 agent（role=coordinator 的那位）」，支持注入真实协调者名（`crew.py:440-454`）；`test_member_heartbeat_prompt_no_bare_coordinator_recipient` 覆盖。

### 版本与台账

- ✅ PLAN.md §二 A1–A5 均已勾 ✅（`:38, :41, :44, :47, :50`），D3 已勾（`:97`）；§一 M1 状态为「交付完成（待 kimi 独立复核落款）」。本报告即落款依据。

## 三、非阻塞观察（不影响冻结）

1. `normalize_name("a.b.c")`（多个不相邻点）仍可通过——现行口径只承诺「单点支持 + 拒绝 `..`」，「恰好一个点」的严格约束按 A1 报告 §三裁定留给 B 线联邦层（导出侧）执行，属设计分工而非缺陷。建议 B3 实现时在导出路径显式校验点数。
2. `tests/test_infocenter.py:349` 有 `ResourceWarning: unclosed file`（测试自身 subprocess 管道未关闭），不影响结果，可顺手清理。
3. `inbox()` 走 `BEGIN IMMEDIATE`（含 `_upsert` 写）——纯读浏览也会短暂持写锁，当前规模无实际影响，仅记录。

## 四、总结论

**通过（可冻结）。** 31/31 测试实测全绿，无 `unable to open database file` 残留；A1–A5 各项交付物与 PLAN §二验收口径逐条相符，未发现阻塞问题。M1 可正式收口冻结。
