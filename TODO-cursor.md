# TODO · cursor（A 线：代码质量与测试加固）

> 只写本方进展；计划调整发信给 coordinator（opencode）。

- [x] 2026-10-14 认领 A 线分工（M1 质量基线），签收并回复 kickoff 启动信與 coordinator 裁定信。
- [x] **A5-1 & A5-2（D3 实现）**：在 `infocenter/crew.py` 最小区域实现 `heartbeat_prompt`（支持 `{agent}`/`{project}` 展开，未配回退内置模板）、`heartbeat_mode`（`idle`/`always`）及守护进程与 headless 子进程注入 `INFOCENTER_AGENT`；已在 `tests/test_quality_crew.py` 补齐断言，可将 `crew.py` 临时所有权交回 `grok`。
- [x] **A2｜边界与错误码矩阵**：新建 `tests/test_quality_store.py`，覆盖 `limit` 越界、`subject` 空/超长/换行、`body` 空/超 100k、广播（`*`/`all`/`everyone`）与混投拦截、非法名字、非收件人 `mark`、thread 可见性隔离、HTTP 错误码矩阵及多线程长轮询 `notify_all` 唤醒；同时修复 `parse_recipients("all"|"everyone")` 未归一化为 `["*"]` 及 `workspace`/`reply_to`/`priority` 非法类型校验问题。
- [x] **A3｜MCP 帧协议与提醒**：新建 `tests/test_quality_mcp.py`，覆盖 LSP `Content-Length` 与 line-JSON 双模、`_Watch` 首帧 `since=0` 积压补报、`--no-notify` 不推通知且工具尾巴不带未读数、工具错误分支；修复 `run_mcp()` 进程内重入时 `_mode` 复位。
- [x] **A4｜crew 守护纪律**：新建 `tests/test_quality_crew.py`，覆盖 `poll`/`heartbeat` clamp（5–3600 / 0–86400）、workspace 过滤、多项目状态与 `down --dir` 隔离清理。
- [x] **A1｜store 并发与事务审查**：完成 `store.py` / `httpapi.py` 并发与事务审查，修复写事务 `BEGIN IMMEDIATE`、读快照 `BEGIN DEFERRED` 与 `events()` 超时/拆卸尾查询问题，产出 `_work/a1-store-concurrency-audit.md`。
- [x] **A6｜coordinator 复核追加修正（`m1a138dfe93fd5bf04`）**：
  - **A6-1**：修复 `infocenter/crew.py` `watch()` 中 `if heartbeat` 回归为 `elif heartbeat and time.monotonic() - last_run >= heartbeat:`，仅在新信分支内按 `if mode != "always": last_run = time.monotonic()` 门控重置，确保单轮最多运行一次 headless。
  - **A6-2**：修复 `build_heartbeat_prompt()` 内置 member 模板中的裸身份名文案（改为 `给主协调 agent ...（\`ic agents\` 里 role=coordinator 的那位）发信说明`，并在 `watch` 能取到配置时注入真实协调者名 `coordinator_name=config.coordinator(home)`，同时将 `coordinator 靠它调配资源` 改为 `主协调 agent 靠它调配资源`）。
  - 在 `tests/test_quality_crew.py` 新增 `test_member_heartbeat_prompt_no_bare_coordinator_recipient` 与 `test_watch_elif_at_most_one_headless_per_round`，全量测试 33/33 通过；`infocenter/crew.py` 心跳区临时授权归还 `grok`。
- [x] **M1 非阻塞观察 #2 清理（`m1a138fe76f4e11223`）**：在 `tests/test_infocenter.py` 的 4 处 `Popen` `finally` 块补齐 `stdin/stdout/stderr` 显式关闭，`python3 -W error::ResourceWarning -m unittest discover -s tests -v` 全量 33/33 零 `ResourceWarning` 通过。
- [x] **B3 协作（§10 diff 合入）**：已在 `infocenter/store.py` 与 `infocenter/httpapi.py` 合入 `Store.send(..., local_node)`、`Store.reply(..., local_node)`、`Store.import_remote`、`Store.apply_receipt` 及 HTTP 层 `local_node` 透传，`tests/test_federation.py` 全部 27 项（含原 9 项 `@skipUnless`）通过。
- [x] **D1 数据层（窗口号 / 认领 / 案卷 / 办公室，派单 `m1a1390537469b0f80`）**：
  - `infocenter/store.py`：新增 `projects`、`windows`、`cases`、`case_nodes` 四张表及 `deliveries` 的 `claimed_by`/`claimed_at` 自动迁移；实现按 `(thread_id, agent)` 先到先得认领（`already_claimed` 409 与 `force=True` 强制接手）、`events`/`inbox` 按窗口过滤与软认领、案卷生命周期与单行节点校验、三种收局（`pause` 暂停不封卷、`abort` 中止封卷、`close` 办结封卷）与 `reopen` 重开、办公室按窗口隔离 `activity`（D6）、正常离场自动中止与进程崩溃惰性补齐（`[自动]` 节点）。
  - `infocenter/httpapi.py`：支持 `X-Window` 请求头（兼容 query/body `window`/`window_id`）透传，新增 `/v1/cases*`、`/v1/windows*`、`/v1/projects*` 路由（无 `X-Agent` 时默认 `"human"` 供管理台直接请求）。
  - `infocenter/mcp_server.py`：启动读取 `INFOCENTER_WINDOW` 或自动生成 `w-<hex>`，`_Watch` 与各工具透传窗口号，退出时自动上报离场，新增只读工具 `ic_case`。
  - `tests/test_quality_window.py`：覆盖规格 §8 全部 10 条测试场景，全量测试 70/70 全绿。
