# 信息中心 · PLAN（团队任务分解）

> 项目目录：`/Users/robinlaw/Downloads/信息中心`
> coordinator：**opencode**（本文件由其独家维护；成员进展只写自己的 `TODO-<名字>.md`）
> 基线版本：0.4.0（commit `8daf1bc`，测试 16/16 绿）
> 建立日期：2026-10-14｜任务口号：**综合推进 = 质量加固 + 跨机联邦 + 发布就绪**

---

## 〇、团队名册与文件所有权（单一写者）

| 身份 | 角色 | 负责轨道 | 可写的生产文件 |
| --- | --- | --- | --- |
| **opencode** | coordinator | 裁定 / 验收 / PLAN 维护 | `PLAN.md`、`TODO-opencode.md`、`_work/plan-maintenance.md` |
| **cursor** | 成员 | **A 线**：代码质量与测试加固 | `infocenter/store.py`、`infocenter/httpapi.py`、`infocenter/mcp_server.py`、`tests/test_quality_*.py`（新建）、`_work/a1-*.md`（`crew.py` 心跳区临时授权已完成并交回 grok） |
| **grok** | 成员 | **B 线**：R10 跨机联邦 | `infocenter/sync.py`（新建）、`infocenter/client.py`、`infocenter/config.py`、`infocenter/cli.py`、`infocenter/crew.py`、`tests/test_federation.py`（新建）、`_work/federation-*.md` |
| **pi** | 成员 | **C 线**：发布工程 | `README.md`、`skills/infocenter/SKILL.md`、`pyproject.toml`、`docs/`、`CHANGELOG.md`（新建）、`LICENSE`（新建）、`SECURITY.md`（新建）、`需求文档/01-需求整理.md` |
| **kimi** | 成员 | 跨线独立复核（不写生产代码） | `_work/verify-*.md`（复核报告） |

> **身份变更（2026-10-14 15:48）**：`omp` 下线——其 `alibaba-token-plan/qwen3.8-max` 主回合间歇 `socket closed` 掉线、D 线文档久未落地，human 裁定退休；改由 **`pi`**（pi-coding-agent v1.2.0，provider `qwen-token-plan-cn`，模型 `qwen3.8-max`，无审批）接管 **C 线发布工程 + D 线文档**。花名册身份与文件所有权已由 `omp` 改为 `pi`（`TODO-omp.md` → `TODO-pi.md`）；下文历史叙述中出现的 `omp` 指其下线前的记录。

**规则**：不是你名下 owner 的文件，只发 `ic_send` diff 建议或复核报告，由 owner 合入。新测试一律**新文件**，避免同一测试文件并发写。

> **D 线（新）**：窗口 · 案卷 · 应用。设计与文件所有权见 §七。

---

## 一、里程碑

| 里程碑 | 内容 | 依赖 | 状态 |
| --- | --- | --- | --- |
| **M1 质量基线** | A 线审查完成 + 新增测试落盘，全量测试绿（33/33 零 ResourceWarning），无已知并发/边界漏洞 | 无（可立即开工） | ✅ **已冻结**（A1–A6 coordinator 验收 + kimi 独立复核通过，见 `_work/verify-m1.md`） |
| **M2 联邦定稿** | B1 决策冻结 ✅ → B2 设计冻结 ✅ → B3 实现冻结 ✅（27/27 零跳过全绿，见 `_work/verify-b3.md`） → B4 文档合入 ✅ | 无 | ✅ **已冻结**（B4 三处文档由本项目 omp 守护 pid 27470 于 2026-10-14 15:03 合入，coordinator 核工作区与 kimi D3 预复核一致） |
| **M3 发布就绪** | C1–C4 文档/打包/公开准备/版本对齐已完成 ✅；B4 联邦文档已合入 ✅；公开动作待 human 拍板 | C1–C4 ✅ + B4 ✅；公开 `gh repo edit --visibility public` 待 human D4 | ✅ **已就绪**（等 D4 触发公开） |
| **M4 窗口·案卷·应用** | 认领按线不重复处理；案卷（办事追踪）可追踪；办公室/案卷可视化；应用可暂停/中止/办结、设定项目地址、立案 | D1 数据层 ✅（coordinator+kimi 双签，D1-a/b 已补）；D2 命令与守护 ✅（coordinator+kimi 双签，D2-a/b 已收口）；D3 应用 ✅（kimi 预复核通过后，D3-a/b/c 由 cursor 一次性收口）；D1-a/b 与 D2-a/b 补强已齐，coordinator 本轮核验 82/82 零告警全绿；D 线文档待补（pi） | 🔨 收口中（**仅余 D 线文档**；M5 已冻结、码不再改，已解除 pi 的「停跑待命」请其落笔，见 §八） |
| **M5 最后评审加固** | human「先不急着启动，做最后一轮评审和 debug」→ 3 名分区独立评审 + 3 名对抗性交叉复核（`_work/final-review.md`，基线 82/82 零告警）：P0 6 项必修、P1 9 项应修、P2 若干；按文件分区派 idle 成员修，kimi 独立复核 + 全量回归 | 无 | ✅ **已冻结**（cursor 10 项 + grok 8 项全部落码，18/18 对码无半修/假修；kimi 独立复核 `_work/verify-debug.md`：P0 反驳复现 5/5 通过、基线 **118/118 零告警**，coordinator 独立复跑一致；N1–N4 转 P2，见 §八） |

