# TODO · grok（B 线 · R10 跨机联邦）

> 只写本方进展。计划调整写信给 coordinator，不改 PLAN.md。

- [x] 2026-10-14 认领 B 线。生产文件按 §〇；B4 的 README 与需求文档归 omp，我只发 diff。
- [x] 2026-10-14 B1 推荐案：`_work/federation-decision.md`（未冻结，等 D1/D2）。
- [x] 2026-10-14 B2 草案：`_work/federation-design.md`（未冻结；等 opencode / kimi 复核）。
- [x] 2026-10-14 按 `_work/verify-b2.md` 改了单文件上限（先写成 700000）和三处措辞。
- [x] 2026-10-14 B2 终签 `m1a138f175e4072b7d`：`_work/federation-design.md` 改为已冻结。上限定为 1000000（对齐 `httpapi.MAX_BODY`）。无本机收件人 `skipped`、回执 `by` 左段、`import_remote` 的 `to` 比对、`reply` 透传 `local_node` 已写入 §4/§5/§7/§10。
- [x] 2026-10-14 B3 本方已落地：`config.federation_settings`（`add`/`remove` 保留顶层键）、`infocenter/sync.py`、`ic sync export|import|once`、`tests/test_federation.py`。§10 已在 cursor 的工作区（`local_node` / `import_remote` / `apply_receipt` / httpapi 透传），不再另发一份 diff。全量 `unittest` 75 项绿。
- [x] 2026-10-14 D2：`client.request` 的 `X-Window`；`ic case` / `ic cases` / `ic windows`；`crew pause|resume|abort|close`；`crew down --only`；守护生成窗口号、离场上报、headless 改为读案卷→干活→写回一行节点。`tests/test_d2_commands.py`。
- [x] 2026-10-14 kimi 已核实预复核两处修复（`m1a1391fb06bde94fe`）：`_plant` 目录名、三处 sqlite 显式 close，并在同一快照复跑 75 项全绿、0 跳过。六要点第 6 项算过。第二轮冻结复核由 kimi 进行，结论未到。
- [x] 2026-10-14 B4：按 coordinator `m1a139240bc2a9d4fe`，已把 diff 发给 omp（抄送 opencode，`m1a1392892ce2a30c7`）。含 README「跨机联邦」小节、需求文档 R10 状态、`.gitignore` 的 `.infocenter/`。三份文件未改，等 omp 合入。
- [x] 2026-10-14 D2-a：`crew pause` 打断正在跑的 headless。`_run_headless` 改用独立进程组的 `Popen`，把 `headless_pid` 写入守护 meta；`control(pause)` 对该进程组发 `SIGTERM`，不杀守护。`watch` 在本窗口 `paused` 时跳过新一轮，有未处理新信则不推进游标。
- [x] 2026-10-14 D2-b：`tests/test_d2_commands.py` 补了 `crew resume` / `crew close`、`_run_headless` 的 `INFOCENTER_WINDOW`、pause 真打断、暂停时跳过 headless。全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` = 82/82 OK，零 ResourceWarning。PLAN 里 D2-a/b 仍写「收口在途」，已写信请 coordinator 改。
- [x] 2026-10-14 16:10 心跳：本项目无未读。B 线与 D2（含 D2-a/b）均已验收，coordinator 已回信并改 PLAN（`m1a1395fbeecc6db7b`）。名下无下一项。D 线文档在 pi；D8 重启在 coordinator，其 TODO 已改为等 pi 本轮结束后再 `crew down/up`。未发信。
- [x] 2026-10-14 16:21 心跳：收件箱仍无未读。B1–B4 与 D2（含 D2-a/b）在 PLAN 里均已冻结/验收，名下无下一项。D 线三份文档 mtime 未动（README 14:53、SKILL 02:43、需求文档 15:01），仍归 pi。D8 重启仍归 coordinator，其 16:12 巡检已记下等 pi 本轮结束后 `crew down/up`。未发信。
- [x] 2026-10-14 16:34 签收 omp 的 B4 收口确认（`m1a1398a3192916a5c`）：三处文档已在位，来信写明无需再动。未回。
- [x] 2026-10-14 16:54 P0/P1（coordinator `m1a1398c0bbfd82e1e`，规格 `_work/final-review.md`）。只改了 `crew.py` / `client.py` / `cli.py`，新测试 `tests/test_debug_p0.py`。P2 与 `sync.py` U-3 未做。M1 在 `store.py`，不在本方文件。
  - C-3 `crew.py`：`Popen(..., errors="replace")`，`watch` 捕获本轮异常后记日志续跑，不因此离场。`test_headless_replaces_non_utf8_and_reports_failure`、`test_watch_continues_after_headless_decode_error`
  - C-7 `crew.py` `control`：`pause` 先 `_interrupt_headless`，再查办公室/案卷。`test_pause_interrupts_before_office_lookup`
  - C-6 `crew.py` `watch`：headless 超时、失败或被打断时不推进游标，并补 `[自动] 中断` 节点。`test_failed_headless_keeps_cursor_and_notes_interrupt`
  - C-1 `crew.py`：`_start_daemon` 去掉子进程的 `INFOCENTER_WINDOW`；`_adopt_window` 只认显式 `--window`（`cli.py` 已加该参数）。`test_start_daemon_drops_window_and_records_start`、`test_adopt_window_ignores_inherited_env`
  - C-5/M5 `client.py`：`TimeoutError` / `OSError`（含 `RemoteDisconnected`）包成 `ApiError(transport)`，`call` 再转 `MailError`。`test_request_wraps_timeout_and_remote_disconnect`
  - C-4 `crew.py`：pid 文件写入 `started_at`，核对命令行与启动时刻；收到停止信号后删掉自己的 pid 文件。`test_reused_pid_is_not_our_daemon`、`test_live_watch_pid_matches_and_unlinks_on_stop`
  - C-8 `crew.py`：办公室连续 3 次登记失败就退出、不进收信循环；`crew status` 对 `office_registered=false` 打红字「办公室未登记」。`test_watch_exits_when_office_never_registers`、`test_status_marks_unregistered_office_in_red`
  - 全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` = 108 OK，零 ResourceWarning。已回 coordinator。
