# D 线（窗口 · 案卷）独立复核 · D1 数据层 + D2 命令与守护

> 复核人：kimi｜日期：2026-10-14
> 基线：`~/.opencode/plan/infocenter-窗口案卷应用-计划.md`（已定稿）；D3 `/ui` 真实 fetch 联调由 omp 收口中，本报告只覆盖 D1+D2。
> 实测：`python3 -W error::ResourceWarning -m unittest discover -s tests` 全量 **75/75 零告警全绿**；专项 `test_quality_window` 10/10、`test_quality_store`+`test_quality_mcp` 11/11、`test_d2_commands` 5/5 均真跑通过。

## 总体结论

**D1、D2 均判「通过」。** 计划 §6 改动清单两线全部落地，§4 设计语义与 §7 边界逐条吻合，§12 不越界（无中心调度、无鉴权改动、crew 全文零 `sync` 引用，B2 冻结约定未破）。共 4 条 ⚠️（1 条建议修复 + 3 条测试覆盖/提示级），均不阻塞验收，建议 D3 收口时一并处理。B3 轮遗留的 D1-1（MCP 工具数 10 vs 9，缺 `ic_case`）已修复确认。

## D1（cursor：store.py / httpapi.py / mcp_server.py / test_quality_window.py）

### §6 落地
- `deliveries` 加 `claimed_by`/`claimed_at` + 旧库迁移（PRAGMA+ALTER，`store.py:214-215、277-281`）；`cases`/`case_nodes`（append-only，全库唯一 INSERT `store.py:2247`）/`windows` 三表齐备。✅
- `X-Window` 头读取透传（`httpapi.py:483-494`，头优先、query/body 兜底）；`read`/`reply` 落认领（`httpapi.py:263-292`）；`GET /v1/cases|cases/{id}|windows|projects` 均在。✅
- MCP：窗口号 `INFOCENTER_WINDOW` 优先、否则 `w-{6hex}`（`mcp_server.py:20-24`）；events 带窗口查询；推送只提醒"未被其他窗口认领"的信（`store.py:484-491`）；since=0 补报同路径过滤；未读尾巴按窗口计数；只读工具 `ic_case` 已上线（工具数 10，D1-1 闭环）。✅

### 语义与边界
- 认领：先到先得条件 UPDATE（`WHERE claimed_by IS NULL OR = 自己`，冲突 409 `already_claimed`）；按 thread 传播归属（`_propagate_thread_claim`，`send()` 继承线程归属）；软认领 + force 接手；转办 `transfer_case`（节点"转办 → 交接给 Y"）。✅
- 状态与收局：状态集 pending/open/waiting/paused/aborted/done ↔ 待办/在办/待谁/暂停/中止/办结，无第七种混入；封卷只读（`_must_unsealed`，封卷后五种操作逐一 409 `case_sealed`）；离场主动上报 → 自动中止封卷；崩溃惰性补齐 `[自动]` 节点（PID 活性检查，触及才回填）；无任何超时扫描。✅
- 案卷格式：一行一节点（拒换行、限 200 字）、`[自动]` 双保险标记、正序。✅
- §7 边界：无窗口号四条路径（events/inbox/mark/reply）逐条断言行为同旧版、不立案；同窗口重读不算冲突；广播按 `(message_id, to_agent)` 隔离。✅
- §8 十条规格测试全部真实存在、语义对得上（逐条看过断言，非空断言）、运行绿。✅

### D1 发现
| # | 级别 | 问题 | 修法 |
| --- | --- | --- | --- |
| D1-a | 建议修复（测试缺口） | §8.1 前半句"旧库自动加列/建表"无专项测试：没有任何用例构造旧 schema 库再打开验证 ALTER/建表；迁移代码肉眼核对正确但无回归保护 | 加一条：手工建无 claimed_by/无 cases 表的旧库，Store 打开后断言列与表存在、旧数据可读 |
| D1-b | 建议修复（测试缺口） | §8.7"转办可交接"正向路径未测：仅断言封卷后 transfer 被拒，成功 transfer 的 owner 切换、节点内容、thread 归属 overwrite 无断言 | 对未封卷案卷调 transfer_case，断言 owner_window 变更与节点文案 |
| D1-c | 提示级 | `httpapi.py:178-179` D 类路由缺 X-Agent 时回落为 `human` 身份而非 401，任何本地调用都能以 human 名义操作案卷（计划未规定，localhost-only 下风险低） | 如需收紧，D 路由也强制 X-Agent；可后续 |

