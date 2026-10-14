# _work/plan-maintenance.md · coordinator 维护记录

> 本文件由 opencode（收信守护，只读维护轮）维护。**不修改 PLAN.md / TODO-opencode.md / 生产文件**；需改计划或派单的事项记在此处「待交互 coordinator 裁定/应用」段。
> 依治理方案 item 3：守护产出写本文件，改计划/派单只发建议信给交互 coordinator。

## 2026-10-14 18:03 · 本轮收信裁定（2 封，均属本项目）

### 背景
- `m1a139d3b64e4dcaa4`（grok）：PLAN §八「P2 本轮不做」与已入库 P2 代码（commit `f175222`）不一致，请裁定追认还是回退。
- `m1a139d1881483f214`（kimi）：pi 解除待命信 1 小时未签收，建议按 §八 停滞登记改派 D 线文档。

### 裁定一：追认已落盘 P2，不退回（已回 grok `m1a139de19775cc465`）
- **依据**：① 已入库且全量绿——本轮复跑 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **144/144 OK 零 ResourceWarning**；② 落码方式合 `_work/final-review.md`「coordinator 直接改」，且服务 `d8-handoff.md`「等 P2 补齐再重启」意图，属有意落地；③ 真正瑕疵是文档滞后（PLAN §八 / d8-handoff 未同步），按改 PLAN 处置。
- **范围**：只追认已落盘且绿部分（D-6/D-7/D-8、C-2/C-9–C-16、C-14、M4、U-3/U-5/U-6、L2/L3、N1/N2/N3）。**M2（`crew.kickoff` 的 `read_text`/`request` 异常穿透）、N4（旧裸 pid 无 `started_at` 只判活）确认未做**，维持 backlog 本轮不做。
- **D8 前置加一条**：P2 晚于 kimi 17:19 复核落盘 ⇒ 需 kimi 补一段 P2 独立复核后方执行 D8。

### 裁定二：不改派，pi 未停滞（已回 kimi `m1a139de197bb56efc`）
- pi unread 已归零、activity 17:53「接单…落 D 线文档」；README D 线章节已合入 commit `5a1b1fc`（17:51）。§八 停滞登记触发条件不成立。唯余 SKILL.md / 需求文档 未落。

### 事实提醒
- 工作树仍有未提交改动：`infocenter/crew.py`（17:54，删 `pid=os.getpid()` 及注释）、`infocenter/sync.py`（17:57，补 U-3 降级注释），晚于 grok 的信；归属待确认，随 kimi P2 复核一并定。

---

## 待交互 coordinator 应用（改 PLAN / 派单 / 交接单）

> 守护只读，下列改动未执行，请交互 coordinator 窗口落地。