---

## 二、A 线 · 代码质量与测试加固（owner: cursor）

围绕 0.4.0 现状做审查 + 补测 + 修边界，**不新增功能**。

- **A1｜store 并发与事务审查** ✅
  - `_session()` 每请求新建连接 + `busy_timeout=5000`、WAL；`events` 长轮询持 `threading.Condition` 并在锁内查库 → 确认多线程 `ThreadingHTTPServer` 下 `_signal`（`notify_all`）能唤醒所有等待者、无丢唤醒、无长轮询期间的锁饥饿。
  - 交付：`_work/a1-store-concurrency-audit.md` + `store.py` 写事务 `BEGIN IMMEDIATE`、多 SELECT 读快照 `BEGIN DEFERRED`、`events()` `_seq` 与拆卸保护修复。
- **A2｜边界与错误码矩阵** ✅
  - 覆盖：`limit` 越界、`subject` 空/超长/换行、`body` 空/超 100k、广播（`*`/`all`/`everyone` 归一化）与其他收件人混投、非法名字、非收件人 `mark`、thread 可见性隔离、字段类型校验。
  - 交付：`tests/test_quality_store.py` + `store.py`/`httpapi.py` 修复。
- **A3｜MCP 帧协议与提醒** ✅
  - line-JSON 与 LSP `Content-Length` 双模；`_Watch` 首帧 `since=0` 补报；`--no-notify` 不推、工具尾巴不带未读数；`run_mcp()` 重入复位 `_mode`。
  - 交付：`tests/test_quality_mcp.py` + `mcp_server.py` 修复。
- **A4｜crew 守护纪律** ✅
  - `poll`/`heartbeat` 的 clamp（5–3600 / 0–86400）、workspace 过滤、多项目隔离、`down --dir` 只清本项目。
  - 交付：`tests/test_quality_crew.py`。
- **A5｜已知缺口（已全部实现闭环）** ✅
  - **A5-1｜`heartbeat_prompt` & `heartbeat_mode` 实现**：`crew.py` 读取 `heartbeat_prompt`（展开 `{agent}`/`{project}`，未配回退内置模板）与 `heartbeat_mode`（`idle`/`always`）。
  - **A5-2｜守护 env 注入 `INFOCENTER_AGENT`**：`_start_daemon` 与 `_run_headless` 注入 `env["INFOCENTER_AGENT"] = agent`，消除无 `--as` 时误落 `human` 的问题。`crew.py` 临时所有权已交回 grok。
  - **A5-3｜`normalize_name` 的 `.` 语义**：保持单点支持并拒绝 `..`，记入 A1 报告 §三，与 B 线 `节点.名字` 对齐。
- **A6｜coordinator 复核追加修正** ✅
  - **A6-1**：`watch` 的 `elif`→`if` 回归已修复——恢复 `elif heartbeat and time.monotonic() - last_run >= heartbeat:`，仅在新信分支内按 `if mode != "always": last_run = time.monotonic()` 门控重置，确保单轮至多触发一次 headless（`test_watch_elif_at_most_one_headless_per_round`）。
  - **A6-2**：内置 member 心跳文案已移除裸身份名「给 coordinator 发信」与「coordinator 靠它」，改为「给主协调 agent（`ic agents` 里 role=coordinator 的那位）发信说明」并支持 `coordinator_name` 注入真实协调者名（`test_member_heartbeat_prompt_no_bare_coordinator_recipient`）。`crew.py` 临时授权已归还 grok。
- **验收（M1）**：`python3 -W error::ResourceWarning -m unittest discover -s tests -v` 全量 33/33 零告警全绿 ✅；A1 报告已落 `_work/a1-store-concurrency-audit.md` ✅；coordinator 验收 + kimi 独立复核（`_work/verify-m1.md`）双签通过，**M1 正式冻结** ✅（非阻塞观察：多点名在 B3 导出侧校验点数已转入 B3 复核清单；`test_infocenter.py` 4 处 `Popen` 管道 `ResourceWarning` 已由 cursor 清理闭环 ✅；`inbox()` 走 IMMEDIATE 已记录备查）。

---

## 三、B 线 · R10 跨机联邦（owner: grok）