## D2（grok：client.py / cli.py / crew.py / test_d2_commands.py）

### §6 落地
- `client.request()` 窗口参数 + `X-Window` 头（`client.py:81、93-95`；显式值优先 → `INFOCENTER_WINDOW` 回退 → 匿名不发头），cli 全部命令经 `call()` 统一生效。✅
- `agents` 显示办公室（`cli.py:461-464`）；`ic case <thread>` 打印案卷（服务端 `_find_case` 按 id OR thread_id 双匹配），附 `--note/--status` 写回节点；`crew pause/resume/abort/close`；`crew down --only` 与 up 对称且只杀点名身份的 tmux 窗口而非整个会话。✅
- crew：守护启动生成窗口号写入 `os.environ`，`_run_headless` 用 `os.environ.copy()` 注入 headless 子进程；headless 每轮动作固定为"读案卷 → 顺藤读信 → 干活 → 写回一行节点"（`build_prompt()` 写明固定序列，心跳 prompt 同）；正常退出（含 SIGTERM/SIGINT）`finally` 主动 POST `/v1/windows/{id}/leave` 上报离场。✅
- §7 边界：无窗口号不发头、服务端按"没有窗口"处理，专项测试显式验证匿名不产生窗口记录。✅

### 五项测试逐条核对
全部真跑真断言：X-Window 头三态（显式/env/匿名）、`ic case/cases/windows` 真实子进程端到端、`crew pause/abort` + `down --only` 真假守护验证只杀目标、`crew watch` 真起进程 SIGTERM 后窗口变 `left`、prompt 文案含固定动作序列。

### D2 发现
| # | 级别 | 问题 | 修法 |
| --- | --- | --- | --- |
| D2-a | 建议修复（语义缺口） | §4.3/§4.8"暂停要快——打断当前那一轮"只完成一半：停触发 ✅（events/inbox 过滤已暂停 thread）、案卷断点行已写 ✅，但**在跑的 headless 子进程不会被真正中断**——`crew.control()` 只发 HTTP 状态变更，`_run_headless` 阻塞 `subprocess.run(timeout=1800)`，在跑的一轮最长可跑满 30 分钟，与案卷"当前轮被中断"文案不符 | pause 时按窗口 meta 已记的 pid（`store.py:1500`）向 headless 子进程发 SIGTERM，或守护每轮前自查 paused 状态 |
| D2-b | 可后续（测试缺口） | `resume`/`close` 未测；窗口号注入 headless env（`_run_headless` 的 env）无断言 | 补两调用 + 一个 mock subprocess 断言 env 含 `INFOCENTER_WINDOW` |
| D2-c | 提示级 | 窗口被 pause 后守护仍轮询、其他未暂停 thread 仍触发 headless——按"叫停一条线"语义是对的；若期望"暂停整个办公室"则需另议 | 语义解释问题，可后续 |

## 附：越界检查

- crew.py 全文 grep 零 `sync` 引用（import 仅 config/client/store/paths），B2"v1 不在 crew 调 sync"冻结约定未破。✅
- 无中心调度/排班、无鉴权改动、无名字空间改动。✅
- cli 新增 `cases`/`windows` 两个列表命令，§6 未列出但与 §5 管理台只读接口同源，属合理配套，非"明确不做"项。ℹ️

## 下一步建议

1. D2-a（pause 真打断）建议在本期修复——这是 §4.3 明文写的"快"，且案卷文案已经承诺了断点；
2. D1-a / D1-b / D2-b 三个测试缺口建议随 D3 收口一并补；
3. D1-c / D2-c 提示级，记录即可；
4. D3 `/ui` 联调完成后在本文件追加 D3 段即可出 D 线终审。

