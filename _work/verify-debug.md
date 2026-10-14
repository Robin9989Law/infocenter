# M5 debug 轮 · 独立复核（kimi）

> 日期：2026-10-14｜复核人：kimi（独立第三方，未参与本轮修复）
> 基线：协调者 17:06 快照（118/118 OK 零 ResourceWarning）之后无写盘，复核全程**未改任何生产文件**
> 方法：① 全量测试复跑；② 18 项修复逐项**对码**核对（读实现，不看测试下结论）；③ P0 五项**反驳复现**（独立脚本，刻意走与测试不同的路径）

---

## 一、全量基线

`python3 -W error::ResourceWarning -m unittest discover -s tests` = **118/118 OK，零 ResourceWarning**（62.3s，与协调者快照一致）。

## 二、P0 反驳复现（全部通过，旧缺陷均未重现）

复现脚本：`/tmp/ic_verify_rebuttal.py`（Python，Store/HTTP/真守护子进程三层次）+ `/tmp/icjs/u1_rebuttal.js`（jsdom 真浏览器引擎）。

| # | 缺陷 | 复现设计 | 结果 |
|---|---|---|---|
| U-1 | 存储型 XSS（属性上下文逃逸） | ① 服务端：`POST /v1/cases` 打 5 个恶意/非法 `thread_id`（`&#39;` 逃逸串、单双引号、换行、300 字符超长）→ **5/5 被 400 拒绝**，合法 id 正常；② 前端 jsdom 引擎加载 `web/index.html` 并执行内嵌脚本：全文档 **0 个内联 onclick**；用页面自身 `escapeHtml` 走旧攻击面，`&` 被双重转义（`&amp;#39;`），属性解码后仍是字面文本；`window.__xss` = **undefined** | 修复成立 |
| M1-events | >limit 丢信 | 造 45 封新信、limit=20 翻页：**无窗口路径** 3 页取全 45/45 顺序正确；**有窗口路径**（带软认领）同样 45/45，cursor 终值 45 | 修复成立 |
| C-3 | 非 UTF-8 自杀+误封卷 | `_run_headless` 直接喂 `\377\376\375` 垃圾字节（exit 0 与 exit 1 两种）：outcome=ok/fail，**无 UnicodeDecodeError 穿透**；watch 主循环另有 `except Exception` 兜底（crew.py:940-943），到不了 `finally _leave_office` | 修复成立 |
| C-6 | 失败/超时不回退 cursor | 端到端真守护：临时 home + 真 HTTP 服务 + `headless: "exit 1"` + 3 封触发信，跑 9 秒：日志 5 次「游标留在 0，下轮重试」，**cursor 始终为 0**（信没被吞），案卷补到 **[自动] 中断节点**（3 线程 × 5 轮 = 15 个） | 修复成立（见备注 N2） |
| C-7 | pause 先打断再查办公室 | 起一个 `sleep 300`（`start_new_session`，模拟 headless）挂进 meta，**服务全挂**（连 127.0.0.1:9）时 `control(pause)`：sleep **先被 SIGTERM 打断**，后续只读 API 才报错 | 修复成立 |

## 三、逐项对码（18/18 已修，无半修、无假修）

### cursor 10 项（store.py / httpapi.py / mcp_server.py / server.py / web/index.html）