目标：两台以上各跑本地服务，共享目录只当**传输带**；**SQLite 邮箱绝不放网盘**。

- **B1｜决策（✅ 已冻结，见 `_work/federation-decision.md`）**
  - D1 传输层：**v1 只做网盘 JSON 中继**（outbox 目录 + JSON 文件），不做 Tailscale 直连。
  - D2 身份命名：**接受且只接受恰好一个点的 `节点.名字`**（如 `robin.grok`）；裸名留在本机不导出；`*` 只展开本机名单不跨节点广播。
  - 交付：一页决策 memo（`_work/federation-decision.md`）✅，已冻结进入 B2。
- **B2｜设计冻结（✅ 已冻结，见 `_work/federation-design.md` + `_work/verify-b2.md`）**
  - outbox 结构 `<项目>/.infocenter/outbox/<节点>/`（回执在 `receipts/`，单节点只写自己目录）；单条消息一个 JSON；导入按 message id + 不可变字段幂等（同 id 冲突留先到）；导入时 `workspace` 改写为本地 `--dir`；已配 `node` 时 `*` 跳过 wire name；未配 `node` 时 `ic sync` exit 2；v1 不 GC、不加密、不在 crew 调 sync。
  - 复核修正项（随 B3 一并落入设计稿与实现）：① §4 单文件字节上限由 120KB 提至 700,000–1,000,000 字节（导出 `ensure_ascii=False`，容纳 100k UTF-8 中文/转义正文）；② 回执 `by` 的节点段须等于所在 `outbox/<节点>/receipts/` 的 `<节点>` 目录名；③ 他机消息若无任何 `<本机node>.<裸名>` 收件人（`local_bare_names` 为空）则 `skipped` 不入库；④ `import_remote` 查重对 `to` 的数据库比对以 `deliveries` 中的 `sorted(local_bare_names)` 为准；⑤ `Store.reply` 透传 `local_node` 至 `Store.send`。
  - 交付：`_work/federation-design.md` ✅ + kimi 独立复核 `_work/verify-b2.md` ✅ + coordinator 终签冻结 ✅。
- **B3｜实现（✅ 已冻结，见 `_work/verify-b3.md`）**
  - 已落盘：`infocenter/sync.py`、CLI `ic sync`（export/import/once）、`config.py`（`peers`/`outbox_dir`/`node` + `federation_settings(home)`）及 `tests/test_federation.py`（27 项全激活零跳过通过）；`store.py` / `httpapi.py` 的 §10 最小 diff（`import_remote`/`apply_receipt`/`local_node`）已由 cursor 合入并通过 coordinator 验收。
  - 复核状态：kimi 第二轮终审（`_work/verify-b3.md`）通过，全量 75/75 零告警、联邦 27/27 零跳过，coordinator + kimi 双签确认 **B3 正式冻结** ✅。
- **B4｜文档（✅ 已合入）**
  - `需求文档/01-需求整理.md` 与 `README.md` 归 C 线（omp）owner；grok 的 B4 diff（`m1a1392892ce2a30c7`）已由**本项目 omp 守护**（pid 27470，dir=信息中心）于 2026-10-14 15:03 合入：README 新增「跨机联邦：ic sync」小节 + FAQ/安全两条改写 + 数据文件位置两行；需求文档 R10/遗留第 3 条/验收第 9 条改为 v1 已落地；`.gitignore` 补 `.infocenter/`。
  - **插曲（已裁定）**：B4 信件按身份 `omp` 全域投递，LLM思维链 仓的 omp 会话也读到了它，因该仓无 `README.md`/`.gitignore`/`需求文档` 而误报「合入阻塞」（`m1a139360104d6301a`）。coordinator 裁定：B4 只属 infocenter 仓（`/Users/robinlaw/Downloads/信息中心`），无需跨仓操作，已合入核验通过（见收件箱裁定）。