---

# 追加：D3 预复核（`/ui` 管理台代码 + 真机联调）· 2026-10-14 第二轮

> 范围：`infocenter/server.py` `/ui` 静态路由 + `infocenter/web/index.html`。D 线文档（README/SKILL/需求文档）omp 尚未合入，本段只签代码与联调，**D 线终审仍待文档与 D2-a/b 收口**。
> 实测：全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` **77/77 零告警全绿**（较上轮 +2，即 D1-a 旧库迁移专项与 D1-b 转办正向专项，两条均已落 `test_quality_window.py` 并真跑通过 ✅）。

## 结论

**D3 代码侧判「基本通过」，1 条必修显示 bug（D3-a）+ 3 条提示级。** 路由、字段映射、四操作、路径防护逐条核过；并起真服务（临时 home，随机端口）curl 冒烟全链通过：`GET /ui` 200 text/html；`%2e%2e` 编码穿越 400；`POST /v1/projects` 设定项目地址 201；`POST /v1/cases` 立案（含起始信 `to`+`body`）201；案卷 pause→paused / resume→open / close→done+sealed、封卷后再 pause 409 `case_sealed`；办公室 pause/resume/leave 三态正确翻转；三视图 `list_projects`/`list_windows`/`list_cases` 字段与 UI 取用逐一对应（`state_label`/`status_label`/`last_seen_local`/`case_count`/`active_cases` 均真实存在）。

## D3 发现

| # | 级别 | 问题 | 修法 |
| --- | --- | --- | --- |
| D3-a | **建议修复（显示 bug）** | `index.html:193` `const last = c.last_node \|\| c.last_note`：服务端 `_case_view` 的 `last_node` 是**节点 dict**（`store.py:2330`），凡有节点的案卷在案件列表的末行都渲染成 `[object Object]`（冒烟已复现：`last_node` 为 dict、`last_note` 才是文案） | 改为 `c.last_note \|\| (c.last_node && c.last_node.note) \|\| ""`，单行 diff 已发 omp |
| D3-b | 提示级 | 案件操作按钮显隐只看 `status_label` 是否恰为「在办/暂停」：`waiting` 显示为「待 xxx」、`pending` 为「待办」，这两类案卷在 UI 上**没有暂停按钮**（服务端允许暂停），只能从 abort/close 进 | 按钮判断改用原始 `c.status` 字段（open/doing/pending/waiting → 可暂停） |
| D3-c | 提示级 | 办公室「停用」按钮调 `leave`（`auto=true`），会**自动中止并封卷该窗口名下全部未封卷案卷**，无确认提示，误触代价大 | 加 `confirm()` 或改名「离场并封卷」；另 177–179 行 `onclick` 内嵌 window_id 未转义（当前 `normalize_window` 字符集无引号，不可注入，仅作加固记录） |
| D3-d | 提示级 | `server.py` `_serve_ui` 无 Cache-Control；`/ui` 前缀匹配会把 `/uixxx` 也送进静态分支（返 404 文本，无既有路由冲突，无实际影响） | 记录即可 |

## 越界与一致性

- `/ui` 只接 GET、不碰 JSON 路由、不进 store；单 HTML + 裸 `fetch`，零框架零外链，符合 §6「不引框架」。✅
- 管理台全部写操作经既有 D1 接口，无新服务端语义；立案走 `create_case`（`to`+`body` 同发起始信），与 D1 复核语义一致。✅
- curl 直接 `/ui/../store.py` 返回 401 是 curl 客户端先归一化了路径（实际请求 `/store.py` 落入 JSON 路由）；服务端侧编码穿越 `%2e%2e` 实测 400，防护有效。✅

## 终审待办（收口清单）

1. **D2-a 未修**：当前树 `crew.control()` 仍是纯 HTTP 状态变更 + `interrupted=True` 节点文案，`watch` 主循环与 `_run_headless`（`crew.py:597-667`）均无对在跑 headless 子进程的 SIGTERM 或轮前 paused 自查——案卷写着「当前轮被中断」但实际最长仍可跑满 30 分钟，文案与行为仍不符；
2. **D2-b 未补**：`test_d2_commands.py` 仍 5 项，无 resume/close 用例、无 headless env 注入断言；
3. **D3-a** 待 omp 合入单行修复；
4. **D 线文档未合入**：README 已有 B4 联邦章节 ✅，但 README / `skills/infocenter/SKILL.md` / `需求文档/01-需求整理.md` 三处均尚无窗口·案卷·管理台内容（已 grep 确认）；
5. 以上收口后补一次全量回归即可出 D 线终审。

---

# D3-a/b/c 收口复核（2026-10-14 15:33 心跳，kimi）

> 背景：omp 因上游 provider 卡死未能落 D3-a，coordinator 15:24 裁定改由 cursor 一次性收口 D3-a/b/c；cursor 15:25 落盘（`index.html` mtime 15:25:51），coordinator 15:29 核验 78/78。本段为 kimi 独立复核。

## 逐条核验

- **D3-a ✅**：`index.html` 案件列表末行改为 `c.last_note || (c.last_node && typeof c.last_node === "object" ? (c.last_node.note || c.last_node.line || "") : c.last_node) || ""`——dict 型 `last_node` 取 `.note`/`.line`，字符串型兼容旧服务端，`[object Object]` 消除；且 `last_note` 优先，与服务端 `_case_view` 同时供两字段的现状对齐。
- **D3-b ✅**：按钮显隐改按原始 `c.status`：open/doing/pending/waiting 可暂停、paused 可恢复、sealed/aborted/done 隐藏中止/办结；`rawStatus` 为空时回落 `status_label` 中文判断，兼容旧服务端。等待/待办两类案卷恢复暂停入口。
- **D3-c ✅**：`disableWindow` 加 `confirm()`，文案明示「停用将上报离场，并自动中止且封卷该窗口名下全部未封卷案卷」；`cid = escapeHtml(c.id)` 后内嵌 onclick（id 字符集无引号，属加固，与此前记录一致）。
- **测试 ✅**：新增 `test_13_ui_console_smoke_and_d3_abc`（`tests/test_quality_window.py:433`）——HTML 侧断言三条修复特征串且在、旧写法特征串不在；API 侧起真服务全链路冒烟（设项目→办公室上报→立案→断言 `last_node` 为 dict 且 `last_note` 为非空字符串→暂停/恢复→停用窗口级联封卷），真跑通过。
- **回归 ✅**：本机复跑 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **78/78 OK 零 ResourceWarning**（47s），与 coordinator 15:29 核验一致。

## 结论

**D3-a/b/c 收口通过，kimi 侧无追加意见。** D 线终审剩余前置缩为三项：D2-a（grok）、D2-b（grok）、D 线文档（omp）。三项收口后补一次全量回归即出 D 线终审。


---

# D2-a/D2-b 收口复核（2026-10-14 15:50 心跳，kimi）

> 背景：grok 于 15:41–15:42 落盘 D2-a/D2-b 收口（`crew.py` mtime 15:41、`test_d2_commands.py` mtime 15:42），coordinator 15:45 复跑 82/82 零告警。本段为 kimi 独立复核。

## 逐条核验

- **D2-a（pause 真打断在跑 headless）✅**：
  - `_run_headless`（`crew.py:757-813`）改用 `start_new_session=True` 独立进程组，注释明示「pause 只打断这一轮，不会把收信守护一起杀掉」；`_note_headless_pid`（542-560）把 `headless_pid` 写入/清出守护 meta。
  - `crew pause` 先调 `_interrupt_headless`（511-539）：按身份+项目过滤守护 meta，取 `headless_pid` 且**排除与守护 pid 相同**的情况、校验存活，经 `_signal_group`（494-508）发 `SIGTERM`——`killpg` 先、OSError 回落 `kill(pid)`；守护本身不动。文案「已打断 … 正在跑的 headless」与真实行为一致，原「interrupted=True 只改文案」问题消除。
  - watch 主循环每轮 `_window_paused` 自查（713-726）：paused 时跳过 headless 且**不推进游标**（`continue` 在 `cursor` 写入之前），未处理新信留待 resume 后处理；自查失败（连不上/异常）回落未暂停，fail-open 可接受。
  - `_run_headless` 对 `-SIGTERM`/`128+SIGTERM` 退出码单列「被暂停打断」文案（810-812），守护日志可读。
  - 实测：`test_pause_interrupts_running_headless` 真起 `sleep 30` headless、`crew.control pause` 后进程组被 SIGTERM、线程退出、meta 中 `headless_pid` 清除——通过。
- **D2-b（resume/close 用例 + env 注入断言）✅**：`test_d2_commands.py` 由 5 项扩至 **9 项**，新增 `test_crew_resume_and_close`（pause→resume→close 全链 + 封卷断言）、`test_run_headless_env_includes_window`（断言 `INFOCENTER_WINDOW`/`INFOCENTER_AGENT` 注入且 env 非 `os.environ` 本体）、`test_pause_interrupts_running_headless`、`test_watch_skips_headless_while_window_paused`（paused 时不触发 headless、游标不推进）。专项 9/9 通过。

## 提示级观察（不阻塞）

- `_run_headless` 中 `Popen` → `_note_headless_pid` 之间存在小窗口：若 `crew pause` 恰在该间隙执行，这一轮 headless 拿不到 SIGTERM 会跑完；下一轮起 watch 已见 paused 不再触发新 headless，影响仅限当前轮，可接受。
- `_window_paused` 每轮一次额外 HTTP 请求（超时 5s），规模下无实际影响，仅记录。

## 回归

本机复跑 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **82/82 OK 零 ResourceWarning**（51s），与 coordinator 15:45 核验一致。

## 结论

**D2-a/D2-b 收口通过，kimi 侧无追加意见。** D 线终审前置仅剩一项：**D 线文档合入（omp：README / `skills/infocenter/SKILL.md` / `需求文档/01-需求整理.md`，三处 mtime 14:53 / 02:43 / 15:01，自上一轮心跳起未变）**。文档合入后补一次全量回归即出 D 线终审。

---

# D 线终审（2026-10-14 20:48，kimi）

> 触发条件达成：D 线文档三处已齐——`README.md`（18:05，已提交）、`需求文档/01-需求整理.md`（18:15，已提交）、`skills/infocenter/SKILL.md`（**20:44 落盘，未提交**，工作树 diff +37 行）。复核快照：HEAD `537c72d`（20:34），工作树含 `web/index.html`（20:41）与 SKILL.md 未提交改动。

## 文档对码核验（逐项抽查，均一致）

- **SKILL.md**（最全，含 M5+ 口径）：窗口号 `X-Window`/`INFOCENTER_WINDOW` ✅、认领按线程传播 ✅、`ic_case` 只读 ✅、`ic case <id> --note --status` / `ic cases --workspace` / `ic windows --agent`（`cli.py:129-143` 逐项对上）✅、`crew pause|resume|abort|close` ✅、**封卷默认只读 + `POST /v1/cases/{id}/reopen` 显式重开留痕**（`httpapi.py:518-519` → `store.reopen_case`，与 §七「封卷与重开语义」裁定一致）✅、离场自动中止补 `[自动]` 节点 ✅、管理台三栏级联 + 立案指派 + 项目归档 + 网页起停守护（`/v1/crew`，`httpapi.py:178`）+ 全局痕迹（`/v1/activity`，`httpapi.py:202`、`store.py:2005`）✅。
- **README.md**：窗口/办公室/案卷术语表（85-87）✅、`ic case/cases/windows` 参数表与 `cli.py` 一致（`--owner`/`--state`/`--workspace`/`--limit` 均存在）✅、crew 收局四操作（212-215）✅、「封卷后只读（要改只能重开）」（477）与新口径一致 ✅。
- **需求文档**：R14 §七 三栏级联 + 写操作下移规格已记录（180-189）✅。

## 回归

本机复跑 `env -u INFOCENTER_AGENT python3 -W error::ResourceWarning -m unittest discover -s tests` = **144/144 OK 零 ResourceWarning**（71.5s，20:48 快照，含工作树在途改动）。注：M5 冻结（118）后 human 新增 25+ commit 但**测试数仍为 144**，§九 登记的「归档/`/v1/crew`/`/v1/activity`/reopen UI 待补测」依然成立（属 cursor 事后具名复核范围，非本终审阻塞项）。

## 提示级（不阻塞 D 线冻结，建议 pi 在 human 停笔后补一轮，或并入 cursor 事后复核）

1. **README §管理台（483 行）滞后**：仍写「三视图（办公室/案卷/项目）」与旧交互（主界面直接设定项目地址/立案），未反映 R14 已收口的三栏级联 + 写操作下移，也无重开/归档/新建项目/立案指派/起停守护/痕迹流等 M5+ 能力。
2. **需求文档同样缺 M5+ 簇**（19:58 后的 headless 上下文持久化、办公室状态筛选、`/v1/open` 等）。→ 即 PLAN §九「D 线文档范围 ❓」的答案：**SKILL.md 已覆盖，README / 需求文档未覆盖**。

## 结论

**D 线终审通过（按「封卷默认只读、可显式重开」现行口径）**：D1–D3 码冻结双签在先，D 线文档三处齐且抽查与实码一致，全量 144/144 零告警。残余事项（README/需求文档补 M5+、cursor 事后具名复核补测、SKILL.md 待提交）均已在 PLAN §九 有归属，不阻塞 M4 收口。若 human 后续 commit 再改 D 线语义，按 diff 复核即可。

---

# 追审 · README 21:35 增量（2026-10-14 21:3x 心跳，kimi）

> 背景：终审（20:48）提示级第 1 条「README §管理台滞后」之后，README 于 21:35 再改（diff +49 行，未提交）。本轮对该增量做对码抽验。

## 结论：增量全部与实码一致，终审提示级第 1 条**完全消解**

- HTTP 表新增 17 行逐一对上：`/v1/cases` 过滤参数（`workspace/status/owner/owner_window/limit`）✅、`POST /v1/cases` 带 `to`+`body` 发始信（`httpapi.py:494-497` → `create_case(to=..., body=...)`）✅、`nodes`/`pause|resume`/`abort|close`/`reopen`/`claim|transfer`（`httpapi.py:508-540`，含 `claim` 的 `force`、`transfer` 的 `to_window` 必填校验）✅、`/v1/windows` GET/POST 与 `/v1/windows/{id}` GET/PUT/DELETE + `pause|resume|leave`（`httpapi.py:557-608`，代码另收 `exit|stop|abort` 别名，为 README 超集）✅、`/v1/projects` 含 `archived` 布尔校验 ✅、`/v1/activity` limit ≤ 100（`store._limit` 1–100 硬夹）✅、`/v1/headless` / `/v1/headless/runs` / `/v1/daemons`（`httpapi.py:215-244`）✅、`/v1/agents/{name}/messages`（`httpapi.py:337-338`）✅、`/v1/restart`（`dry_run` 预览 + 日志 `~/.infocenter/restart.log`，`httpapi.py:246-287`，路径=`store.path.parent/restart.log` 属实）✅、`/v1/open`（非目录 400 + CSRF 守卫 `_check_write_security`，`httpapi.py:175/289-301`）✅。
- **F-1 热加载**段落（README §config.json）与 `crew.py:1005/1064` 注释及实现一致：每轮重读 config，`headless`/`heartbeat_mode`/`heartbeat_prompt`（及未显式传入的 `heartbeat`）免重启；清空 `headless` 优雅退出；`poll` 与显式 `--heartbeat` 仍需重启 ✅。
- §管理台改写为三栏级联 + 写操作下移 + 干预中枢（`#human`）+ 标题栏重启 + 工作痕迹流 + 编号点击复制 ✅（与 §九 M5+1–M5+8 及 19:19–20:34 簇对应）。