- [x] **D1 复核建议补齐（`_work/verify-d.md` D1-a / D1-b，派单 `m1a1392e2f1090eba6`）**：
  - **D1-a**：在 `tests/test_quality_window.py` 新增 `test_11_legacy_schema_auto_migration`（及 `test_01` 内迁移回归断言），验证无 `deliveries.claimed_by`/`claimed_at` 列、无 `projects`/`windows`/`cases`/`case_nodes` 四张表的旧 schema SQLite 库打开后表与列完整补齐且历史信件可读、可认领。
  - **D1-b**：在 `tests/test_quality_window.py` 新增 `test_12_transfer_case_positive_flow`（及 `test_07` 内转办断言），验证未封卷案卷调用 `transfer_case` 后 `owner_window` 切换、`"转办 → 交接给 w-2"` 节点文案落库及同线程投递 `claimed_by` 归属覆写。
  - `python3 -W error::ResourceWarning -m unittest tests/test_quality_window.py -v` 12/12 零 `ResourceWarning` 通过。
- [x] **D3-a / D3-b / D3-c 一次性收口（`infocenter/web/index.html`，派单 `m1a139436d103b6d91` & 授权 `m1a1394caa10b67397`）**：
  - **D3-a**：修复案件列表末节点渲染 `[object Object]` 问题——改为 `const last = c.last_note || (c.last_node && typeof c.last_node === "object" ? (c.last_node.note || c.last_node.line || "") : c.last_node) || "";`。
  - **D3-b**：修复案件操作按钮显隐仅判断 `status_label` 的问题——改为优先按原始 `c.status`（`open`/`doing`/`pending`/`waiting` 均可暂停，`paused` 可恢复，`c.sealed` 或 `aborted`/`done` 隐藏中止/办结），同时补齐 `.status` CSS 对原始状态名及「待办」「离场」样式的兼容（修复 `waiting` 状态「待 xxx」因类名拼接丢失底色的问题）。
  - **D3-c**：办公室按钮文案改为「离场并封卷」，并在 `disableWindow()` 中增加 `confirm()` 二次确认提示（确认后同步刷新 `loadWindows()` 与 `loadCases()`），同时统一对 `onclick` 内嵌的 `window_id` 与 `case.id` 做 `escapeHtml` 转义加固。
  - 在 `tests/test_quality_window.py` 新增 `test_13_ui_console_smoke_and_d3_abc` 覆盖 `/ui` 静态页、三视图、waiting 暂停/恢复、办公室离场级联封卷端到端冒烟（并加固 `test_quality_crew.py` 心跳文件落盘轮询断言避免半写竞态），全量 `python3 -W error::ResourceWarning -m unittest discover -s tests -v` **78/78 零 `ResourceWarning` 通过**。
  - 已获 coordinator 复核签收通过（`m1a13952a77bbe701a`），`infocenter/web/index.html` 与 `infocenter/server.py` 临时所有权已释放。
- [x] **M5｜最后评审与 debug 加固（`_work/final-review.md`，派单 `m1a1398c0bb968cdd9`，回信 `m1a139a7565f8e42b7`）**：
  - **P0 修复**：**U-1 存储型 XSS**（`store.py` 新增 `CASE_ID_RE` / `normalize_case_id` 白名单校验，`web/index.html` 移除全部内联 `onclick` 改用 `dataset` + `addEventListener` 委托及 `encodeURIComponent`）；**M1(events) 信件丢失**（`store.py` 无窗口与有窗口两路径均改取最旧 `limit` 条、`cursor` 取实际返回最大 `delivery_seq`）。
  - **P1 及顺手加固**：**D-1 + M1-data**（`inbox` 过滤下推 SQL、先过滤后切片，`unread_total` 与 `_unread_counts` 口径对齐）、**D-2**（跨 agent 转办释放源 owner 投递认领，封卷与惰性对账按线程清理）、**D-3**（非当事人立案/认领统一校验 `_is_thread_party` 返回 404）、**D-4**（死 pid 窗口显式复活清 `pid=NULL`、软触达不复活已离场窗口）、**D-5**（HTTP 与 Store 入口统一类型校验返回 400）、**U-2**（`server.py` 校验 `Content-Length` 非整数/`<0` 返回 400、`>MAX_BODY` 返回 413）、**U-4**（`mcp_server.py` 注册纳入 `try` 并校验窗口号规范）、**L1**（`httpapi.py` 写接口校验 `Sec-Fetch-Site` / `Origin` 并强制 JSON Content-Type），并顺带覆盖 D-6/D-7/D-8/M2/M3/L2。
  - **回归与复核**：新建 `tests/test_hardening_store.py`（15 项）并更新 `tests/test_quality_{store,mcp,window}.py`，全量 **118/118 零 `ResourceWarning` 通过**；已通过 coordinator 独立复跑与 kimi 独立复核（`_work/verify-debug.md`）。




