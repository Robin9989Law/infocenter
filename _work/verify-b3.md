# B3 联邦实现 · 独立复核（第二轮：终审，结论=通过可冻结）

> 复核人：kimi｜日期：2026-10-14（第二轮）
> 终审结论：**通过，B3 可进入冻结流程**。第一轮 6 红全部修复归零，六要点维持全绿，§10 接口已合入且逐条符合冻结设计。
> 基线对照：`_work/federation-design.md`（B2 已冻结）+ B2 复核 5 项修正项；第一轮报告存档于下方"第一轮记录"。

## 终审实测（第二轮）

- 命令：`python3 -W error::ResourceWarning -m unittest discover -s tests` → **75/75 全绿，零告警**（第一轮为 60 项 6 红）。
- 联邦专项：`python3 -W error::ResourceWarning -m unittest tests.test_federation -v` → **27/27 全绿，零跳过**（`_HAS_SEND_NODE/_HAS_REPLY_NODE/_HAS_IMPORT/_HAS_RECEIPT` 均为 True，skipUnless 全部激活）。

## 第一轮 5 项 B3 红灯归因修复核对

| 编号 | 第一轮归因 | 修法核对 | 结果 |
| --- | --- | --- | --- |
| B3-1（3 项） | `_plant` 固定写 `workspace_name="elsewhere"` 与 `_ok` 的 `err==""` 冲突 | `test_federation.py:76` 改为按真实目录名派生（`directory.parent.parent.parent.name`），`:581` 显式用 `project.name`；`_ok` 的 `err==""` 断言保留不动——按第一轮建议的前者处置，警告本身（`sync.py` 导入侧 stderr 提示）保留且不再误伤 | ✅ |
| B3-2（2 项） | 三处裸 `sqlite3.connect` 泄漏触发 ResourceWarning | `test_federation.py:645-652、:666-673、:687-694` 三处均改为 `try/finally: conn.close()` | ✅ |

## §10 接口合入核对（cursor 部分，对照冻结设计 §10 四条）

1. `Store.send(..., local_node)`：`store.py:550-561` 参数存在，`None`/空串归一为 `None`，有值时 `*` 展开去 wire name（`test_star_with_local_node_skips_wire_names` 等已激活通过）。✅
2. `Store.import_remote`：`store.py:726-788`——空 `local_bare_names` 返回 `skipped` 不插入；按原 id/thread_id/reply_to/created_at 插入；查重为 `sorted(_recipients(conn, id)) == sorted(local_bare_names)` 加不可变字段全比，同 id 不同体返回 `conflict` 不覆盖；不 upsert 发信方、不 `_signal`。✅
3. `Store.apply_receipt`：`store.py:790-812`——`to_agent == by` 且 `unread` 才标 `read` 写 `read_at`（applied）；已 `read` 返回 `duplicate` 保留原 `read_at`；`archived` 不改；无投递返回 `absent`；不 `_signal`。✅
4. `Store.reply` 透传 `local_node` 给 `send`（`store.py:677、:722`）；`httpapi.py:468-470` `_local_node()` 从 `federation_settings` 取 node，`POST /v1/messages`（`:227`）与 reply（`:290`）均透传，未配置即 `None`。✅

## 六要点复核结论

第一轮已核的六要点本轮抽查无回退（导出幂等、原子写、symlink/冲突副本忽略、flock 超时、未配 node exit 2 零副作用、crew 不调 sync、`ensure_ascii=False`、700k–1M 上限等均维持），单机不回归一项由 75/75 全绿闭环。**六项全绿。**

## 终审意见

实现与 B2 冻结设计（含 §12 六项拍板）逐条吻合，测试侧卫生问题已按第一轮建议修复，无新增偏差。**建议 coordinator 走冻结流程。** D 线（窗口/案卷）复核另见 `_work/verify-d.md`，与 B3 冻结互不阻塞。

---

# 第一轮记录（存档：预复核，结论=阻塞，已闭环）

> 复核人：kimi｜日期：2026-10-14
> 快照：工作树未提交状态（`sync.py` 801 行最后编辑 14:07、`test_federation.py` 681 行 14:09）；grok 尚未官宣完成，本报告为预复核，供修红后第二轮对照。
> 基线对照：`_work/federation-design.md`（B2 已冻结）+ B2 复核 5 项修正项。

## 一、实测结果

- 命令：`python3 -W error::ResourceWarning -m unittest discover -s tests`（Python 3.14，约 40s）
- 结果：**60 项，6 红**。其中 5 项在 `test_federation.py`（B3 自家测试打自家实现），1 项在 `test_quality_mcp.py`（D1 在途，非 B3）。
- 结论：**B3 当前不可进入冻结流程**。

## 二、六要点逐项核对（TODO-kimi 复核清单）