## 仍欠（不变，归 pi 在办）

- `需求文档/01-需求整理.md` 仍 18:15 未动（R14 节「写操作下移 待做」过期 + M5+ 簇未覆盖）——pi 21:21 自报在办（last_seen 21:26，pid 存活）。
- `skills/infocenter/SKILL.md` 20:44 落盘仍**未提交**。

## 备注

本轮未复跑回归：源码末写盘为 `index.html` 20:41（与 21:23 复跑 156/156 快照同源），21:35 仅动 README，回归结果不受影响。

---

# 追审 · 需求文档 23:03 增量（2026-10-14 23:07 心跳，kimi）

> 背景：终审提示级第 2 条「需求文档缺 M5+ 簇、R14 节过期」——pi 于 23:03 落 `需求文档/01-需求整理.md` 改写（diff +80/−16，未提交），§七 重写为「D 线 · 窗口 · 案卷 · 应用」：新增 R15/R16/R17 三节 + R14 改 ✅ + 新增 R18 M5+ 簇表。本轮逐条对码抽验。

## 结论：全部与实码一致，终审提示级第 2 条**完全消解**

- **R15 窗口号与线程认领** ✅：`X-Window` 头 / `INFOCENTER_WINDOW` env / `claimed_by` 按线程传播 / `force=true` 强接 / MCP 推送按窗口过滤——与 D1 冻结码一致（D1 双签时已逐条验过）。
- **R16 案卷** ✅：状态机（open/doing/waiting_for/paused/aborted/done+sealed）、`ic case/cases`、`crew pause|resume|abort|close`、`GET /v1/cases{,/{id}}`、`POST /v1/cases/{id}/pause|resume|abort|close|reopen`、封卷可重开留痕、离场自动中止补 `[自动]` 节点、MCP 只读 `ic_case`——与 §七 封卷重开裁定口径及实码一致。
- **R17 管理台** ✅：`/v1/crew`（`httpapi.py:178`）、`/v1/activity`→`store.recent_activity`（`:206-213`）、`/v1/headless/runs`（`:215`）、`/v1/daemons`（`:239`）、`/v1/restart`（`:246`）、`/v1/open`（`:289`）、`/v1/agents/{name}/messages`（`:337-338`）、`reopen`（`:522-523`）、hash 深链 `#human`/`#agent/<名>`（`index.html:1155-1157`）、引用 commit `5297ca4/20e04f3/fd8eba9/baec4e6/f12aed0/537c72d` 全部在 git log 中核实存在。
- **R14 改 ✅** ✅：「写操作下移 待做」旧表述已删，状态改「已全量收口（4bc9baa→58d188c，含 960dbfa/dfa9f24）」——`960dbfa`（.panel-head flex-grow 根因修复）与 `dfa9f24`（72vh 等高 + no-store）均在 log 中核实。
- **R18 M5+ 簇表** ✅：M5+1–M5+8 + F-1 九行，关键提交列与 git log 逐一相符；「18:05–22:17 · 54 commit」经 `git log --since=18:00` 实测 = 54，一致；「基线 156/156 零告警（kimi + coordinator 独立复跑确认）」属实；M5+1/5/7「回归 0 命中待 cursor 补测」口径与 PLAN §九 一致。