- **验收（M2）**：B1 决策书面冻结 ✅；B2 设计经 ≥2 方复核 ✅；B3 测试通过 ✅；B4 文档 diff 合入 ✅；单机既有测试不回归 ✅（全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` 77/77 零告警）。**M2 正式冻结。**

---

## 四、C 线 · 发布工程（owner: pi）

- **C1｜文档一致性审查** ✅：README 命令表 / 九工具 / config 字段 / 版本号与现码逐条对齐，产出 `_work/c1-audit-deviations.md` 并修正 `README.md`（测试数 `14`→`33`、补齐 `heartbeat_mode`/`heartbeat_prompt` 字段说明）。
- **C2｜打包与分发** ✅：`python3 -m build` 产出 `dist/infocenter-0.4.0-py3-none-any.whl` 与 `dist/infocenter-0.4.0.tar.gz`；干净 venv `pip install` 验证 `ic --version` / `ic status` 通过；新建 `CHANGELOG.md`（0.1.0 → 0.4.0 + Unreleased 规划）。
- **C3｜公开准备** ✅：新建 `LICENSE`（MIT）、`SECURITY.md`（明示 localhost-only / 无鉴权 / 明文存储边界），完成敏感信息与硬编码路径扫描；公开命令 `gh repo edit --visibility public` 仅在 M1/M2 稳定后由 human 触发（D4）。
- **C4｜版本与变更** ✅：`pyproject.toml` 动态版本、`infocenter/__init__.py` 版本号、README badge/footer 三处 `0.4.0` 对齐；`CHANGELOG.md` 已规划 0.4.x → 0.5.0（联邦为 minor）。
- **验收（M3）**：C1–C4 已由 coordinator 核验通过 ✅（sdist/wheel 可装可用、文档零偏差、公开清单就绪、版本号三处一致）；B4 联邦文档已合入 ✅。**M3 就绪收口**，仅待 human D4 拍板后触发 `gh repo edit --visibility public`。

---

## 五、决策队列（待裁定）

| # | 事项 | 归属 | 状态 |
| --- | --- | --- | --- |
| D1 | R10 传输层 | human 已签 | ✅ **冻结**：v1 只做网盘 JSON 中继，不做 Tailscale |
| D2 | 身份命名 | human 已签 | ✅ **冻结**：恰好一个点的 `节点.名字`；裸名只留本地；`*` 不跨节点 |
| D3 | `heartbeat_prompt`：**裁定＝实现**（未配置回退内置）；与 A5-2 env 注入一并处置 | A 线（cursor） | ✅ 已实现并验收（含 A6-1/A6-2 追加修正，33/33 全绿，`crew.py` 已归还 grok） |
| D4 | 仓库是否转公开、时机 | human | ❓ 待拍板（建议 M1/M2 后） |
| D5 | `~/Downloads/信息中心` 的 TCC 权限（headless 读写 Downloads 可能被拦） | human | ✅ 已确认正常（守护进程可正常读写） |
| D6 | `activity` 是**全局按身份**、非「身份×项目」：同一身份的多项目守护会互相覆盖 activity（实测 cursor 在 A 线自报后被 ICLR 守护覆盖回 GPU 状态） | human / coordinator | ✅ **已裁定＝按办公室（窗口）分区**，随 D 线 D1 一并实现（见 §七） |
| D7 | 凭空身份 `coordinator`（A6-2 副作用）残留 3 封 kimi 未读；根因修复后是否清理该身份与信件 | human / coordinator | ❓ 待处理（低优先，不阻塞） |
| D8 | 本项目 5 个守护（pid 27466–27469 起于 13:12:49、pi 56260 起于 15:49）与 8787 服务端（pid 64020 起于 04:46）均仍运行旧码；M5 已冻结（码不再改），重启即加载全部修复 | coordinator | ⏸ **就绪待执行**（M5 复核通过＝技术前置已满足；但 ① human 15:55「先不急着启动」的 go-live 指令优先，② 无头会话执行 `crew down` 会经 C-7/D2-a 路径 `killpg` 自身进程组而自杀、并波及共享服务端与另 2 个项目在飞守护 → 应由**非无头** coordinator 窗口一次性执行 `ic stop && ic serve --detach` + `crew down/up --dir 信息中心`，见 §八） |

---

## 六、协作纪律（本团队生效）

1. coordinator（opencode）裁定优先；有异议只回**一条**针对冲突点的信，不并行改文件。
2. 单一写者：见 §〇 所有权表；跨文件改动先发信。
3. 规划纪律：只 coordinator 改本文件；成员进展写 `TODO-<名字>.md`；要改计划发信。
4. 状态自报：开始/切换/完成任务调 `ic_activity`；coordinator 看 `ic agents` 调配，**不发问询信**。
5. 礼仪：无新信息不回帖，禁止 +1/「收到」；同一分歧最多两轮。
6. 冻结流程：关键产物（B2 设计、里程碑验收）沿用「草案 → 独立复核 → 冻结签名」模式。
7. **单一活跃 coordinator**：`opencode` 身份存在多 actor（交互会话 + 心跳守护）。改 `PLAN.md` / 发派单任务信**只能由交互 coordinator 窗口**执行；守护已只读化（仅收信、写 `_work/plan-maintenance.md`、必要时发建议）。治理方案见 `~/.opencode/plan/infocenter-多coordinator治理-计划.md`。
8. **禁影子写手**：coordinator **不得亲自或派非花名册子代理改成员文件**，一律派给 §〇 所有权表里的 owner 并等回报。

---

## 七、D 线 · 窗口 · 案卷 · 应用（主办：opencode 设计/接口/验收；实现按文件分派）

> 设计已定稿：本机 `~/.opencode/plan/infocenter-窗口案卷应用-计划.md`。**以该文件为规格**。
> 一句话：给每个窗口一个内部编号（走 `X-Window` 请求头，不动名字空间），**认领按 thread 传播**；每个问题一份**案卷（办事追踪）**；办公室/案卷可视化；管理应用可**暂停/中止/办结**、**设定项目地址**、**立案**。

**D 线文件所有权（单写者，覆盖 §〇 中的同名文件）**

| 文件 | owner | 说明 |
| --- | --- | --- |
| `infocenter/store.py` | **cursor** | `deliveries` 加 `claimed_by`/`claimed_at` + 迁移；新增 `projects`/`windows`/`cases`/`case_nodes` 表；认领/立案/案卷节点/办公室登记/离场自动中止 |
| `infocenter/httpapi.py` | **cursor** | 读 `X-Window` 透传；`read`/`reply` 落认领；`GET /v1/cases`、`/v1/cases/{id}`、`/v1/windows`、`/v1/projects`；`POST /v1/cases/{id}/pause\|resume\|abort\|close` |
| `infocenter/mcp_server.py` | **cursor** | 启动生成/读取窗口号；events 带窗口；推送按 `claimed_by` 过滤；尾巴按窗口计数；只读工具 `ic_case` |
| `tests/test_quality_window.py`（新建） | **cursor** | 见设计 §8 的 10 条 |
| `infocenter/client.py`、`cli.py`、`crew.py`、`config.py` | **grok** | 窗口号透传（`X-Window`）；`ic case`/`ic cases`/`ic windows`；`crew pause/resume/abort/close`、`crew down --only`；守护生成窗口号注入 headless env；离场主动上报；headless 动作改为"读案卷→干活→写回节点" |
| `infocenter/server.py`、`infocenter/web/index.html`（新建） | **cursor**（D3-a/D3-b/D3-c 收口） | `/ui` 静态路由 + 管理台单页（三视图 + 操作 + 设定项目地址 + 立案；单 HTML + `fetch`，不引框架）。**收口规则（2026-10-14 15:24 更新）**：`index.html` 单写者＝cursor，连同必修 **D3-a**（`last_node` 渲染 `[object Object]`）一并收口；omp 因上游 provider（`alibaba-token-plan`/`qwen3.8-max`，401 Invalid credential + socket closed 重试循环）卡死未能落地 D3-a，已停其在本文件上的写入、专注 D 线文档 |
| `README.md`、`skills/infocenter/SKILL.md` | **pi** | 补 D 线文档（窗口/案卷/应用），与实码一致 |
| `_work/verify-*.md` | **kimi** | 独立复核 |
| `PLAN.md`、设计/接口冻结、验收 | **opencode** | 本文件与设计规格 |

**任务与排期**

- **D1 数据层（cursor）**：store + httpapi + mcp_server + 测试。**✅ 已完成并经 coordinator + kimi 独立复核通过**（`infocenter/store.py`、`infocenter/httpapi.py`、`infocenter/mcp_server.py`、`tests/test_quality_window.py` 10 项测试全绿，同时合入 B3 §10 接口，含 D6 按办公室隔离 activity；`_work/verify-d.md` D1-a/D1-b 旧库迁移与转办正向单测已补齐；coordinator 本轮核验 82/82 零告警全绿）。
- **D2 命令与守护（grok）**：client/cli/crew/config。**✅ 已完成并经 coordinator + kimi 独立复核通过**（`infocenter/client.py`、`infocenter/cli.py`、`infocenter/crew.py`、`tests/test_d2_commands.py` 9 项测试全绿；D2-a/D2-b 已收口：`_run_headless` 改用独立进程组 `Popen` 并将 `headless_pid` 写入守护 meta，`crew pause` 对该进程组发 `SIGTERM` 不杀守护、本窗口 `paused` 时 `watch` 跳过新一轮且未处理新信不推进游标，`crew resume`/`close` 与 `INFOCENTER_WINDOW` env 注入已补测；coordinator 本轮复跑全量 82/82 零 `ResourceWarning`）。
- **D3 应用（cursor 收口）**：`/ui` + 管理台。**✅ 已完成并经 coordinator 核验**（`infocenter/server.py` `/ui` 静态路由与 `infocenter/web/index.html` 真实 `fetch` 版）：kimi 预复核（`_work/verify-d.md` §D3）判「代码侧基本通过，1 条必修 D3-a + 3 条提示级」。D3-a（案件列表 `last_node` 渲染 `[object Object]`）原派 omp，因其 headless（pid 23564）自 15:09 起陷于上游 provider（`alibaba-token-plan`）重试循环（>15 分钟零工具调用，`m1a1393f19c75484a2` 未签收），coordinator 于 15:24 裁定改由 **cursor 连同 D3-b/D3-c 一次性收口**（见上表收口规则），并已终止该卡死进程。cursor 15:29 交付：`index.html:195` 末节点对象渲染、`197-214` 按钮显隐改按原始 `c.status`（open/doing/pending/waiting 可暂停、paused 可恢复、sealed/aborted/done 隐藏中止办结）+ CSS 补原始状态与「待办/离场」底色、`281-286` 离场二次 `confirm()` + `window_id`/`case.id` 转义加固，新增 `test_13_ui_console_smoke_and_d3_abc`；coordinator 复跑 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **78/78 OK 零 ResourceWarning**。**剩余**：**D 线文档**（`README.md`、`skills/infocenter/SKILL.md`、`需求文档/01-需求整理.md` 的窗口号/案卷/`ic case`/`crew pause|resume|abort|close`/`/ui` 说明）归 pi（原派单 `m1a13963cf861a79e5`，15:48）；因 M5 debug 轮一度暂缓，M5 收口后已重新激活（见 §八），待落笔。
- **D4 复核（kimi）**：**✅ D1+D2 独立复核已通过**、**✅ D3 预复核已出**（`_work/verify-d.md`，77/77 零告警实测）；D1-a/b、D2-a/b、D3-a/b/c 均已收口（coordinator 本轮 82/82 零告警），**唯余 D 线文档（pi）**，文档合入后补一次全量回归即出 **D 线终审**。
- **顺带**：D6（activity 按办公室分区）已随 D1 落地 ✅；D8（守护重启）技术前置已满足、**就绪待执行**，须由非无头窗口一次性执行（见 §五/§八）。

**派单**（2026-10-14，high）：cursor D1 `m1a1390537469b0f80`｜grok D2 `m1a139053766b70bbd`｜omp D3 `m1a13905378431a235`｜kimi D4 `m1a1390537991671bd`。

**验收（M4）**：同身份两窗口新信只提醒一个；案卷节点完整且封卷只读；暂停可恢复、中止/办结封卷；离场自动中止并补 `[自动]` 节点；应用三视图可用 + 四个操作可用 + 能设定项目地址与立案；旧库升级不炸、单窗口与无窗口号行为不变、全量测试绿。

---

## 八、M5 · 最后评审 · debug 轮（触发：human 2026-10-14 15:55「先不急着启动，做最后一轮评审和 debug」）

> **规格＝`_work/final-review.md`**：3 名分区独立评审（数据层 / 管理台·服务端·联邦·MCP / CLI·守护·客户端·config）+ 3 名对抗性交叉复核（逐条证伪 + 补漏），基线 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **82/82 零告警**。全程未改生产文件、未动现网 8787。
> **D8 重启**就绪待执行（见 §五/§八）；D 线文档（pi）曾暂缓以免文档追改，M5 已收口、码冻结 → 已解除待命、pi 可落笔。

**修复清单与按文件派单（单一写者，`§〇` 所有权）**

- **P0（本轮必修）**
  - `cursor`｜**U-1 存储型 XSS**（`web/index.html:196/201/209-212` + `httpapi.py:324` + `store.py:1018/1047`；jsdom 实测属性上下文实体解码逃逸 JS 串）：① 服务端校验 `thread_id`（ID_RE/白名单）；② 前端废 `onclick` 串改 `dataset`+`addEventListener`。**仅加强 `escapeHtml` 无效**。
  - `cursor`｜**M1(events) 信件丢失**（`store.py:440/453/493/494`：cursor 取全表 `MAX(rowid)` 但只回最新 `limit` 条 → 取最旧 `limit` 条且 cursor=实际返回最大 seq）。
  - `grok`｜**C-3 非 UTF-8 自杀+误封卷**（`crew.py:768-789` `text=True` + `729-744` 无捕获 → `UnicodeDecodeError` 穿透 watch、`finally _leave_office` 自动封卷）：`Popen(errors="replace")` + watch 包 try/except 记日志续跑。
  - `grok`｜**C-7 暂停排序**（`crew.py:452-459` `_interrupt_headless` 排在两条只读 GET 之后，旧服务 404 即失败）：先打断或 `try/finally`。
  - `grok`｜**C-6 触发信丢信**（`crew.py:727-732/790-806` 在跑 headless 前写 cursor、超时/失败不回退）：失败回退 cursor 到本轮起点 + 补 `[自动] 中断` 节点。
  - `grok`｜**C-1 窗口串号**（`crew.py:274-277`→`816-824` `_start_daemon` 不 pop `INFOCENTER_WINDOW`，`_adopt_window` 复用 env）、**C-5/M5 裸 traceback**（`client.py:99-108` 把 `TimeoutError/OSError/RemoteDisconnected` 包成 `ApiError`）。
- **P1（应修）**
  - `cursor`｜**D-1 inbox 截断 + M1-data 未读口径**（`store.py:841-862` 过滤下推 SQL；`_unread_counts`@`2428-2432` 同修）、**D-2 转办双归属**（`store.py:1266-1274/1925-1953` 释放源 owner 认领）、**D-3 非当事人立案**（`store.py:1218-1234` 复用 `_is_party`）、**D-4 死 pid 复活**（`store.py:2126-2131` 复活置 `pid=NULL`）、**D-5 500→400**（`httpapi.py:320/324/442/401/417` 入口 `isinstance(str)`）、**U-2 Content-Length 下界**（`server.py:59-66`）、**U-4 MCP 崩溃**（`mcp_server.py:20-24/38-46` 注册纳入 try + 规范校验）、**L1 CSRF/Origin**（`httpapi.py:169-182` + `server.py` 校验 `Origin`/`Sec-Fetch-Site` 或强制 JSON）。
  - `grok`｜**C-4 pid 复用**（`crew.py:283/84-89` pid 文件记 `started_at`/命令行校验或退出 unlink）、**C-8 登记失败降级**（`crew.py:834-851/563-571` 登记成功才进收信循环/退出/标红）。
- **P2（可修/记录，本轮不做）**：D-6/D-7/D-8、C-2/C-9~C-16、M2/M3/M4、U-3、U-5、U-6、L2/L3（详见 `_work/final-review.md`）。

**验收（M5）**：P0 全绿、P1 全绿或明确记录；每条附回归测试；kimi 独立复核（`_work/verify-debug.md`，含 P0 反驳复现）；全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` 零告警；D8 重启可安全执行。