- [ ] **PLAN §八** P2 行：「P2（可修/记录，本轮不做）」→ 追加「**追认**：D-6/D-7/D-8、C-2/C-9–C-16、C-14、M4、U-3/U-5/U-6、L2/L3、N1/N2/N3 已由 coordinator 17:35–17:43 直接落码并复跑 144/144 绿；**M2、N4 维持 backlog 本轮不做**」。
- [ ] **PLAN §五 D8 行** 触发条件：「待 P2 补齐」→「P2 已落（除 M2/N4），就绪；执行前置增加 kimi P2 独立复核」。
- [ ] **PLAN §八 尾部**（D 线文档段）签收 pi 复活：README 已合入 `5a1b1fc`，余 SKILL/**需求文档**；移除「停滞登记改派」待触发表述。
- [ ] **`_work/d8-handoff.md`** 触发条件（第 4 行）：「待 P2 补齐…之后再执行」→「P2 已落（除 M2/N4）→ 可执行；前置＝kimi P2 独立复核通过」。
- [ ] **派单建议**：给 kimi 一段 **P2 独立复核**（对 `tests/test_hardening_p2.py` 覆盖的落盘项逐条对码 + 复跑 144），完成后 D8 才可执行。
- [x] **工作树归属**（18:26 闭合）：`crew.py`/`sync.py` 未提交改动系同一批 P2 收尾，已入库 `11b5445`；grok 18:08 回信确认，非别路。并入 P2 复核对象即可。

---

## 2026-10-14 18:06 · 第二路 headless 复核补记（同窗口，未重复回帖）

> 背景：同窗口另有前一路 opencode headless 已处理本轮（18:03 签收两封 + 回信 `m1a139de19775cc465`（grok）/`m1a139de197bb56efc`（kimi）；kimi 18:04 已结案封卷）。本路**不重复回帖**（无新裁定可加、避免 +1），仅补记独立复核发现的 2 条事实。

1. **全量 144/144 有环境前提**（复核上一路的「144/144 OK」）：
   - 从 **opencode headless 内直接**跑 → 会继承 `INFOCENTER_AGENT=opencode`，命中 `mcp_server.py:176` 的 MCP **locked** 分支（`required_agent=[]`），令 `tests/test_quality_mcp.py::test_line_json_no_notify_and_unlocked_agent_tools` 报 **1 红**（`'agent' not found in []`）——**非 P2 回归，系环境敏感**。
   - `env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **144/144 OK 零 ResourceWarning**（本轮实测，69.8s）。
   - ⇒ **今后无头复核跑全量须先清 `INFOCENTER_AGENT`/`INFOCENTER_WINDOW`**（与 F-2「重活」相关）；建议后续给该测试自身清 env（owner=cursor，`test_quality_mcp.py`）。

2. **D8 的重启动作已发生，PLAN/交接单未同步**（上一路 18:03 仍记「D8 待执行」）：
   - `ic serve --detach` pid **46322 @18:00:08**、crew 守护 **46356–46360 @18:00:10**（旧：服务端 64020@04:46、守护 27466–27469@13:12）。
   - 实测 `GET /ui` → **200**、`POST /v1/windows` → **400**（旧码均 404）⇒ 现网**已加载含 P2 的新码**。
   - ⇒ 上列「待交互 coordinator 应用」中 §五/§八 D8 行与 `_work/d8-handoff.md` **不应再写「就绪待执行」**，应改「**✅ 已执行 18:00**」；且「D8 前置＝kimi P2 复核」**不再是 gating**（码已上线）。kimi 的 **P2 独立复核仍建议做**，但目的改为「确认已上线的 P2 无回归」，非「解锁 D8」。

---

## 2026-10-14 18:12 · 第三路 headless（心跳，只读；未改 PLAN/未派单）

> 本路起于 17:42（早于 D8 18:00），收到的是**旧心跳模板**（「维护 PLAN.md / 派单」）——见末条 F-1。

- **no-op 确认**：无新信（2 封已由前路 18:03 裁定并签收，本轮仅签收）；`PLAN.md` / `TODO-opencode.md` 无新变化；「待交互 coordinator 应用」6 项仍待办（按上节：D8 相关行应改「✅ 已执行 18:00」、kimi P2 复核降为「确认无回归」）。
- **补一条独立验证事实**（与上节并列）：本路在**并发**场景（另一路孤儿全量 pid 38777、SIGTERM 不响应、滞留 18min）跑全量，曾在 `test_multi_project_daemons_isolated_by_workspace` 出 **1 红**、并曾挂起 >10min；`kill -9` 清场后该测单跑 **3/3 绿（~9.4s）**、全套件 **144/144 OK 零 ResourceWarning**。⇒ 再证系 **F-2 并发假象**，非 P2 回归。
- **F-1 已验证随 D8 消解**：本 headless（pid 36404）由**旧**守护 27468 派生、用旧模板；D8 已将守护换新（46356–46360），故**下一轮心跳起，守护应已加载只读 prompt**（拟按下一轮心跳确认）。

---

## 2026-10-14 18:26 · 第四路 headless（维护心跳，只读；未改 PLAN / 未派单）

### 收件箱
- `ic --as opencode inbox --full`：**0 未读**（`unread_total=0`）。无信可处理，未回帖、未发信。
- 四个项目守护（grok/kimi/cursor/opencode，pid 46356–46360）均在心跳中；名册成员 kimi/grok/cursor 收件箱亦 0 未读。

### 已完成（本轮观察到的、PLAN/TODO 之外的新落点）
- **R14 管理台 UI 一轮连提**（human/交互 coordinator 代改，`infocenter/web/index.html`）：18:05–18:24 共 **11 个提交** `f9a2503 → df1e8cf`。要点：三栏级联（项目→办公室→案件）、行内 ⚙️ 设置弹窗、**R14 写操作下移**（`be1aa56` 18:19，主界面去掉零散表单）、案件节点改四列表格（`4f0b6ae`）、项目/办公室设置页分区化 + 下级跳转（`df1e8cf` 18:24）。全部已入库，工作树对该文件干净。
- `README.md`：测试数 118 → 144（`f9a2503`，P2 后）。
- `TODO-grok.md`（18:08/18:23）、`TODO-kimi.md`（18:16）本轮新增心跳记录，**未提交**（工作树 `M`）；均为纯登记、无生产改动。
- D8 事实（沿用前两路）：服务端 + 5 守护已于 **18:00 换新码**；`GET /ui` 200、`POST /v1/windows` 400（旧码 404）⇒ 现网已载 P2。

### 待办变化
- **PLAN §九 R14 行已过期**：仍写「级联已实现（`4bc9baa`+）…『写操作下移』待做」；实际「写操作下移」已于 `be1aa56`（18:19）落地，且后续又迭两版。PLAN 停留在 18:15 快照。
- **PLAN §五 D8 行 / §八 D8 段仍写「⏸ 就绪待执行」**：与实测 18:00 已执行不符（前两路已记，仍未同步）。
- 「待交互 coordinator 应用」原 6 项：**工作树归属已闭合**（grok 18:08 回信确认 `crew.py`/`sync.py` 未提交改动系同一批 P2 收尾并入库 `11b5445`，非别路）；其余 5 项（PLAN §八 P2 行、§五/§八 D8 行、`_work/d8-handoff.md`、kimi P2 复核建议）**仍待办**。

### 风险（本轮新发现，重要）
- **R14 重写使 M5/P2 冻结测试转红**：全量 `env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **144 ran, 2 failures**（均 `test_hardening_p2.py::WebP2Test`）：
  1. `test_u5_select_case_request_token` — 断言 `const token = id;` / `if (state.selectedCase !== token) return;` **在现文件已不存在**（`git show 11b5445` 中该 guard 原在 260/263/278 行）。R14 把案件行点击改为直接 `openSettings("case", …)`，`loadCaseNodes(id)` **无 stale-response token 校验**，`selectCase` 成死代码 ⇒ **U-5（P2）的乱序响应防护被回退，属真实行为回退**（非仅测试文字）。
  2. `test_l3_action_urls_encoded` — 断言字面量 `"/v1/cases/" + encodeURIComponent(id) + "/pause"` 已不存在；现 `doCase(action)` 用 `"/v1/cases/" + encodeURIComponent(id) + "/" + action`（`index.html:378`），**L3 编码意图仍在**，属**断言过时**（改测试即可，非行为缺陷）。
- **观察环境**：复跑期间 `index.html` 又变两次（md5 变化、18:24 新提交），human 正**在飞编辑**该文件；上述红/绿为 **18:24 快照**（`df1e8cf`）。M5 冻结基线（144/144）**当前不成立**（142/144）。
- 说明：本轮**未**在并发重活下运行（无其他 `unittest` 进程），红项非 F-2 假象，可复现。

### 待 coordinator 裁定（本轮新增/更新；守护只读，未执行）
- [ ] **PLAN §九 R14 行**：改「写操作下移」为 ✅，并把 18:19–18:24 的相关收敛（级联 + 设置弹窗 + 写操作下移 + 节点四列 + 设置页分区）合并登记；确认 R14 是否视为收口（或仍留 backlog）。
- [ ] **R14 收尾是否需回归/复核**：`index.html` 在 M5/P2 冻结后多轮改写 → 是否安排一次只针对 web 层的复核（建议 owner=cursor，`tests/test_hardening_p2.py` + `test_quality_window.py`）并决定如何处置下面两条。
- [ ] **U-5 行为回退**：二选一——(a) 在 `loadCaseNodes` 恢复 stale-response token 守卫（保留 U-5 语义），或 (b) 承认交互模型变更、把 U-5 测试改写成对应新语义。**须先定 (a)/(b)**，否则全量恒红。
- [ ] **L3 断言过时**：更新 `test_l3_action_urls_encoded` 期望串以匹配 `doCase` 的 `"/" + action` 写法（编码意图已保持）。
- [ ] **PLAN §五/§八 D8 行 + `_work/d8-handoff.md`**：改「就绪待执行」→「✅ 已执行 18:00」；「D8 前置＝kimi P2 复核」不再是 gating（码已上线），kimi P2 复核目的改为「确认已上线 P2 无回归」。
- [ ] **PLAN §八 P2 行**：追加追认范围（沿用 18:03 裁定一）；M2/N4 维持 backlog。
- [ ] **测试数口径**：README 已写 144，若 U-5 处置改变用例数，需同步 README/PLAN。

---

## 2026-10-14 18:42 · 第五路 headless（维护心跳，只读；未改 PLAN / 未派单）

### 收件箱
- `ic --as opencode inbox --full`：**0 未读**（`unread_total=0`）。无信可处理，未回帖、未发信。
- 名册 agent 未读：kimi/grok/cursor/pi 均 0；human 5、coordinator 3、omp 5（非本仓待办）。
- 四个项目守护（grok/kimi/cursor/opencode，pid 46356–46360）仍在心跳中；仅 opencode 窗口 `w-3f522a` 自报本轮收信收尾。

### 已完成（本轮观察到的、PLAN/TODO 之外的新落点）
- **R14 管理台 UI 再迭两版**（human/交互 coordinator 代改，均已入库）：`1bd5ab1`(18:37) 立案加「指派」——不指派＝只建卷、指派＝`to`+`body` 同时发始信触发开工；`8f49c0e`(18:38) 项目栏加「新建项目」按钮 → 弹窗登记目录。含前一路记录的 `f9a2503→df1e8cf`，R14 自 18:05 起已连提 **13 个提交**。
- **进行中（未提交）**：`infocenter/store.py` / `infocenter/httpapi.py` / `infocenter/web/index.html`（mtime 18:39–18:40）新增**「项目归档」**能力——`projects.archived` 列 + 旧库 `ALTER TABLE` 迁移、`add_project(..., archived=)` 校验（非 bool 即 400）、`_collect_projects` 回传 `archived`、UI「显示/隐藏归档」+ 项目设置页归档/取消归档按钮。工作树对该三文件 `M`（含 `TODO-grok.md`/`TODO-kimi.md` 纯登记改动）。
- D8 事实（沿用）：服务端 + 5 守护 18:00 已换含 P2 的新码；`GET /ui` 200、`POST /v1/windows` 400。

### 待办变化
- `PLAN.md`（mtime 18:15）/ `TODO-opencode.md`（17:59）**无新变化**：前一路登记的「PLAN §九 R14 行过期」「§五/§八 D8 行仍写『就绪待执行』」**仍未同步**。
- 「待交互 coordinator 应用」段：工作树归属已闭合（`11b5445`），其余 5 项**仍待办**（未动）。

### 风险（本轮实测）
- **冻结基线仍不成立 = 142/144**：`env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **144 ran, 2 failures**（`test_hardening_p2.py::WebP2Test`），与 18:26 快照**同两条**、可复现、非 F-2 假象：
  1. `test_u5_select_case_request_token` — `const token = id;` 守卫在现 `index.html` 确不存在，`loadCaseNodes(id)` 无 stale-response 校验 ⇒ **U-5 乱序防护仍缺失**（行为回退未处置）。
  2. `test_l3_action_urls_encoded` — 字面量断言过时（现 `doCase` 用 `"/" + action`），编码意图仍在 ⇒ **仅测试需改**。
- **新增归档功能零测试覆盖**：`archived` 全链路（迁移 / 校验 / 过滤 / UI）**无对应用例**（`tests/` 无引用）；且旧库迁移分支与 `_collect_projects` 新字段未纳入任何断言。M5/P2「118/118→144/144 冻结」已因 web 层多轮改写而失效，回归网未同步。
- **F-2 并发重活**：本轮无其他 `unittest` 进程时复跑，仍 2 红，佐证红项非并发假象。

### 待 coordinator 裁定（本轮新增/汇总；守护只读，未执行）
- [ ] **U-5 行为回退（延续 18:26 未决）**：定 (a) 在 `loadCaseNodes` 恢复 stale-response token 守卫，或 (b) 改 `test_u5` 匹配新交互语义；**不定则全量恒红**。
- [ ] **L3 断言更新**：改 `test_l3_action_urls_encoded` 期望串以匹配 `doCase` 的 `"/" + action`。
- [ ] **归档功能收尾**：是否作为正式需求登记进 PLAN（§九 backlog 或新行）；**并派一次回归/复核**（建议 owner=cursor：补 `archived` 迁移/校验/过滤单测 + 复跑全量）。工作树未提交，归属与「是否随 R14 一并收口」待定。
- [ ] **PLAN §九 R14 行** + **§五/§八 D8 行 + `_work/d8-handoff.md`**：沿用 18:26 三项（R14 改 ✅ / D8 改「✅ 已执行 18:00」/ P2 行追认），均**仍未同步**。
- [ ] **测试数口径**：冻结基线已由 144/144 变 142/144，README 仍写 144；U-5/L3 处置后需同步 README/PLAN。

---

## 2026-10-14 19:01 · 第六路 headless（维护心跳，只读；未改 PLAN / 未派单）

### 收件箱
- `ic --as opencode inbox --full`：**0 未读**（`unread_total=0`）。无信可处理，未回帖、未发信。
- `inbox --status all`＝20 封，**多属 01_ICLR2027 项目（A3-dx3 / 豁免）**，非本项目。其中 kimi 18:54《PLAN 空缺/过期：R14 写操作下移已实现 + M5 后码漂移 8 commit》**已读**（本窗口前路签收式处理），其内容与本轮独立观察一致 → 归入下方「待 coordinator 裁定」，不重复回帖（无新信息、避免 +1）。
- 名册：kimi/grok/cursor/pi 收件箱均 0；四守护（pid 46356–46360）在心跳中。

### 已完成（本轮观察到的、PLAN/TODO 之外的新落点）
- **P2 冻结测试的两条红已由 human 提交闭合**（`e3200f0` 18:44）：
  - `test_u5_select_case_request_token` 取 **(b) 改测试**——断言由 `const token = id;` 守卫改为 `loadCaseNodes` + `openSettings("case"`（承认案件详情改在设置弹窗内加载、无分离面板竞态）；
  - `test_l3_action_urls_encoded` 期望串更新为 `"/v1/cases/" + encodeURIComponent(id) + "/" + action`（编码意图保持）。
- **R14 续迭 3 提交**（human/交互 coordinator 代改，均已入库）：`e3200f0`(18:44) 项目归档（`projects.archived` 列 + 旧库 `ALTER TABLE` 迁移 + `add_project(archived=)` 校验 + `_collect_projects` 回传 + UI 归档按钮/🗄图标/显示归档开关）；`4f0c129`(18:55) 网页起停守护（后端 `GET/POST /v1/crew`：status/up/down + 项目设置「启动/停止守护」按钮）；`58d188c`(18:59) 项目设置分区顺序修正（立案→守护）+ 启守护前无案卷弹确认。R14 自 18:05 起累计 **16 个提交**。

### 待办变化
- **18:26/18:42 记的「冻结基线 142/144」风险 ✅ 已闭合**：本路实测 `env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **144 ran, OK 零 ResourceWarning**（70.4s，`58d188c` 快照）。README 测试数 144 与之一致。
- 「待交互 coordinator 应用」段：原 6 项中 **工作树归属已闭（18:26）**、**U-5 / L3 已由 `e3200f0` 闭合**；**余 4 项仍待办**（PLAN §八 P2 追认行、§五/§八 D8 行、`_work/d8-handoff.md`、kimi P2 复核建议）。PLAN.md（mtime 18:15）/ TODO-opencode.md（17:59）**仍无新变化**。
- 工作树：`TODO-cursor.md` / `TODO-grok.md` / `TODO-kimi.md` 仅心跳登记（`M`）；`_work/plan-maintenance.md` 未跟踪。生产文件对 HEAD 干净。

### 风险（本轮实测）
- **两条新功能零测试覆盖**：`grep` 全 `tests/` —— `archived`（`projects.archived` 迁移 / `add_project` 校验 / `_collect_projects` 过滤 / UI）**0 命中**；`/v1/crew`（起停守护）**0 命中**。冻结基线虽 144/144，但新增能力未纳入回归网。
- **文档未覆盖新功能**：`README.md` 无「项目归档 / `/v1/crew` / 新建项目 / 立案指派」表述（惟 inbox `ic archive` 旧义）；`skills/infocenter/SKILL.md` 仍 **02:43** 未落；`需求文档/01-需求整理.md` 18:15 未再更新。pi 在途，写作范围是否覆盖 R14 新能力待定（kimi 18:54 亦提同点）。
- **F-2 并发重活**：结构性风险仍存（无互斥）；本轮无并发 `unittest`，红/绿可复现。

### 待 coordinator 裁定（本轮更新；守护只读，未执行）
- [x] **U-5**（18:26 起未决）→ **已闭合**：`e3200f0` 取 **(b)**，改测试匹配新交互语义（非恢复守卫）。请 coordinator 于 PLAN 记「U-5 按新交互语义收口」。
- [x] **L3**（18:26 起未决）→ **已闭合**：`e3200f0` 更新断言串，保留 `encodeURIComponent` 编码意图。
- [ ] **新功能是否登记 PLAN + 派回归**：`项目归档`（`projects.archived`）与 `/v1/crew`（起停守护）属 M5 冻结后新增，PLAN 未登记；是否登记（§九 backlog / 新行）并**派一次 web/后端回归**（建议 owner=cursor：补 `archived` 迁移/校验/过滤单测 + `/v1/crew` status/up/down 单测，并复跑全量）。
- [ ] **pi 文档范围**：其 D 线文档是否需覆盖「项目归档 / 新建项目 / 立案指派 / 守护启停」——若需，现补一句范围说明可免返工（kimi 18:54 建议）。
- [ ] **PLAN §九 R14 行**：改「写操作下移」为 ✅，并把 18:19–18:59 收敛（级联 + 设置弹窗 + 写操作下移 + 节点四列 + 设置页分区 + 新建项目 + 立案指派 + 项目归档 + 守护启停）合并登记；确认 R14 是否收口。
- [ ] **PLAN §一 M4 行**「M5 已冻结、码不再改」：与 M5 后 16 个提交（含新功能）冲突，需改述（kimi 18:54 提）。
- [ ] **PLAN §五/§八 D8 行 + `_work/d8-handoff.md`**（第 4 行触发条件）：改「就绪待执行 / 待 P2 补齐」→「**✅ 已执行 18:00**」；kimi P2 复核目的降为「确认已上线 P2 无回归」（不再是 gating）。
- [ ] **PLAN §八 P2 行**：追加追认范围（沿用 18:03 裁定一）；M2/N4 维持 backlog。
- [ ] **测试数口径**：README 已写 144（现与实测一致）；若上述新功能补测改变用例数，需同步 README/PLAN。

---

## 2026-10-14 19:49 · 第七路 headless（维护心跳，只读；未改 PLAN / 未派单 / 未改生产文件）

### 收件箱（`ic --as opencode inbox --full`：起手 3 封未读 + 本轮内又到 1 封＝4 封未读）
1. **human《检查案件》两连发**（均广播 to cursor/grok/kimi/omp/opencode/pi，均未签收）：
   - `m1a13a3a8a9faaa49e`（19:44，workspace=null）：「看看所有案件是否都办结了, 如没有, 请办结.」
   - `m1a13a3df8dcc17666`（19:48，workspace=/Users/robinlaw/Downloads/信息中心）：「检查所有经手的案件, 没有办结的办结.」
   - **非只读操作**（办结案卷＝写生产库状态），本只读守护**不执行**；已记入下方「待 coordinator 裁定」，**均不签收**（留 opencode 身份的交互 coordinator / 非只读窗口处理）。旁证：信对 cursor、pi 仍为未读、对 grok/kimi 已读 ⇒ 已在被处理中，无需重复。
2. **kimi `m1a13a2c1ccec9ec72`《D7 清理半失效…》** + 3. **kimi `m1a13a3a7a38c0c592`《Re: …裁定选 2》**（19:44）——D7 线程**已闭环**：交互 coordinator 19:41 裁定「选 2」（`omp` 系跨仓共享身份、守护重建属预期、不再反复清理、靠 `roster` 隐藏），PLAN.md D7 行已改注。**两封均已签收**，不回帖（无新信息，避免 +1）。

### 已完成（本轮观察；多为前几路「待应用」项的落地）
- **前路「待交互 coordinator 应用」6 项已落地**（交互 coordinator 19:1x，**工作树未提交**，`git diff` 逐条核实）：PLAN §八 P2 行补追认（D-6…N1–N3，**M2/N4 留 backlog**）；§五 D8 行 / §八 D8 段改「✅ 已执行 18:00 + 19:08/19:18 再重启，本轮不 `down/up`」；§九 新增 **M5+1…M5+7** 子表；§九 R14 行改 ✅ 收口；§一 M4 行改述为「M5 冻结后 23 commit 演进…144/144」；`_work/d8-handoff.md` 改「✅ 已执行」留档；`TODO-opencode.md` 补 19:1x 三条裁定。
- **D7 收尾**：`ce42ab6`(19:23) `/v1/agents` 加 `roster` 标记 + `0ccd3b7`(19:24) PLAN D7 标 ✅（均 human 提交）。
- **M5 冻结后 UI 继续演进**（human 直改，19:19–19:46 又 **14 个提交**）：agent 状态条(23106be 19:20)、点 agent 看信件往来历史 `GET /v1/agents/{name}/messages`+`Store.agent_messages`(0f128a6 19:28)、信件明细补项目/办公室/案卷(4d668e4 19:33)、human「标为已读」(84d4197 19:39)、案件编号显示+点击复制(fa7fff2/29ecd22 19:40–41)、工作痕迹筛选(2ce380e 19:44)+limit 200→100 修正(1041127)、human 发信加『项目』选择(d786f8d 19:46)。

### 待办变化
- **PLAN 数字过期**：§一/§九 仍写「25 commit 截至 19:18」，实际 19:18–19:46 再 +14；§九 M5+ 表只登记到 19:18 的 7 项。
- **M5+ 登记缺口扩大**：19:19 后的 `/v1/agents/{name}/messages`、`/v1/messages/{id}/read`(UI 入口)、工作痕迹筛选、案件编号复制、human 发信项目选择均**未登记**。
- **派单仍未发**：M5+ 事后具名复核（owner=cursor）按 PLAN 待 human 停笔后发；本轮**仍不发**（human 在飞编辑 web，避免对空复核）。
- **D 线文档**：`skills/infocenter/SKILL.md` 仍 **02:43**（未动）、`需求文档/01-需求整理.md` 18:15；pi 19:48 在办（activity「接手 D 线文档收尾」），未停滞。

### 风险（本轮实测）
- **新增能力回归 0 命中（复验确认）**：`grep tests/*.py` → `/v1/crew` **0**、`recent_activity`/`/v1/activity` **0**、`projects.archived` **0**（仅 `test_federation.py` 的投递态 `"archived"` 2 处、语义无关）、`/v1/agents/{name}/messages` **0**。⇒ M5+ 多项新能力**无回归网**，冻结基线 144/144 不再代表全量特性。
- **本轮未跑全量**：human 高频直改 web/后端（约 2 分钟一提交），**不复跑**（moving target + F-2 并发风险 + 避免打断 human）。最后可靠快照＝PLAN 19:1x 的 **144/144 零告警**；此后 14 个提交未验。

### 待 coordinator 裁定（含需非只读窗口执行者）
- [ ] **human 求办结案卷（`m1a13a3a8a9faaa49e` 19:44 + `m1a13a3df8dcc17666` 19:48，两连发）**：现 `ic cases` 9 份中 **4 份在办**——`m1a13a22a2d9193507`（PLAN §五/§七 案）/ `m1a13a19b952b0f871`（PLAN 补登）/ `m1a139d1881483f214`（pi 解除待命）/ `m1a139ff54405a80b5`（A3-dx3），余 4 中止 + 1 办结。**需交互 coordinator / 非只读窗口执行办结**（本只读守护不执行；两封信均未签收留待处理）。
- [ ] **PLAN 冻结基线 + M5+ 登记**：human 停笔后一次性对齐（commit 数、把 19:19 后新能力补进 §九 M5+ 表）。
- [ ] **M5+ 事后具名复核派单（cursor）**：补 `archived` / `/v1/crew` / `/v1/activity` / `/v1/agents/{name}/messages` 单测 + 复跑全量（待 human 停笔）。
- [ ] **D 线文档（pi）+ kimi D 线终审**（收口 M4）：SKILL/需求文档落笔后出终审。
- [ ] **D4**（human 拍板公开）——无变化。
- [ ]（低）**治理边界**：D7 闭环中 PLAN.md 的 19:41 裁定改注（工作树未提交）系**成员 kimi 直接落笔**，与 §六.3「只 coordinator 改 PLAN.md」有张力；建议交互 coordinator 复核归属后以自己名义提交/追认。

---

## 2026-10-14 20:05 · 第八路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **1 封未读**：grok `m1a13a49c755856da6`《案卷 m1a139ff54405a80b5 已封，但不是 D3 豁免》（**workspace＝01_ICLR2027 仓**，非本项目）。
  - **已闭环，不回帖**：同身份交互 coordinator 窗口已于 **20:05:01** 回信 `m1a13a4d392163edba`（逐条更正/确认 + 磁盘复验 + 回填 PLAN 第 25 次心跳）；本维护轮复核线程已 2 封成对，**不重复回帖**（无新信息、避免 +1）。已被签收，`unread_total=0`。
  - **grok 两处依赖核实结果（摘要，供记录）**：① **D3 三封（`m1a13958a51843ece6`/`m1a13985de150147ec`/`m1a139fce5f19a4105`）实为「已读未答」**——真相源＝`deliveries` 表，三封已于 **19:39 全部被 human 签收**；此前心跳以「出站副本 `read_at=null`」判未签收**判据无效**（发送方看不到收件人 read_at）。三线程各仅 1 封 ⇒ human **从未给 ①②③/(a)/(b)**。② 案卷 `m1a139ff54405a80b5` `status=done / sealed=1 / closed_at=19:56:53`、human 手动关闭（`auto:false`），封卷注无豁免信 id ⇒ **不构成 dx2 §1.4 第 7 条书面豁免**，**第 5/6/7 项继续不放行**。（属他仓，仅登记，不派单。）
- human《检查案件》两连发（`m1a13a3a8a9faaa49e` 19:44 / `m1a13a3df8dcc17666` 19:48）**均已读**，见下「待办变化」。

### 已完成 / 待办变化（本轮观察）
- **案卷盘点（`ic cases`）= 11 份，仅 1 份在办**：`m1a13a3a8a9faaa49e`《检查案件》在办（owner＝pi）；其余 8 办结 + 2 中止。⇒ human 19:44/19:48「没有办结的办结」**已基本落地**；kimi/pi activity 自报正在处理余项（kimi「检查全部案卷是否办结」、pi「盘点案卷并按指示办结」）。
- **PLAN.md 工作树改动（mtime 19:43，仍未提交）**：D7 行补「19:41 opencode 裁定选 2」注；D8 行「**每周长轮询**」→「**每次长轮询**」（**grok 19:30 请求**，已采纳）+ 补记 19:08:32 / 19:18:06 两次服务端重启。前述「待 coordinator 应用」的 §五/§八 D8 行、§八 P2 行**已落地**（19:1x–19:43）。
- **新提交（19:49 上轮快照 `d786f8d` 之后又 4 个，均 human 直改 UI）**：`3a990c5`(19:51) 标题刷新图标；`a3ad73f`(19:53) 案件栏状态筛选；`1b908de`(19:56) **human 升级为干预中枢**（发信含『所有项目广播』+ 全局在跑办公室/未结案卷可直接干预；`#human` / `#agent/<名>` hash 深链）；`1e7691f`(19:58) 各栏刷新 + **信件主题可点开看正文**（后端 `agent_messages` 补 `body`）。
- **M5 后提交数口径进一步过期**：PLAN §一/§九 仍写「**25 commit（截至 19:18）**」；实测 `git rev-list --since "17:19"` = **46**（`--since 19:18` = 20）⇒ 落后约 20 笔。
- **未提交生产改动（在途新功能）**：`infocenter/crew.py` 新增 `running_headless()`（扫守护 meta 的非空 `headless_pid`）+ `infocenter/httpapi.py` 新增 **`GET /v1/headless`**（配合干预中枢的「在跑 headless」视图）。`git diff --stat`：crew.py +39 / httpapi.py +10；生产文件对 HEAD 不再干净。

### 风险（本轮实测）
- **`/v1/headless` 与 `running_headless` 回归 0 命中**（`grep tests/*.py` 无引用）⇒ 又一项 M5 后新能力**无回归网**；M5+ 补测清单需追加此项。
- **human 仍在飞编辑**：`infocenter/web/index.html` mtime **20:05:05**（正在动）⇒ 本轮**不复跑全量**（moving target + F-2 并发风险 + 避免打断 human）。最后可靠快照＝19:1x 的 **144/144 零告警**，此后约 20 个提交未整体复验。
- **F-2 并发重活**：结构性风险仍在（无互斥），未变。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **human「办结案卷」收口**：仅余 `m1a13a3a8a9faaa49e`《检查案件》在办（owner＝pi），kimi/pi 正在处置；如需 coordinator 显式收口/中止，请明确（本只读守护不执行办结）。
- [ ] **PLAN 冻结基线 + M5+ 登记一次性对齐**（待 human UI 停笔）：更新 commit 数（25→现 ~46）；把 19:19–19:58 新能力（`/v1/agents/{name}/messages`、human 干预中枢 + 广播、hash 深链、案件状态筛选、各栏刷新、信件正文、**`/v1/headless`**）补进 §九「M5 冻结后迭代」表。
- [ ] **M5+ 事后具名复核派单（owner=cursor）**：补测范围追加 **`/v1/headless`/`running_headless`**；仍待 human 停笔后发（现 `index.html` 20:05 在飞）。
- [ ] **PLAN.md 工作树改动待提交**（D7 裁定注 + D8「每次长轮询」）——建议由交互 coordinator 以自己名义提交。
- [ ]（他仓，仅登记）**01_ICLR2027**：D3 三封「已读未答」、案卷 `m1a139ff54405a80b5` 办结但不构成豁免、第 5/6/7 项不放行——由该仓 coordinator 处置，本仓不派单。

---

## 2026-10-14 20:31 · 第九路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 1 封未读：grok `m1a13a62e9d6e49821`《D8 事实过期：本项目守护 20:11 已换新，长轮询不再带 X-Pid》（**workspace＝本仓**）。心跳只读性质，grok 明言「不请派 cursor」「提交数与 §九 M5+ 表停笔后一次对齐」。**已签收，不回帖**（无新动作可加，避免 +1）。

### 独立复核（全部证实 grok 判据，另补 1 处）
1. **守护已换新（PLAN 过期）**：`ps` 实测 5 守护 = **30603 grok / 30604 kimi / 30605 opencode / 30606 cursor / 30607 pi**，均 **@20:11:34** 起、cwd=本仓。加载 **`fd8eba9`（20:11:10）** 的 `crew.py`：`watch` 的 `GET /v1/events` query 仅 `since/wait/limit/workspace`，**无 `pid=os.getpid()`** ⇒ 这批守护长轮询**不再带 `X-Pid`**，PLAN「现存守护每次长轮询仍带 X-Pid」对这 5 个进程**不成立**。（办公室登记 `POST /v1/windows` 仍带 `body.pid=`，属一次性登记、非该缺陷——grok 判据成立。）
2. **服务端 pid 确已过期**：PLAN 写 91849；实测 **39090**（`lsof -iTCP:8787 -sTCP:LISTEN`，20:27:45 起）。沿用 19:30「随 human 重启、不追」，本路不 `crew down/up`。
3. **提交数过期**：HEAD **`f12aed0`（20:28:09）**；`git rev-list --since 17:19` = **51**（grok 20:28 记 50，因随后又落 `f12aed0`），`--since 18:05` = 49，`--since 19:18` = 25。PLAN §一/§九 仍写「**25 commit（截至 19:18）**」。另 `baec4e6`(20:15:53) 的 `running_daemons` 晚于守护启动（20:11:34）⇒ 这批守护内存无该函数，grok 判断成立。
4. **补 grok 未提**：`f12aed0`（20:28:09）新增**一键重启**——后端 `POST /v1/restart`（detached 脚本 + 日志 + `dry_run`）+ 标题旁「重启」按钮（确认 + 轮询 `/health`）。⇒ D8「折入下次自然重启」现已有 human 侧显式工具，PLAN 可据此改述。

### 待办变化
- **PLAN §五 D8 行两处过期**（加重前几路「D8 行待同步」）：① 守护仍写「46356–46360」→ 实为 **30603–30607 @20:11:34**；② 服务端仍写「现 pid 91849」→ 实为 **39090**；③ 「每次长轮询仍带 X-Pid」对现役守护**已失效**（缺陷随换新消解）。
- 工作树 `PLAN.md`（mtime 19:43，未提交，D7/D8 两行改动）**未新增**；生产文件对 HEAD 干净。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **PLAN §五 D8 行（更新，比前几路更重）**：改「守护仍 46356–46360」→「**守护 20:11:34 换新＝30603–30607，加载 `fd8eba9`，长轮询已不再带 `X-Pid`**（该缺陷随换新消解）」；「现 pid 91849」→「服务端随 human 重启漂移到 **39090（20:27）**，不追」；补 **`f12aed0` 一键重启 `POST /v1/restart`**。
- [ ] **PLAN §一 M4 行 / §九 M5+ 表**：commit 数 25→现 **51（since 17:19）**；补 20:05–20:28 新能力（`fd8eba9` headless 运行上下文 + `GET /v1/headless/runs`、`baec4e6` `running_daemons`+`GET /v1/daemons`、`76e7706` 项目行一键开关、`2a21b33` 案卷标题 headless 徽标、`f12aed0` 一键重启 `POST /v1/restart`）；**待 human 停笔一次性对齐**（grok 同请）。
- [ ] **M5+ 事后具名复核（owner=cursor）**：补测范围再追加 **`/v1/daemons`、`/v1/restart`（含 `dry_run`）、`/v1/headless/runs`、`_record_run`/`headless_runs`、`running_daemons`**；仍待 human 停笔后发。

---

## 2026-10-14 20:47 · 第十路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **0 未读**（`unread_total=0`）。无待签收、无可直接处理项。

### 已完成 / 待办变化（本轮观察）
- **关键路径推进：pi 交付 `skills/infocenter/SKILL.md`**（mtime **20:44:30**，diff **+30 / −7**，仍**未提交**）。至此 D 线文档三件套齐：`README.md`(18:05) / `需求文档/01-需求整理.md`(18:15) / **`SKILL.md`(20:44)**。新增「窗口与案卷（办事追踪）」段，覆盖窗口＝办公室、认领按线程传播、`ic_case` 只读、`ic case --note/--status` 写节点、封卷默认只读 + `POST /v1/cases/{id}/reopen` 显式重开、`crew pause|resume|abort|close`、离场自动中止、`/ui` 管理台。
  - **只读取样核验（全部对得上现码）**：`httpapi.py:518-519` `reopen`→`store.reopen_case`；`mcp_server.py` 有 `ic_case`(254/335) / `ic_thread` / `ic_archive` / `ic_activity`；`httpapi.py:422/521` 读/认领均有 `force`；`cli.py:131-132` `case --note/--status`；`cli.py:419` `crew pause|resume|abort|close`；`store.py:1804/1836` 离场自动中止封卷标 `auto`；`httpapi.py:285` `POST /v1/open`（本轮新提交）。⇒ **文档可信**，非空写。
- **新提交 2 笔**（上轮 HEAD `f12aed0` 20:28 之后）：`f5aaa84`(20:30:20 办公室栏状态筛选)、`537c72d`(20:34:04 **`POST /v1/open`** 打开项目文件夹，CSRF 守卫)。`git rev-list --since 17:19` = **53**（上轮 51），`--since 19:18` = 27。
- **服务端再换 pid**：`lsof` 实测 **40412**（**20:31:49** 起；上轮记 39090 @20:27）。沿用「随 human 重启、不追」。
- **守护未变**：仍 **30603–30607 @20:11:34**（cwd=本仓）。
- **案卷盘点（`ic cases`）= 13 份，在办仅 1**：`m1a13a62e9d6e49821`（owner=opencode，即上轮签收的 grok「D8 事实过期」核实信）；**6 办结 + 6 中止**。⇒ human《检查案件》两连发（`m1a13a3a8a9faaa49e` / `m1a13a3df8dcc17666`）**均已中止**，human「没有办结的办结」请求**已落地**（余下唯一在办件为本守护名下）。
- **成员心跳持续**：grok 20:41（已自行核实上轮事实：守护换新、服务端 40412、HEAD `537c72d`/53，未改 activity）；kimi 20:35（仍报 SKILL.md「02:43 未落」，**落后于 20:44 的落盘**——其下一心跳即可见）；cursor 20:33（待命，等事后复核派单）。

### 风险 / 未提交项（本轮实测）
- **D 线终审触发条件现已满足**：kimi 20:35 心跳所据「SKILL.md 未落」已过期；`SKILL.md` 20:44 落盘且抽验通过 ⇒ 待 human 停笔后 kimi 可出终审（本只读轮不派单）。
- **一批工作树改动未提交**：`PLAN.md`(4 行) / `TODO-cursor|grok|kimi|opencode.md` / `_work/d8-handoff.md`(D8 状态改「✅ 已执行」) / `infocenter/web/index.html`(**+198/−117**) / `skills/infocenter/SKILL.md`(+30/−7)。
- **human 仍在飞编辑**：`infocenter/web/index.html` mtime **20:41:42**（仍在 MU 窗口内）⇒ 本轮**不复跑全量**（moving target + F-2 并发风险）。最后可靠快照仍＝19:1x **144/144 零告警**；此后约 30 个提交未整体复验。
- **F-2 并发重活**：结构性风险仍在（无互斥），未变。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **kimi D 线终审**：`SKILL.md` 20:44 落盘、三件套齐、抽验通过 ⇒ 触发条件已满足；待 human 停笔后（或按 §九）交 kimi 出终审以收口 M4/M5+（本只读轮不派单）。
- [ ] **PLAN §五 D8 行同步（同前几路，更重）**：守护「46356–46360」→ **30603–30607 @20:11:34**（长轮询已无 `X-Pid`）；服务端 pid「91849」→ **40412 @20:31:49**（不追）；补 `POST /v1/restart`。
- [ ] **PLAN §一 M4 / §九 M5+ 表一次性对齐**（待 human 停笔）：commit 数 25→**53**；补 20:30–20:44 新能力 **`f5aaa84` 办公室栏筛选、`537c72d` `POST /v1/open`（打开项目文件夹）** + 前几路未登记项（`/v1/daemons`、`/v1/restart`、`/v1/headless/runs`）；登记 **D 线文档三件套落盘**。
- [ ] **M5+ 事后具名复核（owner=cursor）**：补测范围追加 **`/v1/open`（CSRF/非目录 400）** 及前几路清单（`/v1/daemons`、`/v1/restart` 含 `dry_run`、`/v1/headless/runs`、`running_daemons`、`running_headless`）；仍待 human 停笔后发。
- [ ] **工作树提交归属**：`SKILL.md` 系 pi 交付、`index.html` 系 human 在飞，`PLAN.md`/`TODO-*.md`/`d8-handoff.md` 待归属 ⇒ 建议交互 coordinator 复核后以自己名义分批提交。
- [ ] **human「办结案卷」**：现仅 **1 份在办**（`m1a13a62e9d6e49821`，owner=opencode，本守护上轮已签收核实）；其余全封卷。如需显式办结/中止该件，请 coordinator 明示（本只读守护不执行办结）。

---

## 2026-10-14 20:56 · 第十一路（收信处理：kimi D4 终审交卷）

### 收件箱（本仓 2 封）
- kimi `m1a13a778a7f939ce6`《Re: 【D线·D4 复核】窗口·案卷·应用》（起手 1 封，**workspace＝本仓**）。已核、已签收（`ic_read` 20:56）、已回信 `m1a13a7c54b59602c1`。
- grok `m1a13a7bc977a34f92`《D 线终审已落盘，PLAN §一/§七仍写未出》（本轮进行中 20:55:53 到达，**workspace＝本仓**）。已签收、已回信 `m1a13a7e1b35913679`：告知 D4 已收口 + PLAN §一/§七 同步待 human 停笔、本维护轮不改 PLAN/不派单。
  - grok 另点明 **20:52 起 3 处未派单的工作树生产改动**：`crew.py`（引号剥离 + 非对象日志）、`store.py`（项目锚点）、`httpapi.py`（`crew up` 目录不存在改 400）；实测工作树现已 `M` 此三文件 + 未跟踪 `tests/test_hardening_sess.py`（20:52–20:53）⇒ human/交互 coordinator 在飞，未纳入 D 线终审快照（20:48），归下一次整体复核。

### 处理
- **D4 交卷已核：D 线终审通过予以确认**（口径＝封卷默认只读、可显式重开）。
- **独立抽验（HEAD `537c72d`，逐条一致）**：`cli.py:129-143` case/cases/windows 参数；`cli.py:419` crew pause|resume|abort|close；reopen `httpapi.py:522`→`store.reopen_case`；离场自动中止补 `[自动]`（`store.py:2701`）；`/v1/crew`(178)/`/v1/activity`(206) 路由。`tests/` 共 **144** 个 test 方法；M5+ 六项 API（`/v1/crew`、`/v1/activity`、`archived`、`/v1/headless`、`/v1/restart`、`/v1/open`、`agent_messages`、`running_daemons`）回归 **0 命中** ⇒ kimi 判据成立。
- **一处新事实（已回 kimi）**：工作树 `README.md` mtime **20:50**（晚于 kimi 20:48 快照）已把 §管理台「三视图」改写为**三栏级联 + 写操作下移 + X-Window 接口表** ⇒ kimi 提示 1（README 滞后）在工作树**已消解**；残留仅「需求文档缺 M5+ 簇」。
- **案卷收口**：`m1a1390537991671bd` 20:52 曾被 `[自动]` 中止（主办离场惰性补齐、非实质）→ 本轮显式 **reopen(20:55) → 结论节点(20:56) → close/done(20:56)**，status=done、sealed。

### 待办（转交互 coordinator；本维护轮不提交/不派单）
- [ ] **SKILL.md（+30/-7）/README 提交**；**需求文档 `01-需求整理.md` 补 M5+ 簇**（kimi 提示 2）。
- [ ] **cursor 事后具名补测**（M5+ 六项 API）；仍待 human 停笔后发。
- [ ] **PLAN §一 M4 / §九 M5+ 表一次性对齐**（commit 数、D 线文档三件套落盘）。

---

## 2026-10-14 21:2x · 第十二路（收信处理：kimi §九 三项登记）

### 收件箱
- 起手 **1 未读**（`unread_total=0`）：kimi `m1a13a96af912c9513`《§九 基线独立确认：156/156 零告警；README M5+ 覆盖已补齐；需求文档/SKILL 仍欠》（**workspace＝本仓**）。已签收（`ic_read`）。无本线程案卷（`ic case` → not_found），按信本身办。

### 独立核验（coordinator，本轮）
- **全量复跑**：`env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **156/156 OK 零 ResourceWarning**（74.0s）——与 kimi 一致，human 自报 154/154 + F-1 `7dbccf2` 自带 +2 成立。
- **快照无漂移**：末 commit `7dbccf2` 21:04；生产/测试文件 mtime 最晚止于 `tests/test_hardening_sess.py` 21:03:13（同批），此后无写盘 ⇒ human 代码面已停笔。
- **残余文档抽验**：`需求文档/01-需求整理.md` mtime 18:15、§七 R14 末行仍「写操作下移 待做」（过期）；`skills/infocenter/SKILL.md` mtime 20:44、diff +30/−7、新增「窗口与案卷」段对码现码通过、仍**未提交**。
- **在办人手**：`pi` 21:21 activity＝「C线：核对 SKILL.md 案卷草稿与冻结码，补 需求文档 D 线章节」⇒ 两残余归 pi 在办。

### 已执行
- **PLAN §九 三项登记**：表头基线 154/154（自报）→ **156/156（独立确认）**；F-1 行 / M5+8 行「待独立复跑确认」→ **已确认（156/156，含 +2）**；`·` D 线文档行 → README ✅（20:50 覆盖 M5+）/ SKILL 待提交 / 需求文档欠（pi 在办）。
- **PLAN §一 M4 行同步**：commit 25→**53**（截至 21:04，human 已停笔）、基线 144/144→**156/156**（21:2x 独立复跑）。
- **PLAN §九 追写**「21:2x 补登」块；**TODO-opencode.md** 追写 21:2x 收信条 + F-1 backlog 行标「已确认」。
- **回信**：kimi 来信供 §九 登记、无待答问题 ⇒ **无新信息不回帖**（不 +1 附议）。

### 待办（转交互 coordinator / 未决）
- [ ] **工作树提交**：`SKILL.md`（+30/−7，pi 交付、抽验通过）/ `README.md`（+33，M5+ 覆盖）/ `PLAN.md` / `TODO-*.md` / `_work/*.md` 一批未提交 —— 建议待 `需求文档` 补完 + human 停笔确认后一并复核提交。
- [ ] **cursor 事后具名复核**（M5+ 补测：`archived` / `/v1/crew` / `/v1/activity` / reopen-UI / `/v1/restart` / `/v1/open` / `running_*`）—— 触发＝human **明确确认停笔**（kimi 观察「似已停笔」，本轮不据此派单）。
- [ ] **需求文档 `01-需求整理.md` 补 M5+ 簇 + R14 改 ✅**（owner=pi，在办）。

---

## 2026-10-14 21:45 · 第十三路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **0 未读**；**本轮进行中 21:44:53 到 1 封**：grok `m1a13aa8a783eea8ac`《D8 行在 21:28 补登后仍过期》（**workspace＝本仓**，`reply_to` 重开线程 `m1a13a62e9d6e49821`）。
  - **内容**：21:28 补登已把 §一/§九 写成「human 停笔、53 commit（截至 21:04）、156/156」，但 §五 D8、§八 引言、§九 19:10 段仍写 20:11 换新前的事实（守护 46356–46360、长轮询带 X-Pid、服务端 pid 91849）。
  - **须改 PLAN ⇒ 只读守护不执行**；**未签收**（留同身份交互 coordinator 窗口处置，避免误清其未读），记入下方「待 coordinator 裁定」。
  - **注意**：本守护窗口 `w-85674b` 查收件时已自动认领该线程（`claimed_by=w-85674b`）；交互 coordinator 接手时若被拦，按 SKILL 带 `force=true` 认领。投递仍 `unread`（`read_at=null`），coordinator 收件箱可见。

### 独立复核（本轮实测，证实 grok 三项判据）
- **PLAN 三处过期确认**：行 110（§五 D8）仍「守护仍 46356–46360…每次长轮询仍带 X-Pid…现 pid 91849」；行 171（§八 引言）「现 pid 91849」；行 254（§九 19:10 段）「现 pid 91849」+「human 未停笔期间不派单」——**三处均过期成立**。
- **实测现值**：本项目守护仍 **30603 grok / 30604 kimi / 30605 opencode / 30606 cursor / 30607 pi @20:11:34**（`ps` 核实，cwd=本仓）；服务端 pid **55074（21:05:10）**（`lsof -iTCP:8787`）；HEAD 仍 **`7dbccf2`（21:04:43）**。⇒ grok 判据全部属实。

### 已完成 / 待办变化（本轮观察）
- **kimi 完成 README 21:35 增量对码**：追审段入 `_work/verify-d.md`（21:38）——README 新 HTTP 表 17 行（`/v1/restart`/`/v1/open`/`/v1/headless(-/runs)`/`/v1/daemons`/`/v1/agents/{name}/messages`/`/v1/activity` limit≤100 等）+ F-1 热加载段 + 管理台改写段全部与实码一致 ⇒ **D 线终审提示级第 1 条（README 滞后）完全消解**。
- **工作树文档继续增补（未提交）**：`README.md`(21:37, +49)、`skills/infocenter/SKILL.md`(21:39, +37，较 20:44 再改)、`TODO-cursor.md`(21:39)、`TODO-kimi.md`(21:39)、`_work/verify-d.md`(21:38)。**代码面零漂移**（生产源文件 mtime ≤21:03，全在 HEAD 内）；**本轮未复跑全量**（零漂移，156/156 已于 21:2x 双验，无重跑必要）。
- **案卷（`ic cases`）= 17 份，在办仅 1**：`m1a13a62e9d6e49821`（owner=**opencode**，即本线程；grok 21:44 重开）。其余 13 办结 + 3 中止。
- **成员**：grok/kimi/cursor 21:44–21:45 活跃；**pi 最后活动 21:26**（约 19 分钟，守护 30607 存活，未达停滞阈值）。

### 风险
- **PLAN D8 事实层三处过期，本轮被 grok 二次报告**（首次 20:28 线程 `m1a13a62e9d6e49821`，现重开）——若据 §五/§八/§九-19:10 判 D8 状态会误导（守护 pid、X-Pid 缺陷、服务端 pid、「human 未停笔」四项均失真）。
- **M5+ 新能力回归 0 命中未变**（`/v1/crew`、`/v1/activity`、`/v1/headless(-/runs)`、`/v1/daemons`、`/v1/restart`、`/v1/open`、`archived`、`agent_messages`、`running_*`）⇒ 冻结基线 156/156 **不代表新特性覆盖**。
- **pi 约 19 分钟静默**（21:26→21:45），未达停滞阈值，观察；`需求文档` 仍欠（本轮唯一未收口的 D 线文档）。
- **工作树 10 文件未提交**（约 +434/−141，含 `infocenter/web/index.html` 未提交改动）——提交归属待 coordinator。
- **F-2 并发重活**：结构性风险仍在（无互斥），未变。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **（新，grok 21:44 `m1a13aa8a783eea8ac`）PLAN 三处 D8 事实同步**：行 110（§五 D8）「守护仍 46356–46360 / 每次长轮询仍带 X-Pid / 现 pid 91849」→「守护 20:11:34 换新＝**30603–30607**，长轮询**已不带 X-Pid**（缺陷随换新消解）」+ 服务端 pid「91849」→「**55074 @21:05:10**，不追」；行 171（§八 引言）pid 91849 → 55074；行 254（§九 19:10 段）pid 91849 + 「human 未停笔」→ **human 已停笔（末 commit 21:04）**。
- [ ] **需求文档 `01-需求整理.md` 补 M5+ 簇 + R14 改 ✅**（owner=pi，last_seen 21:26，在办）。
- [ ] **工作树提交**：`README.md`(+49) / `SKILL.md`(+37，pi 交付、抽验通过) / `PLAN.md` / `TODO-*.md` / `_work/verify-d.md` / `_work/d8-handoff.md` / `infocenter/web/index.html` 一批未提交 —— 建议复核后以 coordinator 名义分批提交。
- [ ] **cursor 事后具名复核（M5+ 补测）**：触发＝human **明确确认停笔**；代码面已停笔（21:04）但未获明确停笔确认，本轮**不据此派单**。
- [ ] **唯一在办案卷 `m1a13a62e9d6e49821`**（owner=opencode）：内容已核实闭环（D8 事实过期），可显式办结/封卷；本只读守护不执行。

---

## 2026-10-14 22:51 · 第十四路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **1 封未读**：grok `m1a13ae4778ecab19d`《【依赖过期】第35次心跳两处磁盘事实已过期：TODO-omp 已落盘，14:24:24Z 轮尚无 exit 行》（**workspace＝`…/01_ICLR2027` 仓，非本项目**）。
  - **磁盘实测核实（grok 两项判据均属实）**：
    1. ICLR 仓 `TODO-omp.md`：sha256 `c38eb5edfd5f65017090defb69f47e6cd41325eb501dff88363610dbb5541498`、**12,823 B**、mtime **22:47:12** ✓（与 grok 一致；PLAN 第 35 次心跳与 22:47:40 回信仍写 `e5180b04…`/8,201 B/20:19:37 ⇒ 确为过期）。
    2. `~/.infocenter/crew/omp--03084016.log`：**8,111 B / mtime 22:24:24**，末行 `[2026-10-14T14:24:24Z] 无新信，心跳 600s 到期，触发规划运行`，其后**无 exit 行** ✓（上一轮 `[14:14:23Z] headless 超时` 属更早一轮，不影响本条）；30 分钟上限 **22:54:24** 尚未到，不得记 exit 0。
  - **处置**：系**他仓（ICLR）台账**事务，须改该仓 `PLAN.md`（非本项目文件）⇒ 只读维护轮**不改、不据此重派**（grok 亦明言不请求改派）。**未签收**（留同身份交互 coordinator 窗口处置，避免误清其未读）。
    - ⚠️ 备注：本守护窗口查收时已认领该线程（`claimed_by=w-47e64e`）；交互 coordinator 接手若被拦，按 SKILL 带 `force=true`。投递仍 `unread`，coordinator 收件箱可见。opencode 另一窗口 22:51 activity 正为「第 35 次心跳完成…」，即该仓在办路径。

### 本项目（信息中心）本轮 **no-op**：无新信、无新提交、无漂移
- 本项目来信 **0**；`PLAN.md`(mtime 22:34:47) / `TODO-opencode.md`(22:35:00) **无新变化**（仍 22:4x 补登态）。
- HEAD **`ba7f650`**（无新提交）；5 守护 **78688 grok / 78689 kimi / 78690 opencode / 78691 cursor / 78692 pi @22:17:32**（`ps` 实测：cwd=本仓、无 `--heartbeat`）；8787 服务端 **55074**（`lsof -iTCP:8787`）—— **均与 PLAN §五 D8 / §八 一致，零漂移**。
- 工作树未提交 **10 文件**（`PLAN.md`+38 / `README.md`+49 / `TODO-{cursor,grok,kimi,opencode}.md` / `_work/d8-handoff.md` / `_work/verify-d.md` / `infocenter/web/index.html` 315 行改动 / `skills/infocenter/SKILL.md`+37），较 21:45 快照**无增减**。
- 案卷（`ic cases` = 20）：本项目相关仅 `m1a13ad4f909bca23f`（在办，D8 pid）、`m1a13ad233c674bdfb`（待 kimi）等既有件，**无新增待办**。

### 风险
- 无新增风险。沿用项：M5+ 新能力回归 0 命中（冻结基线不代表新特性覆盖）；F-2 并发重活无互斥；`web/index.html` 工作树未提交改版（+315）待并入 cursor 事后具名复核。

### 待 coordinator 裁定（守护只读，未执行）
- [ ]（他仓，仅登记）**01_ICLR2027**：第 35 次心跳台账两处磁盘事实过期（TODO-omp.md 现值 / omp 14:24:24Z 心跳轮尚无 exit 行，不得计入「连续 exit 0」）；**与本仓无关**，由 ICLR 侧 coordinator 改其 `PLAN.md`。
- [ ]（沿用，未变）本仓：工作树 10 文件未提交，建议 human 停笔确认后由 coordinator 分批提交；pi 的 `需求文档/01-需求整理.md` 补 M5+ 簇（在办）；cursor 事后具名复核（M5+ 补测）待 human **明确**停笔后发；PLAN §一/§九 commit 数与基线已对齐至 `ba7f650`（54 commit / 156/156）。

---

## 2026-10-14 23:10 · 第十五路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **0 未读**（本仓与 ICLR 仓均无新信）；`ic agents` 中 opencode `未读 0`。**无信可处理/回复**。
- 成员态（`ic agents`，23:08）：`kimi` 未读 0，activity「终审追审：对 pi 新落的需求文档 R15–R18/M5+ 段逐条对码抽验」（23:07）；`pi` 未读 0，activity「在补需求文档 R14 状态更新 + M5+ 增强簇，准备收口 D 线文档」（23:02）；`grok`/`cursor` 均在 ICLR 语境待命（未读 0）；`human` 未读 1（非本仓事务）。`omp` 未读 1（ICLR A5 预检，非本仓）。

### 本轮实测（相对 22:51 第十四路的新变化）
- **pi 已落 `需求文档/01-需求整理.md`**：mtime **23:03:29**，`git diff --numstat` = **+71/−9**（较 22:3x 前的 18:15 未动状态为新增）。内容：R14 行「写操作下移 待做」→ **✅ 已全量收口**（`4bc9baa`→`58d188c`，含级联+写操作下移+状态筛选+三栏等高）；新增 **R15 窗口号与线程认领 / R16 案卷 / R17 管理台 `/ui` / R18 M5 冻结后增强（M5+1…M5+8 + F-1）** 四节，逐条与 PLAN §九「M5 冻结后迭代」表对齐。⇒ **D 线文档最后缺口（需求文档）由 pi 补齐**（工作树未提交）。
- **kimi 正在终审追审**（23:07 activity）：对 pi 新落需求文档 R15–R18/M5+ 段逐条对码抽验——其 22:3x 前「需求文档缺 M5+」的提示级观察据此推进闭合。
- **成员 TODO 心跳更新（未提交）**：`TODO-grok.md`(+61，心跳至 22:56)、`TODO-kimi.md`(+37，心跳至 ~22:55 + 20:48 终审交卷条)、`TODO-cursor.md`(+9，心跳巡检至 23:00，均「0 未读、0 在办案卷、待 human 明确停笔后接 M5+ 事后复核派单」)。
- **代码面零漂移**：HEAD 仍 **`ba7f650`**（22:17:30，`ba7f650..HEAD` 为空）；`--since 18:05` = **54 commit**，与 PLAN §九表头一致。生产源文件 mtime 未越过 22:17（`index.html` 仍停在 20:41 的未提交改版 +198/−117）。**本轮未复跑全量**（此前 22:3x 已双验 156/156，代码零漂移无重跑必要）。
- **进程零漂移**：本项目 5 守护仍 **78688 grok / 78689 kimi / 78690 opencode / 78691 cursor / 78692 pi @22:17:32**（`ps` 实测，cwd=本仓、无 `--heartbeat`）；8787 服务端仍 **55074**（`lsof -iTCP:8787`，21:05:10）——均与 PLAN §五 D8 / §八一致。
- **案卷**：在办 **1 份** `m1a13ad4f909bca23f`（owner=**opencode**，题「§五 D8 守护 pid 再过期：22:17:32 已换为 78688–78692」）；其**内容已由 PLAN 22:4x 补登闭环**（D8 行已写入 78688–78692/无 `--heartbeat`/已含 `baec4e6`），仅未显式封卷。本只读守护不执行办结（留交互 coordinator）。

### 风险 / 未提交项（本轮实测）
- **PLAN 文档状态行已被 pi 推进而过期**：`PLAN.md`(mtime 22:34) §一 M4 行「余 `SKILL.md` / `需求文档`」与 §九 `·` 行「`需求文档/01-需求整理.md` 仍欠 / R14 节仍写『写操作下移 待做』」**现已过期**——pi 23:03 已补 R14 ✅ + R15–R18 + M5+ 簇。若据该两行判 D 线文档余量会失真。
- **工作树 12 项未提交**（`git status --porcelain` = 12）：`PLAN.md` / `README.md`(+45/−4) / `TODO-{cursor,grok,kimi,opencode}.md` / `_work/{d8-handoff,verify-d}.md` / `infocenter/web/index.html`(+198/−117) / `skills/infocenter/SKILL.md`(+30/−7) / `需求文档/01-需求整理.md`(+71/−9) + 未跟踪 `_work/plan-maintenance.md`。D 线三件套（README/SKILL/需求文档）**齐但仍未提交**。
- 沿用项未变：**M5+ 新能力回归 0 命中**（冻结基线 156/156 不代表新特性覆盖）；**F-2 并发重活无互斥**；`web/index.html` 未提交 UI 改版待并入 cursor 事后具名复核。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **（新，本轮核心）PLAN 文档余量行同步**：§一 M4 行「余 SKILL.md / 需求文档」与 §九 `·` 行「需求文档仍欠 / R14 仍写『写操作下移 待做』」→ 依 pi 23:03 落盘更新为「**三件套齐（README/SKILL/需求文档），均未提交，待提交**」；R14 已 ✅（pi 已在需求文档落 ✅，PLAN §九 R14 行 18:5x 起即为 ✅，仅 §九 `·` 行与 §一 M4 行的「仍欠/待做」措辞过期）。
- [ ] **工作树 12 项一次性提交**：D 线三件套（README/SKILL/需求文档）+ `PLAN.md`/`TODO-*.md`/`_work/*.md`，建议 human 明确停笔确认后由 coordinator 复核分批提交（§六.8：文档系 pi 交付、`index.html` 系 human 在飞）。
- [ ] **kimi 终审追审结论**：kimi 23:07 正在对 pi 新落需求文档 R15–R18/M5+ 逐条对码；结论出后可并入 D 线终审追审段（`_work/verify-d.md`）。
- [ ] **cursor 事后具名复核（M5+ 补测）**：仍待 human **明确**停笔后发；触发未变。
- [ ] **唯一在办案卷 `m1a13ad4f909bca23f`**（owner=opencode）：内容已闭环（D8 事实已入 PLAN 22:4x），可显式办结/封卷；本只读守护不执行。
- [ ]（他仓，仅登记）**01_ICLR2027**：omp/cursor 台账处 D3 待裁决状态；与本仓无关，由 ICLR 侧 coordinator 处置。

---

## 2026-10-14 23:25 · 第十六路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **1 封未读**：kimi `m1a13b00cd42189d8f`《D线文档已提交（90e6a1f）——PLAN §九 文档残余口径可收口》（**workspace＝本仓**）。已签收（`ic_read`，`read_at=23:24:13`）。
  - **内容**：纯登记、无待答问题——事实（human 23:19:13 落 `90e6a1f`，三份 D 线文档入库）+ 独立核对（committed stat 与工作树快照逐数一致、提交后 git diff 为空 ⇒ 入库版＝已审版）+ 3 处 PLAN 待更新提示（§一 M4 行 / §九 `·` D 线文档范围行 / §九 21:2x·22:3x 补登里的过期表述）。kimi 明言「无急迫性、一条登记即可」。
  - **回复判定**：无新信息、无待答 ⇒ **不回帖**（不发「已收悉」客套、不 +1）；PLAN 改动依只读约束不执行，记入下方「待 coordinator 裁定」。

### 独立核验（本轮实测，kimi 三项判据全部属实）
- **commit 存在与范围**：`git log -1` HEAD = **`90e6a1f`**（23:19:13，作者 `Robin9989Law`）；`git show --stat` = `README.md`(+45/−4)、`skills/infocenter/SKILL.md`(+30/−7)、`需求文档/01-需求整理.md`(+80/−16)，共 3 files/+146/−20 ✓（与 kimi 逐数一致）。
- **入库版＝已审版**：`git status --porcelain` 中 **不再含** `README.md` / `SKILL.md` / `需求文档/01-需求整理.md`——三文件工作树 diff 为空 ✓。
- **源码零漂移**：HEAD 仍为 `90e6a1f`，为纯 docs 提交；生产源文件未进本提交。本轮**未复跑全量**（纯文档提交、源码零漂移，156/156 已于 22:3x 双验，无重跑必要）。
- **PLAN 三处过期确认**：行 35（§一 M4 行）仍「仅余 D 线文档…余 `SKILL.md` / `需求文档`」；行 252（§九 `·` D 线文档范围行）仍「`SKILL.md` 20:44 落盘待提交 / 需求文档 仍欠」；行 265（§九 21:2x 补登）仍「需求文档 18:15 未动 / SKILL.md 未提交（pi 残余）」——**三处均已被 `90e6a1f` 消解，确为过期** ✓。
- **成员态（`ic agents` 23:24）**：`kimi` 未读 0、`pi` 未读 0（activity 23:18「D线文档收口：README/SKILL/需求文档(R15-R18)对码核对完毕，准备提交」）；`cursor`/`grok` 未读 0（均 ICLR 语境待命）；`human`/`omp` 未读各 1（非本仓事务）。

### 已完成 / 待办变化（本轮观察）
- **D 线文档三件套全部入库**（`90e6a1f`）：README / SKILL.md / 需求文档（R15–R18 + R14 ✅）——**M4 的 D 线文档前置由此收口**（kimi 独立核对入库版＝已审版，无需追审；kimi 名下 D 线复核链「预复核→终审→文档追审」全部闭合）。
- **待办变化**：原「工作树 D 线三件套未提交」项 **消除**（已入库）。
- **工作树剩余未提交（8 项 + 1 未跟踪）**：`PLAN.md` / `TODO-{cursor,grok,kimi,opencode}.md` / `_work/d8-handoff.md` / `_work/verify-d.md` / `infocenter/web/index.html`（human 未提交 UI 改版）+ 未跟踪 `_work/plan-maintenance.md`。**相对 23:10 第十五路：减少 3 项**（README/SKILL/需求文档转已入库）。
- **案卷（`ic cases`）= 20 份，在办仅 2**：`m1a13ad4f909bca23f`（owner=opencode，§五 D8 pid 事实）、`m1a13ad233c674bdfb`（待 kimi，F-1 `ba7f650` 口径补登，**已由 PLAN 22:4x 补登闭环**）；其余多办结/中止。

### 风险
- 无新增风险。沿用项：**M5+ 新能力回归 0 命中**（冻结基线 156/156 不代表新特性覆盖）、**F-2 并发重活无互斥**、`web/index.html` 未提交 UI 改版待并入 cursor 事后具名复核。
- **PLAN 文档余量行已过期**（行 35 / 252 / 265 仍写「SKILL 待提交 / 需求文档 仍欠」）：若据其判 D 线文档余量会失真，须交交互 coordinator 同步。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **（新，本轮核心）PLAN 三处文档口径同步**：`90e6a1f` 后，行 35（§一 M4 行「仅余 D 线文档…余 SKILL.md / 需求文档」）、行 252（§九 `·` D 线文档范围行「SKILL.md 20:44 待提交 / 需求文档 仍欠」）、行 265（§九 21:2x 补登「需求文档 18:15 未动 / SKILL.md 未提交」）→ 统一更新为「**D 线三件套（README/SKILL/需求文档）已全部入库 `90e6a1f`**」；据此 **M4 的 D 线文档前置可标齐**（kimi 文档追审链闭合）。
- [ ] **工作树剩余 8 项 + 1 未跟踪一次性提交**：`PLAN.md` / `TODO-*.md` / `_work/{d8-handoff,verify-d}.md` / `infocenter/web/index.html`（human 在飞改版）+ `_work/plan-maintenance.md`；建议 human **明确停笔确认**后由 coordinator 复核分批提交。
- [ ] **cursor 事后具名复核（M5+ 补测：`archived` / `/v1/crew` / `/v1/activity` / reopen-UI / `/v1/restart` / `/v1/open` / `running_*`）**：仍待 human **明确**停笔后发；触发未变。
- [ ] **在办案卷 2 份**：`m1a13ad4f909bca23f`（owner=opencode，D8 pid 事实已入 PLAN 22:4x，可显式办结/封卷）；`m1a13ad233c674bdfb`（待 kimi，`ba7f650` 口径补登已入 PLAN，可结案）。本只读守护不执行办结。
- [ ]（沿用，未变）**M3 公开 M3/D4**：待 human 拍板 `gh repo edit --visibility public`。
- [ ]（他仓，仅登记）**01_ICLR2027**：cursor/grok/kimi 仍在 ICLR 语境待命；与本仓无关，由 ICLR 侧 coordinator 处置。

---

## 2026-10-14 23:49 · 第十七路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **1 封未读**：grok `m1a13b19d489c40271`《PLAN 滞后：da3377c 与 23:34 守护换新》（**workspace＝本仓**）。已签收（`ic_read`）。
- **性质**：纯登记 + 对**交互 coordinator** 的改 PLAN 请求（grok 明言「请交互 coordinator 改，不要退回 human 的提交」「并进同一次修改，不必另案」）。本只读守护不执行 PLAN 改动。
- **回复判定**：无新信息、无待答 ⇒ **不回帖**（不 +1、不发「已收悉」客套）；事实与待办记入下方。

### 独立核验（本轮实测，grok 判据**全部属实**）
- **commit**：`git show -s` HEAD = **`da3377c`**（2026-10-14 **23:34:01**，作者 `Robin9989Law`）；`git show --stat` = `infocenter/crew.py`(31) / `tests/test_hardening_sess.py`(21) / `tests/test_quality_crew.py`(2)，3 files/+46/−8 ✓。
- **code**：`crew.py:90` `HEADLESS_TIMEOUT = 1800`；`crew.py:425` `_headless_timeout_seconds()` 缺省回落 + `max(10, min(secs, 7*86400))`（**10 秒–7 天**）；`watch` 每轮 `_headless_timeout_seconds(entry)`（`crew.py:1082`）热读 config entry；`_run_headless(..., timeout=...)`（`crew.py:1255`）超时对**进程组 SIGKILL**（`proc.communicate(timeout=timeout)` + `killpg`）✓。
- **回归（仅跑新类，未复跑全量）**：`env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest tests.test_hardening_sess.HeadlessTimeoutTest` = **2 OK（1.006s）**（`test_timeout_seconds_default_and_override` / `test_run_headless_kills_hung_process_at_timeout`）——与 grok 自报一致 ✓。
- **守护换新**：`ps` 实测 **44650 grok / 44651 kimi / 44652 opencode / 44653 cursor / 44654 pi @ 23:34:09**（ppid=1、cwd=本仓、命令行仅 `crew watch <名> --dir 本仓 --url 127.0.0.1:8787 --home ~/.infocenter --poll 30`、**无 `--heartbeat`**）⇒ 晚于 `da3377c`（23:34:01）8 秒拉起，**该批已载 `da3377c`** ✓。
- **服务端**：8787 仍 **55074**（`lsof -iTCP:8787`，**21:05:10**），沿用 19:30「随 human 重启、不追」口径不追 ✓。
- **计数**：`git rev-list --count ba7f650..HEAD` = **3**（`90e6a1f`/`b987102`/`da3377c`）；`--since 18:05` = **57**；**装载计数 158**（=156 冻结 + 新类 2）✓。

### PLAN 过期口径（四类，均已被 `da3377c`/23:34 换新消解；只读不改，转交互 coordinator）
- **行 35（§一 M4）**：仍「human 18:05 起直改…共 **54 commit（截至 22:17）**…基线 **156/156**…**仅余 D 线文档**：pi 的 README 已合入，余 `SKILL.md` / `需求文档`」→ 应改「**57 commit（截至 23:34）**、**158/158**（含 `da3377c` +2）、**D 线三件套（README/SKILL/需求文档）已全部入库 `90e6a1f`**」。
- **行 110（§五 D8）/ 行 171（§八引言）**：仍写「22:17:32 → **78688–78692**、载 HEAD `ba7f650`」→ 应补「**23:34:09 再换新为 44650–44654、载 `da3377c`**」（8787 **55074** 半句仍对）。
- **行 238（§九表头）**：仍「**54 commit（截至 22:17）** · 基线 **156/156**」→ 应改「**57 commit（截至 23:34）· 158/158**」。
- **行 252/263/265/269/271（§九 `·` 行与 21:2x/22:3x/22:4x 补登）**：仍「`SKILL.md` 20:44 待提交 / 需求文档 18:15 未动 / 停笔 22:17」→ 应统一为「**三件套已入库 `90e6a1f`；停笔时点顺延 22:17→23:34（`da3377c`）**」。

### 未变
- `infocenter/web/index.html` 仍 **20:41 起未提交改版**（+198/−117，纯 CSS 主题重写）——并入 owner cursor 事后具名复核范围。
- M2（kickoff）/ N4 仍 backlog，本轮不改。
- 工作树未提交（9 项 + 1 未跟踪）：`PLAN.md` / `TODO-{cursor,grok,kimi,opencode}.md` / `_work/{d8-handoff,verify-d}.md` / `infocenter/web/index.html` + 未跟踪 `_work/plan-maintenance.md`。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **（本轮核心）PLAN 四类口径同步**：commit **54→57**、基线 **156→158**、停笔 **22:17→23:34**、守护 **78688–78692→44650–44654（载 `da3377c`）**；§一 M4 / §九 `·` / 21:2x / 22:3x 的文档余量措辞统一为「**D 线三件套已全部入库 `90e6a1f`**」（并进第十六路同一次修改，**不另开案卷**——应 grok「不必另案」）。
- [ ] 沿用未变：工作树 9 项 + 1 未跟踪待 human 明确停笔后提交；cursor 事后具名复核（M5+ 补测）触发未变；M3/D4 公开待 human 拍板；在办案卷 `m1a13ad4f909bca23f` / `m1a13ad233c674bdfb` 可结案（本只读守护不办结）。

---

## 2026-10-15 00:08 · 第十八路 headless（维护心跳，只读；未改 PLAN / TODO / 生产文件；未派单）

### 收件箱（`ic --as opencode inbox --full`）
- 起手 **9 封未读**，按 workspace 分两类：
  - **本仓（信息中心）5 封 —— 已签收**（`ic_read`，`read_at=00:08:39`）：
    - pi `m1a13b06bf93676714`《[C/D线文档收口] 需求文档+SKILL 已提交 `90e6a1f`，PLAN §九 相应条目过期》；
    - grok `m1a13b03fdfa961026`《更正：三份文档已入库 `90e6a1f`，§七 150–151 仍写未落笔》；
    - kimi `m1a13b189b0c2f4ea2`《PLAN 补登：`da3377c` headless_timeout（M5+9）+ 基线 158/158 独立确认》；
    - kimi `m1a13b24e15bef9d7d`（同一线程追认补报：README/需求文档 23:54 增量对码通过，残余「未提交 + PLAN §九 表头仍 54」）；
    - pi `m1a13b284af436d546`《[C线] `da3377c` 追码已闭合：文档对齐（`bac9f2d`）+ 158/158 独立复跑；PLAN §九 口径需再刷新》+ CHANGELOG backlog。
    - **五封均为事实登记 / 求改 PLAN，无待答问题**；改 PLAN 属交互 coordinator（约束 ③/④ 禁改），故**不回帖**（不发「已收悉」客套、不 +1），据记录入下方。
  - **他仓（`01_ICLR2027`）4 封 —— 留未签收**（避免误清 ICLR 侧交互 coordinator 的未读；非本仓事务，仅登记）：
    - grok `m1a13b2a0b115b2ec0`（D3 ①(a) 新 DRAFT 重盖通过，绑定 `345073ac`；不放行 freeze / A5；指出 ICLR PLAN 两处过期）；
    - cursor `m1a13b29e7d2c93e31`（Task #3 第 1 步 CLI 转发 `--gated-override` 补丁 + 单测完成，73/73；待放行后跑 `select→materialize→devpkg`）；
    - kimi `m1a13b26a449f55b95`（DRAFT 单次修订**终版** `345073ac` / 4,239 B）；
    - kimi `m1a13b25623ea855fa`（中间版 `e3cc969d…`，**已由上一封明示作废**）。
- 收件后 opencode 未读剩 **4**（全为 ICLR 4 封）。

### 独立核验（本轮实测，相对第十七路 23:49 的新变化）
- **HEAD = `369d1c4`**（2026-10-15 **00:02:52**，作者 `Robin9989Law`）：`git show --stat` = `TODO-pi.md`（+20/−1，纯台账）。前一笔 **`bac9f2d`**（00:01:59，pi）= `README.md`(+7/−3) + `需求文档/01-需求整理.md`(+4/−3)。**`ba7f650..HEAD` = 5**；`--since 18:05` = **59**。
- **C 线追码已入库、文档缺口闭合**（pi 自报属实，抽查）：
  - `README.md:327` 新增 `headless_timeout` config 行（缺省 1800s、钳制 10s–7d）；`:329` 热加载清单已含 `headless_timeout`；`:569/:573` FAQ 已补。
  - `需求文档/01-需求整理.md`：`:237` 基线补「`da3377c` 后 158/158 零告警」；`:250` 新增 **M5+9** 行（headless 墙钟上限可配）；`:252` 状态行改「全部已入库，最新基线 158/158」。
  - 三件套（README/SKILL/需求文档）**均已入库且追码到 `da3377c`**；SKILL.md 不含 config 字段清单，本轮无需改（pi 已说明，非遗漏）。
- **代码面自 23:34 零漂移**：`da3377c` 之后两笔皆 docs/台账，无源码改动；基线 **158/158**（pi + kimi 双独立复跑确认，156 冻结 + `da3377c` 自带 `HeadlessTimeoutTest` +2）。本路**未复跑全量**（纯文档提交、源码零漂移，上一路已验新类 2 OK）。
- **守护零漂移**：`ps` 实测本仓 5 守护仍 **44650 grok / 44651 kimi / 44652 opencode / 44653 cursor / 44654 pi @23:34:09**（ppid=1、cwd=本仓、命令行仅 `--poll 30`、**无 `--heartbeat`**）⇒ 载 HEAD 时代的 `da3377c`（无源码变化）。**8787 服务端仍 55074（21:05:10）**，沿用「随 human 重启、不追」口径。**均与第十七路一致，无再换新**。
- **工作树未提交 = 8 已改 + 1 未跟踪**：`PLAN.md`(mtime 22:34) / `TODO-{cursor,grok,kimi,opencode}.md` / `_work/{d8-handoff,verify-d}.md` / `infocenter/web/index.html`(+198/−117，mtime 仍 20:41) + 未跟踪 `_work/plan-maintenance.md`。较第十七路**无增减**（新增的 pi 文档/台账均已入库，非工作树残留）。
- **CHANGELOG backlog 属实**（pi 第 5 条）：`CHANGELOG.md` `[Unreleased]` 仍停在「测试 16→33 / R10 联邦」，版本号仍 **0.4.0**，落后 0.4.0 后 59 commit；已有 tag `v0.5.0-dev.0`。全量补账依赖 D4/发版指令，**非阻塞**。
- **成员态**：`kimi` 未读 0（23:5x 独立复跑 158/158 + 追审入 `_work/verify-d.md`，mtime 23:59:57 实测）；`pi` 未读 0（C 线追码收口）；`grok`/`cursor` 本仓无在办（均 ICLR 语境）。**无停滞、无空转**（唯一闲人态＝待 human 停笔后接 cursor 事后具名复核）。

### 已完成 / 待办变化（本轮）
- **已完成**：`da3377c`（headless_timeout，M5+9）+ 文指定稿追码（`bac9f2d`）全部**入库**；C 线追码链（README/SKILL/需求文档 → 对码 → 独立复跑 158/158 → 台账）**闭合**；kimi 的「headless_timeout 文档未覆盖」提示级观察**闭合**。
- **待办变化**：M5+ 簇**新增 M5+9**（PLAN §九 尚未登记，见下）；human 停笔判据**顺延至 00:02（`369d1c4`，纯台账）**——源码停笔仍止于 23:34（`da3377c`）。
- **未变**：cursor 事后具名复核（M5+1/5/7 补测 + 全量复跑）**仍待发**（触发＝human 明确停笔）；`index.html` 未提交 UI 改版仍待并入该复核；M2/N1–N4/F-2 仍 backlog。

### 风险
- **无新增风险**。沿用项：**M5+ 新能力回归 0 命中**（158/158 冻结基线不代表 M5+1/5/7 补测覆盖）；**F-2 并发重活无互斥**；`web/index.html` 未提交 UI 改版待并入 cursor 事后具名复核。
- **PLAN 过期面进一步扩大**（非新风险，但口径差继续拉大）：§九 表头仍「54 commit（截至 22:17）· 156/156」，实际 **59 commit（截至 00:02）· 158/158 · M5+9 未登记**；§七 150–151、§一 M4 行、§九 `·` 行 / 21:2x–22:4x 补登的「文档仍欠/未提交」措辞**均已过期**。

### 待 coordinator 裁定（守护只读，未执行）
- [ ] **（本轮核心，承第十七路）PLAN 口径一次性同步**（建议并进同一次修改、不另开案卷）：
  1. **计数/基线**：§九 表头与 §一 M4 行「54 commit（截至 22:17）· 156/156」→「**59 commit（截至 00:02 `369d1c4`）· 158/158 零告警**」；停笔时点：**源码止于 23:34（`da3377c`）**，其后 `bac9f2d`/`369d1c4` 为 pi 纯文档/台账。
  2. **新增 M5+9 行**：`da3377c` headless 墙钟上限可配（`headless_timeout`，缺省 1800s、钳制 10s–7d、热加载、超时 SIGKILL 进程组，+2 回归）。
  3. **文档余量措辞统一**：§一 M4 行 / §七 150–151 / §九 `·` 行 / 21:2x–22:4x 补登的「SKILL.md 待提交 / 需求文档 仍 18:15 未动 / 仍欠」→「**D 线三件套（README/SKILL/需求文档）已全部入库 `90e6a1f`，并已随 `bac9f2d` 追码 `da3377c`（含 M5+9、158/158）**」。
  4. **守护/服务端**：§五 D8 / §八引言补「**23:34:09 再换新为 44650–44654、载 `da3377c`**」（8787 仍 55074 半句不变）。
- [ ] **CHANGELOG 补账**（pi 第 5 条，非阻塞）：`[Unreleased]` 落后 0.4.0 后 59 commit、版本仍 0.4.0；建议**随 `0.5.0` release-cut 一次性补账**，依赖 D4/发版指令 —— 交 coordinator 编排，届时知会 pi。
- [ ] **cursor 事后具名复核（M5+ 补测）**：仍待 human **明确**停笔后发；**对空口径应取 `da3377c`/`bac9f2d` 之后快照**（pi 建议）。触发未变。
- [ ] **工作树 8 项 + 1 未跟踪一次性提交**：`PLAN.md` / `TODO-*.md` / `_work/{d8-handoff,verify-d}.md` / `infocenter/web/index.html`（human 在飞改版）+ `_work/plan-maintenance.md`；建议 human 明确停笔确认后由 coordinator 复核分批提交。
- [ ] **在办案卷**：`m1a13ad4f909bca23f`（owner=opencode，D8 事实）/ `m1a13ad233c674bdfb`（set kimi，`ba7f650` 口径）内容均已入 PLAN，可显式办结/封卷；本只读守护不执行。
- [ ] 沿用未变：M3/D4 公开待 human 拍板。
- [ ]（他仓，仅登记，**不签收**）**`01_ICLR2027`** 4 封：D3 ①(a) 链已到「grok 重盖通过（绑定 `345073ac`）→ kimi 终版就绪 → cursor Task#3 第 1 步完成」；**待放行 cursor 跑 `select→materialize→devpkg`**（放行信属派单，本守护不发）。与本仓无关，由 ICLR 侧交互 coordinator 处置。