## 提示级（不阻塞，1 条）

- 文档头「最近更新：2026-10-14 **22:5x**」与实际落盘时间 23:03 略有出入（无实质影响，pi 若再动顺手修即可）。

## 备注

本轮未复跑回归：源码末写盘仍为 `crew.py` 22:15 / `store.py` 20:52 / `index.html` 20:41，与 22:30 已确认的 156/156 零告警快照同源，23:03 仅动文档。三处 D 线文档（README 21:37 / SKILL.md 21:39 / 需求文档 23:03）内容至此全部对码通过；**残余仅一项：三份文档均仍未提交（git status M），pi 在办**（23:02 自报「准备收口 D 线文档」，last_seen 23:06，未达停滞阈值，不催）。

---

# 追审 · `da3377c` headless_timeout 配置化（2026-10-14 23:45 心跳，kimi）

> 背景：23:35 心跳发现 crew.py 23:31 / 两测试 23:32 秒级在写，按纪律未跑半写回归。本轮（23:44）核查：human 已于 **23:34 提交 `da3377c`**（`HEADLESS_TIMEOUT=1800` 默认值 + `_headless_timeout_seconds()` clamp 10–7d，watch 两处 `_run_headless` 传入，+2 回归），此后无写盘，快照稳定 → 出追审。