**执行方式**（2026-10-14 16:32 coordinator 裁定）：按文件分区**派 idle 成员修**（cursor＝数据层+管理台/MCP；grok＝守护/CLI/客户端/config），kimi 独立复核——保持 `§〇` 单一写者、并行推进。`_work/final-review.md` 原注「由 coordinator 直接改」系针对「旧码守护派单不可靠」的顾虑；本轮改由 owner 成员经 `ic send` 派单执行，coordinator 不自改生产文件。

**进展（2026-10-14 16:55 心跳 · coordinator 独立复跑）**

- **派单已真实执行**：cursor 的 `信息中心` headless（pid 79906，起于 16:36，提示词即 P0/P1 派单 `m1a1398c0bb968cdd9`）与 grok 的 headless（pid 78523）均在跑；两者 activity 自报仍是 `01_ICLR2027` 文案属**跨项目身份共用**的显示现象，不影响本仓执行。
- **代码已落盘**（mtime 16:41–16:50）：
  - `cursor`（数据层+管理台/MCP）：`store.py`、`httpapi.py`、`mcp_server.py`、`server.py`、`web/index.html`。已见：**U-1** 前端废 `onclick` 串改 `dataset`+`addEventListener`（index.html:189/230/236）+ 服务端 `case_id` 白名单校验（`store.py:97` CASE_ID_RE）；**M1(events)** `_events_snapshot` 改取**最旧** `limit` 条、`cursor`=实际返回最大 `delivery_seq`（`store.py:446-489`）；**U-2** `Content-Length` 下界校验（`server.py:59-71`）；**L1** `Origin`/`Sec-Fetch-Site` 校验（`httpapi.py:503-529`）；**U-4** MCP 注册纳入 `try`。
  - `grok`（守护/CLI/客户端/config）：`crew.py`、`cli.py`、`client.py` + 新建 `tests/test_debug_p0.py`（**11 项**，逐一对应 C-1/C-3/C-4/C-5/C-6/C-7/C-8）。
  - `cursor` **回归测试也在补**：新建 `tests/test_hardening_store.py`（**15 项**，D-1…D-5/U-1/U-2/L1 等）+ 更新 `tests/test_quality_{store,mcp,window}.py`（16:57）；`test_d4_implicit_touch_does_not_revive_left_window` 一度红（D-4 实现未落完），17:06 已转绿。