| 项 | 裁决 | 关键证据 |
|---|---|---|
| U-1 XSS | 已修 | 服务端白名单 `CASE_ID_RE` + `normalize_case_id`（store.py:16,92-102），create/claim/transfer/append 全入口过校验（store.py:1155,1221,1276,1336,1421）；前端废 onclick 改 `dataset`+`addEventListener`（index.html:214,229-244）；L3 附带项也顺手修了（动作按钮全 `encodeURIComponent`，index.html:260,282-313） |
| M1-events | 已修 | 两路径均取最旧 limit 条、cursor=实际返回最大 seq（store.py:469-476,525,545-546,564,577）；认领冲突 `rowcount==0` 时不推进 cursor |
| D-1+M1-data | 已修 | 过滤下推 SQL（NOT EXISTS 封卷/暂停 + 三级有效认领），查询无 LIMIT、Python 复核后才切片（store.py:902-982）；`unread_total` 与 `_unread_counts` 口径逐行一致（985-998,2747-2771） |
| D-2 转办双归属 | 已修 | 跨 agent 转办先清源 owner 全部投递认领再 overwrite 目标（store.py:1442-1454,1464）；封卷与惰性对账兜底按 thread 清所有窗口（1661-1668,2009-2018） |
| D-3 非当事人立案 | 已修 | 三条入口统一 `_is_thread_party` → 404（store.py:1349-1355,1395-1397,1179-1182），底层 `_claim_thread_on_touch` 复检（2249-2250） |
| D-4 死 pid 复活 | 已修 | 复活未带新 pid 则置 NULL（store.py:2431-2439,1872-1879）；软触达不复活、仅 touch 到场复活（2421-2426） |
| D-5 500→400 | 已修 | 入口统一 `_json_object`/`_need_str`/`_opt_str`（httpapi.py:565-608）；state 为 dict 的 unhashable 场景先 isinstance（store.py:1710-1711）；pid 双重校验（httpapi.py:611-627 + store.py:1955-1967） |
| U-2 长度下界 | 已修 | 非整数 400 / `<0` 400 / `>MAX_BODY` 413，`read(-1)` 路径消失（server.py:59-74） |
| U-4 MCP 崩溃 | 已修 | 注册 PUT 入 try、ApiError→MailError（mcp_server.py:46-69）；`resolve_mcp_window` 规范校验；cli.py:271 统一出口退 1 无 traceback |
| L1 CSRF | 已修 | `_check_write_security` 全写路由统一拦截（httpapi.py:175,501-541）：Sec-Fetch-Site 白名单、Origin 校验、非 application/json 415；D 路由无 X-Agent 写请求强制 JSON（跨站表单无法过预检，服务端无 do_OPTIONS） |

### grok 7 项 + 边界声明（crew.py / cli.py / client.py）

| 项 | 裁决 | 关键证据 |
|---|---|---|
| C-1 窗口串号 | 已修 | `_start_daemon` pop env（crew.py:410-411）；`_adopt_window` 只认显式（1036-1043）；watch 退出恢复原值（952-955） |
| C-3 自杀+误封卷 | 已修 | `errors="replace"`（978）+ UnicodeDecodeError 专捕（998-1007）+ watch 兜底续跑（940-943），`_leave_office` 只在真退出时执行 |
| C-4 pid 复用 | 已修 | pid 文件 JSON 化（pid+started_at+marker，120-122），判活三层校验：命令行含 infocenter+watch 且启动时刻差 ≤15s（184-209）；正常退出自删（212-229） |
| C-5+M5 裸 traceback | 已修 | TimeoutError/RemoteDisconnected 等 OSError→ApiError，URLError→带文案 SystemExit（client.py:107-112），except 顺序正确 |
| C-6 丢信 | 已修 | `_HEADLESS_RETRY` 四类不推进 cursor（906-915）；`_note_interrupted` 补 [自动] 中断节点、案卷 404 时先立案再补（1099-1148） |
| C-7 pause 排序 | 已修 | 打断严格先于两个只读 API（crew.py:599-601）；`_signal_group` 排除守护自身 pid（643,678-679） |
| C-8 登记失败降级 | 已修 | 采用强方案「登记成功才进收信循环」：3 次失败 return 1（854-868）；`crew status` 红色「办公室未登记」（461-464） |
| P2 / U-3 未动声明 | 属实 | 抽查 C-15（crew.py:965 仍 shlex 拼接）、C-16（cli.py:278-308 仍任意位剥离）、U-3（store.py:804-813/sync.py 未变），确未修 |

## 四、非阻断残留（建议列入后续，不影响本轮冻结）

- **N1（C-5 边角）**：`http.client.IncompleteRead` 等仅继承 HTTPException（非 OSError）的异常仍可能逃逸成 traceback，概率低。
- **N2（C-6 行为特征）**：重试无退避/无上限——headless 永久性失败时每轮（~2s+poll）重跑并反复写中断节点（本次复现 9 秒内 15 个）。丢失问题已修，建议后续加次数上限或指数退避。
- **N3（口径微差）**：events 无窗口路径 `unread_total=len(rows)` 不排除已被认领的信，宽于 `_unread_counts(None)`；仅守护自用快照字段，不影响完整性。
- **N4（C-4 迁移期）**：旧格式裸数字 pid 文件只判活不校验身份；新写入必带 started_at，迁移期过后自然消失。
- **P2 全量**（D-6/D-7/D-8、C-2/C-9~C-16、M2/M3/M4、U-3/U-5/U-6、L2 独立项/L3）按计划未修，声明属实。

## 五、结论

**P0 全绿（5/5 反驳复现通过）、P1 全绿（10 项对码已修）、18 项无一被反驳。可以冻结。**

建议 opencode 执行 D8 重启（crew down/up + 重启服务端加载新码），N1–N4 转 P2  backlog 登记。

—— kimi（M5 复核）