## 结论：通过

- **对码**：`crew.py` +31/−5——配置读取（`headless_timeout` 缺省回落 1800、非数字回落默认、clamp 下限 10s 上限 7d）、watch 两轮调用点（新信触发 / 心跳触发）均传入 `htimeout`、`_run_headless` 签名加 `timeout` 参数并替换两处硬编码 `1800`、超时日志改动态秒数；与 F-1 热加载自洽（`entry` 每轮热读，`htimeout` 随之每轮重取）。
- **测试**：`test_hardening_sess.py` 新增 `HeadlessTimeoutTest` 2 项（默认/覆盖/非法值/下限 clamp 断言 + `timeout=1` 真跑 `sleep 30` 验证按时 SIGKILL 且 <15s 返回）；`test_quality_crew.py` 与既有 `test_hardening_sess.py` 的 `fake_headless` mock 签名同步加 `timeout=None`——调用方兼容性已覆盖。
- **回归**：独立复跑 `env -u INFOCENTER_AGENT python3 -W error::ResourceWarning -m unittest discover -s tests` = **158/158 OK 零告警**（基线 156 → +2 系本 commit 自带）。源码零漂移（末写盘 crew.py 23:31，同 HEAD）。

## 提示级（不阻塞，1 条）

- `headless_timeout` 为新增身份配置字段，README/SKILL/需求文档的配置字段表均尚未覆盖（文档三份已随 `90e6a1f` 提交、冻结于 `ba7f650` 时点）——属 M5+ 文档追补范围，归 pi/cursor 事后复核簇一并处理。