- **全量回归**：coordinator 独立复跑 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **118/118 OK 零 ResourceWarning**（17:06 快照；基线 82 → grok 11 + cursor ~25）。*注：16:51–17:03 期间套件数度红（首跑 8 fail/38 error、后 4 fail/2 error、1 fail），均系 `store.py` 与 hardening 测试**正在被写**的半写快照（md5 逐次变），非跨项回归；收敛后 17:06 全绿。*
- **剩余**：① cursor/grok 各自**回信报完成**（跑完即报，含回归测试清单）；② kimi `_work/verify-debug.md`（**触发条件已收紧：cursor 与 grok 均回信报完成且回归测试落盘**，避免对着半成品出报告）；③ D8 重启；④ D 线文档（pi）。
- **D 线文档（pi）本轮明确暂缓**：pi 的 README/SKILL/需求文档 mtime 仍为 14:53/02:43/15:01，两轮 headless 均因 `qwen3.8-max` 提供方超时退出；因 debug 会再改这些文档要描述的实现，coordinator 已发信请 pi **停跑、静默待命**，待 M5 收口后一次写准（不再让其空转超时）。

**交卷（2026-10-14 17:07 · 两家已回信，coordinator 复核）**

- **grok 交卷**（`m1a1399fe4b5deb78d`，16:55）：C-1/C-3/C-4/C-5+M5/C-6/C-7/C-8 全部落在 `crew.py`/`cli.py`/`client.py`，新测 `tests/test_debug_p0.py` **11 项**；明确 **P2 与 `sync.py` U-3 未做**（本轮不做，符合 §八）；**M1(events) 在 `store.py` 非本方文件，未动**（正确，避免跨写）。
- **cursor 交卷**（`m1a139a7565f8e42b7`，17:03）：自报 `_work/final-review.md` **全部 10 项**（U-1/M1-events/D-1+M1-data/D-2/D-3/D-4/D-5/U-2/U-4/L1）逐项落码，均落在 `store.py`/`httpapi.py`/`mcp_server.py`/`server.py`/`web/index.html` + 测试内；新测 `tests/test_hardening_store.py`（15 项）+ 更新 `test_quality_{store,mcp,window}.py`；自报 118/118 绿。
- **coordinator 独立复核**：`git status` 显示改动严格落在双方 §〇 名下文件（无越界）；全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **118/118 OK 零 ResourceWarning**（17:06 稳定快照，此后无写盘、无 in-flight headless）。
- **下一步**：已派 `kimi` 出 `_work/verify-debug.md`（`m1a139a9be4b9accf0`，含 P0 反驳复现）；复核通过后 coordinator 执行 **D8**（`crew down/up` 加载新码）+ 重启服务端；随后 pi 落 D 线文档 → kimi D4 终审。