| # | 要点 | 结果 |
| --- | --- | --- |
| 1 | 导出侧显式校验 wire name 恰好一个点 | ✅ `canonical_wire`/`is_wire_name` 双段 `SEGMENT_RE` + `normalize_name`；非法发信人 `invalid_sender`、非法收件人 `invalid_recipient` 走 reject 且 ledger 去重 |
| 2 | 单文件上限 700k–1M + `ensure_ascii=False` | ✅ `MAX_ENVELOPE = 1_000_000`；导出紧凑 JSON 无 `\u` 转义（`test_mixed_recipients_export_once` 断言覆盖）；进出口两侧超限都 reject 且只报一次 |
| 3 | 回执 `by` 节点段 == `outbox/<节点>/receipts/` 目录名 | ✅ 导出写 `by=f"{node}.{bare}"` 入本节点 receipts；导入校验 `left == peer.name` 且 `right ==` 文件名片段（`sync.py:666-674`） |
| 4 | 无 `<本机node>.<裸名>` 收件人 → skipped 不入库 | ✅ 双保险：sync 层 `names` 为空即 skip（`sync.py:599-601`）；store 层 `import_remote` 对空 `bare_names` 返回 `skipped`（`store.py:685-689`） |
| 5 | 查重以 deliveries 的 `sorted(local_bare_names)` 为准 | ✅ `store.py:713`：`sorted(_recipients(conn, msg_id)) == bare_names`，同 id 不同体 → `conflict` 不覆盖（测试覆盖，但目前红，见下） |
| 6 | `reply` 透传 `local_node`；未配 node exit 2；单机不回归 | ⚠️ 前两项 ✅（`store.py:677` 透传；`test_unconfigured_node_exits_2_and_writes_nothing` 绿）；**单机不回归 ✗**——全量 6 红 |

## 三、红灯归因（5 项 B3 + 1 项 D1）

### B3-1（3 项）：workspace_name 警告与自家测试冲突

`sync.py:622-626` 在导入时对 `workspace_name != 项目目录名` 的封套向 stderr 打 warning，而 `_ok()`（`test_federation.py:155`）断言 `err == ""`，且 `_plant()` 固定写 `workspace_name="elsewhere"`、项目目录叫 `proj`/`box` —— 实现与测试自相矛盾：

- `test_same_id_different_body_does_not_overwrite`（:544）
- `test_partial_file_then_replace_imports_once`（:585）
- `test_reply_can_arrive_before_parent`（:609）

处置二选一：`_plant` 的 `workspace_name` 改用各测试的目录名（或 `_ok` 容忍该 warning）；或警告降级/去除。B2 设计稿只要求"导入时 workspace 改写为本地 --dir"，未要求此警告，倾向前者。

### B3-2（2 项）：测试侧裸 sqlite 连接泄漏 → ResourceWarning 击穿 stderr 断言

`test_federation.py:643、:660、:677` 三处 `sqlite3.connect(...).execute(...)` 一次性表达式从不 close。`-W error::ResourceWarning` 下 GC 回收时在 stderr 打 `ResourceWarning: unclosed database`，时机漂移，污染同进程后续 `_ok` 的 `err==""` 断言：

- `test_read_receipt_round_trip_is_stable`（:648，直接中招）
- `test_basename_scope_still_exports`（:256，GC 漂移间接受害）

修法：三处改为 `with closing(sqlite3.connect(...)) as conn:` 或取完 `fetchone()` 后显式 `conn.close()`。注意这与 M1 时 cursor 清理的 `test_infocenter.py` Popen 管道告警同性质——测试自身卫生问题，非实现缺陷。

### D1-1（1 项，非 B3，仅记录）：`test_quality_mcp.test_line_json_no_notify_and_unlocked_agent_tools`

断言工具数 10，实际 9。`mcp_server.py` 当前 diff 只有 A3 的 `_mode` 复位 2 行，尚无 `ic_case`——cursor 的 D1 在途中间态（测试先行）。不阻塞 B3，提请 cursor 收口时自查。

## 四、正面确认（已绿且符合设计）

- 导出幂等：内容不变不重写、计数不再增（`test_mixed_recipients_export_once` 二次导出断言）
- 原子写 tmp+`os.replace` + fsync；`._*.tmp` 超 1h 清扫且不越节点目录
- symlink / 路径穿越 / 网盘冲突副本（` (1)`、`conflict` 目录）全部忽略；`peers` 白名单生效
- flock 30s 超时 `sync_busy`；未配 node 时 exit 2 且**零副作用**（不建目录、不建库、不留锁文件）
- 回执：`archived` 投递忽略回执；回执 round-trip 幂等（第二轮全零）
- `crew.py` 现有 diff 全部是 A5/A6 存量（心跳文案/env 注入），**无 crew 调 sync**，符合 B2 "v1 不在 crew 调 sync"
- `httpapi.py` 的 `priority=body.get("priority")` 变化安全：`_priority(None)` → `"normal"`（`store.py:2455`）
- `config.add/remove` 保留 `node/peers/outbox_dir` 及未知顶层键（`test_add_and_remove_keep_federation_keys` 绿）
- §10 最小 diff 边界：`import_remote`/`apply_receipt` 不 upsert 发信方、不 `_signal`，符合设计

## 五、结论与下一步

**阻塞**：B3 实现方向与 B2 设计逐条吻合，六要点中五个全绿，但全量测试 6 红（5 个在 B3 自家），不可冻结。已发信 grok 列明归因与修法；待其修红后做第二轮复核（重点：全量绿 + 本轮已核要点不复查，只补 diff 增量）。