- [x] 2026-10-14 17:09 心跳：签收 coordinator 交卷确认（`m1a139aaf9d8e36e4d`）。C-1/C-3/C-4/C-5+M5/C-6/C-7/C-8 验收通过；P2 与 `sync.py` U-3 本轮不做。进入 kimi 复核 + D8 窗口，**不再改生产文件**。本轮复跑全量 118/118 OK，零 ResourceWarning。未回信。名下无下一项。
- [x] 2026-10-14 17:24 心跳：本项目无未读。kimi 的 `_work/verify-debug.md`（17:19）结论「可以冻结」：本方 C-1/C-3/C-4/C-5+M5/C-6/C-7/C-8 均「已修」，P2/U-3 未动属实。N1（`IncompleteRead`）、N2（失败重试无退避）为非阻断，未派单，未改生产文件。PLAN §一/§八仍停在 17:07「待独立复核」，但 kimi 已于 17:19 回派单信 `m1a139b59cc53a1cd3` 给 opencode（对方未读 1），不另发。D8 仍归 coordinator。名下无下一项。
- [x] 2026-10-14 17:37 心跳：收件箱 0 未读。上条所说的 PLAN 滞后已由 coordinator 在 17:25 补上（`PLAN.md` mtime 17:25）：M5 正式冻结，N1–N4 转 P2 本轮不改，D8 记为非无头窗口待执行。B1–B4、D2、本方 C-1/C-3/C-4/C-5+M5/C-6/C-7/C-8 均已验收。D 线文档仍归 pi（README 14:53、SKILL 02:43、需求文档 15:01，未落笔；17:25 刚解除待命，改派条件未触发）。未改生产文件，未发信，未改 activity。名下无下一项。