**复核（2026-10-14 17:19 · kimi `_work/verify-debug.md`，信 `m1a139b59cc53a1cd3`）**

- **结论：P0 全绿（5/5 反驳复现）、P1 全绿（10 项对码）、18 项无一被反驳 → 可以冻结。**
- 基线复跑 **118/118 OK 零 ResourceWarning**（与 17:06 快照一致，期间无写盘）；coordinator 17:24 独立复跑同结果，生产/测试文件 mtime 最新止于 17:03:49，确认复核对象无漂移。
- P0 反驳复现（独立脚本，刻意走与测试不同路径）5/5：U-1 服务端 5 个恶意/非法 `thread_id` 全 400 + jsdom 真引擎 0 内联 onclick、`__xss` 未注入；M1-events 45 封/limit20 无窗/有窗两条路径翻页 45/45 无丢失；C-3 垃圾字节不再穿透；C-6 真守护端到端 cursor 留 0 + `[自动]` 中断节点补齐；C-7 服务全挂时 sleep 先被 SIGTERM 打断。
- 逐项对码 18/18 已修、无半修/假修；grok 的「P2 与 `sync.py` U-3 未动」声明抽查 3 条属实。
- **M5 正式冻结**（§一 已标 ✅）。
- **N1–N4 登记 P2 backlog**（复核新增，非阻断，本轮不改）：
  - **N1**（C-5 边角）：`http.client.IncompleteRead` 等仅继承 `HTTPException`（非 `OSError`）的异常仍可能逃逸成 traceback，概率低。
  - **N2**（C-6 行为）：重试无退避/无上限——headless 永久失败时每轮重跑并反复写中断节点（复现 9 秒写了 15 个）；建议后续加次数上限或指数退避。
  - **N3**（口径微差）：events 无窗口路径 `unread_total=len(rows)` 不排除已被认领的信，宽于 `_unread_counts(None)`；仅守护自用快照字段，不影响完整性。
  - **N4**（C-4 迁移期）：旧格式裸数字 pid 文件只判活不校验身份；新写入必带 `started_at`，迁移期过后自然消失。
- **D8**：技术前置（复核通过）已满足，但 **本无头会话（pid 20255，由 opencode 守护 27468 派生）不执行**——`crew down` 会给守护发 SIGTERM，经 `_run_headless` 的 `except BaseException`（C-7/D2-a 刻意设计）`killpg` 本 headless 进程组，「自杀」中断本轮、可能把 crew 停在半下状态；且 D8 还要重启**共享 8787 服务端**（另 2 个项目守护挂同一端口）并打断 grok/cursor 在飞 headless。加上 human 15:55「先不急着启动」的 go-live 指令优先于 peer 复核 ⇒ 交**非无头** coordinator 窗口一次性执行。
- **D 线文档（pi）**：M5 已收口、码冻结 → 已发信解除 pi 的「停跑待命」，请其按冻结码落 README/SKILL/需求文档；若本轮仍 `qwen3.8-max` 超时未落，按 §八 停滞登记改派 cursor/grok。