---

# 追审 · 文档 23:54 增量（`headless_timeout` 覆盖，2026-10-14 ~23:58 心跳，kimi）

> 背景：上条提示级指出 `headless_timeout` 未被三份 D 线文档覆盖。本轮核查：README.md 与 `需求文档/01-需求整理.md` 于 **23:54 再被编辑**（`90e6a1f` 提交后的新工作树改动，README +7/−3、需求文档 +4/−3），尚未提交。源码零漂移（末写 crew.py 23:31，同 158/158 快照），仅动文档 → 纯对码追审，不跑回归。

## 结论：通过——上条提示级**已消解**（待提交）

- **README**：config 字段表新增 `headless_timeout` 行（缺省 1800、钳制 10 秒～7 天、到期 SIGKILL 进程组）+ 热加载清单补 `headless_timeout` + FAQ 新增「headless 挂死了怎么办」+ 测试计数 156→**158**。逐条对码：`crew.py:89-90` 缺省 1800 ✅、`:425-430` clamp `max(10, min(secs, 7*86400))` ✅、`:1082` `htimeout` 在 F-1 每轮 `config.entry` 热读块内计算 ⇒ 「热加载、改后无需重启」属实 ✅、`:1255` `_run_headless(timeout=HEADLESS_TIMEOUT)` ✅。
- **需求文档**：R18 表头改「55 commit（截至 23:34）」✅（实测 `da3377c` 为 18:05 起第 55 个 M5 后 commit）、新增 **M5+9 行**（`headless_timeout` 可配 + 缺省/钳制/热加载/+2 回归）对码一致 ✅、基线行补「`da3377c` 后 158/158」——与我 23:45 独立复跑结果一致 ✅。
- **口径核对**：README 热加载段列 `headless`/`heartbeat_mode`/`heartbeat_prompt`/`headless_timeout`（及未显式传入的 `heartbeat`）——与 `crew.py:1074-1085` 热读块逐项吻合，无夸大。

## 提示级（不阻塞，1 条）

- 两处文档改动**仍未提交**（git status M），且 PLAN §九 表头仍写「54 commit（截至 22:17）」、未含 M5+9/`da3377c`——PLAN 侧已在 opencode 在办案卷 `m1a13b19d489c40271`（PLAN 滞后：da3377c 与 23:34 守护换新）覆盖，不重复上报；文档提交归 pi 收口节奏，不催。
