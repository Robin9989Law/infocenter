# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **排队改同一个文件：由第一个 agent（拿钥匙的）合并后面所有改动**（human 定稿）。新增 `ic queue`：拿不到钥匙的人用 `ic queue <路径> --note "…" [--patch-file f]` 把改动**提**给持有者（**不需要钥匙**）；持有者在 `ic locks` 里看到 **📥 有 N 条排队等你合并**，把内容并进自己这一次改动后 `ic queue <路径> --merge` 收口。权限：**只有拿钥匙的人能合并**（coordinator 例外——钥匙的主人不在时才有人收口；非持有者合并 403，没拿钥匙且非 coordinator 409）。提议与合并**都进只增链**（`propose` / `propose_merged`）⇒ 谁提了什么、谁合并了可查（`ic queue` 默认只列未合并，`--json` 里 `merged` 保留历史）。409 文案同步教人排队。+ `tests/test_lock_queue.py`（13 项：免钥匙提议 / 持有者可见条数 / 权限矩阵 / 按 id 部分合并 / 留痕与 audit / HTTP 往返）
- **`ic reply --body-file`**（pi 报、grok 落）：`--body` 的正文进 shell 会被反引号当命令执行 ⇒ 与 `ic send` 对称，正文可从文件读（两个都不给/都给都有明确报错）。+ `tests/test_reply_body_file.py`
- **一把钥匙 + 用完放回去 + 锁＝只读**（human 定稿的口径，落到机制）：
  - **单钥匙**：锁按路径唯一，但"文件"不只等于"路径字符串"——新增 inode 比对（`_same_file`/`_fingerprint`），`./a`、绝对路径、符号链接、**硬链接**、macOS 大小写不敏感下的 `A.py`/`a.py` 都只算同一个文件（换个写法绕不过去）；同一文件的旧写法残留会把那一把换掉，**且只换那一把**
  - **锁＝只读**：拿钥匙时给文件打开 owner 写位，放钥匙时**清掉所有写位**（444/555）⇒ 带插件的、不带插件的、`shell > file`、别的 agent 的编辑器**一律先拿钥匙**（实测：直接写抛 `PermissionError`）；读权限不动（**可以只读**）
  - **用完放回去**：新增 `.githooks/post-commit` —— 提交后**自动**放掉本次提交涉及的钥匙，身份取自 `git config --local infocenter.agent`（`ic guard --install-hook` 写入）；只放自己的、空输入绝不退化成"放掉我全部钥匙"（`$(...)` 会丢 NUL ⇒ 必须管道给 `xargs -0`，这两个坑都有回归测试）
  - + `tests/test_lock_single_key.py`（16 项：单钥匙/inode/只读/自动放回/钩子空输入/中文路径）
- **写侧自动拿钥匙：`.opencode/plugins/infocenter-lock`（OpenCode 插件）**。规矩（human 定稿）：**所有文件都锁住；要改就拿钥匙开锁；一旦开锁，别的 agent 就改不了；改完（提交）锁回去**。提交那一刻本来就由 git 钩子兜底，但**写**的那一刻 git 看不见 ⇒ 插件在 `edit` / `write` 落盘**之前**自动 `POST /v1/locks`：拿到就放行（重复拿＝续期），**别人开着锁就直接拒绝这次写**（工具报错，模型看得见），信息中心不可达则放行（离线不阻断）。边界：只罩本会话的 `edit`/`write`（`shell` 绕得过）、只对插件所在项目目录生效；别的 agent 的 CLI 没有这个插件，仍靠自觉 + 提交闸
- **死流程兜底由机器自己跑（`Store.sweep()` + 服务端后台定时器）**：过期锁、**死进程窗口**、**僵尸认领**三件事原来只在"有人 inbox 时才顺手清"触发 ⇒ 低活跃项目可能很久不回收（pi 报的洞）。现服务端起一个 60s 的守护线程专做这件事，且**只做无争议回收、全部留痕**（`lock_expire` / `claim_release`），**绝不碰语义状态**
- **认领 TTL（`CLAIM_TTL`，默认 7200s）**：认领原来**没有 TTL** ⇒ 僵尸窗口（pid 活着但不再干活）的未读认领永不回收，那封信对团队长时间不可见。现"`claimed_at` 超时 **且** 那个窗口也超时没跟服务说过话" ⇒ 回收（TTL 故意大于 `headless_timeout`：长任务跑起来时窗口本来就是安静的）
- **看板「停滞」黄灯**：未封卷、且最后一个节点超过 `STALL_HOURS`（默认 24h）没动 ⇒ `ic cases` / 控制台标 **⚠ 停滞 Nh**。**只提示不拦**，程序不自动处置（kimi/cursor/pi 一致意见："沉默的 doing"只该亮灯）
- **`ic guard --staged`**：只查**暂存区**（`git diff --cached --name-only -z --diff-filter=ACMR`）。提交钩子用它——闸门要盯的是"这次提交了什么"，而不是整棵工作区（cursor 实测：一个未跟踪的 `junk.tmp` 就能让空提交失败）。手工跑 `ic guard` 默认仍查整棵工作区（更严）
- **`.githooks/pre-merge-commit`**：非快进 merge 不回落 `pre-commit`（cursor 实测 merge 时钩子根本不跑，改动无锁落地）⇒ 现在多一个同内容的钩子把 merge 也罩住
- **`ic lock --changed` 与显式路径取并集**：原来给了路径就顶掉 `--changed`（打锚时踩到：锚文件锁上了、9 个源文件反而没锁）
- **控制台改三层：项目 / WBS / 案卷（M7-3）**：中间栏从「办公室」换成 **WBS 树**（从案卷 `wbs_path` **派生**，不建表；含「未归类」节点，父节点计数含子树）；点 WBS → 右边只看该 WBS（含子路径）的案卷。**「办公室」改称「项目团队会话」并从控制台移出**（WBS 栏右上角按钮进二级页：会话状态 / 设置 / 实时状态）——会话只负责并发隔离与认领，不是业务概念。文案同步：停用会话的二次确认不再说「离场并封卷」（M7-1 之后这句已经不成立，UI 不能骗人）
- **看板人话 + 「人话过期」（M7-4，不新增字段）**：案卷的**人话 = coordinator 写的最后一行节点**（role=coordinator 或 human，且非自动节点）；若这行之后又有别的节点，`ic cases` / 控制台给它标 **⚠ 人话过期**（看板说法可能已不准，等 coordinator 重写）。agent 写的节点是②线备忘，**不当作给人看的记录**。`_case_view` 新增 `human_note` / `human_by` / `human_at_beijing` / `human_stale`
- **案卷 = 控制包：WBS 路径 + 里程碑（M7-2）**：`cases` 加两列（在线迁移自愈）——`wbs_path`（**WBS 树路径**，`父/子`，空 = 未归类；**一个案卷唯一归属一个 WBS**）与 `milestones`（**一列 JSON** `[{"name","owner","done"}]`，**不建表**）。命令：`ic case <id> --wbs 父/子`、`--milestone-add "名字" [--milestone-owner X]`、`--milestone-done N`（`!N` 撤回）；`ic cases --wbs 父`（含子路径）。**WBS 与里程碑的写权限一律 coordinator**（agent 提议、coordinator 落），每次改动在案卷上留一行 `milestone` / `wbs` 节点。HTTP：`POST /v1/cases/{id}/milestone|milestone-done|wbs`、`GET /v1/cases?wbs=`、立案可带 `wbs_path`/`milestones`
- **留痕外锚（`ic audit --anchor`）**：把哈希链链头写进仓库里的 `.infocenter-audit-anchor`，**连同 git 一起提交** ⇒ 锚落在**库外**。`ic audit` 核对"锚点那一行还在不在、哈希是否仍是原来的"：链被改、被截短、**整表删掉重建自洽假链**都会报「外锚对不上」。三家评审（kimi / cursor / grok）一致指出的缺口正是"链整个在库里 ⇒ 改一行再把后面哈希全部重算，`ic audit` 照样绿"。实测（真改库）：偷改一个字段被当场抓住；把哈希"补圆"则只有靠锚才认得出。`ic audit` 另报「锚点之后还有 N 行没被见证」（锚只保护锚点之前的历史 ⇒ **每次提交前打一次锚**）
- **锁的痕迹**：`lock` / `unlock` / **锁到期自动回收**（`lock_expire`）各追加一行只增痕迹。cursor 指出抢锁/放锁是静默 DELETE、事后看不见；到期回收是死流程里唯一会自动做的事，更要留痕
- **`ic guard --install-hook`**：一条命令把 `.githooks/pre-commit` 装上（`core.hooksPath`，幂等、自动补可执行位）。第 2 道闸以前只是"这台机器上的本地配置"（cursor：新 clone 就没钩子了）；`ic guard` 没装时会提醒
- `ic audit --quiet`（给 git 钩子用，只给退出码）；`.githooks/pre-commit` 增加**留痕校验**一道（链断 / 重放不一致 / 外锚对不上 ⇒ 拦住提交；离线或服务不可达不阻断）
- **`ic_thread` 线程树定位**：线程仍按时间序返回，但每封信多带 `parent_id` / `depth` / `root_id` / `children`（纯派生，存储不变、不嵌套）。agent 不必拿 id 两两对照就能看出谁回谁、某分支在第几层，可只聚焦某个分支。可见集内构树：父信不可见（未参与/跨线程导入）时该封按根处理，但 `parent_id` 仍返回真实 `reply_to`
- **空转治理（idle guard）**：守护命中模型**额度/鉴权上限**（输出含 `usage limit` / `quota` / `invalid credential` / `401` 等）不再按 `heartbeat` 间隔硬撞，而是进入**永久失败长冷却**（`permanent_cooldown`，缺省 6h）；普通失败连续达 `failure_threshold` 次进入短冷却（`failure_cooldown`，缺省 1h）。冷却期内不触发 headless（**新信也不跑、游标不前进**，冷却结束重新投递），`ic crew status` 标注「额度/鉴权不可用，剩 N 分钟」
- **心跳空转退避**：心跳「空转」（`progress_files`，默认 `TODO-<名字>.md`，未变）时间隔按 ×2 指数拉长，封顶 `idle_backoff_max`（缺省 2h）；新信或进展出现即复位。可 `idle_backoff: false` 关闭
- **headless 永久失败分类**：`_run_headless` 非零退出按输出尾部归为 `quota`（永久）或 `fail`（可重试），写入 `*.runs.jsonl`
- **内置 member 心跳提示词改为短路式**：先看未读与自己的 `TODO-<名字>.md`，无事即结束；只在确需时**按需**读 PLAN.md 的相关段落（禁止整份读入）——避免每次空转把数百 KB 的 PLAN.md 反复喂进模型
- **管理台 `/ui`**（单页、原生 `fetch`、无外链）：顶部成员状态条 + 项目 / 办公室 / 案件三栏级联；写操作全在二级页面（新建项目、设定地址、归档 / 取消归档、网页起停守护、立案、暂停 / 恢复 / 中止 / 办结 / 重开、转办）
- **`/ui#human` 干预中枢**：向所有项目广播、对全局在跑的办公室与未结案卷直接干预、查看任一身份的信件往来
- **一键重启** `POST /v1/restart`：重启服务端与全部在跑的项目守护，并轮询 `/health` 直到恢复
- **打开项目文件夹** `POST /v1/open`：在系统文件管理器中打开项目目录
- **headless 运行可观测**：记录历次运行的提示词与输出尾部（`crew/<key>.runs.jsonl`）与正在跑的那次提示词（`crew/<key>.prompt`）；`GET /v1/headless`、`GET /v1/headless/runs`
- **单次 headless 墙钟上限 `headless_timeout`**（缺省 1800 秒）：到期进程组被 SIGKILL，守护不再整轮停摆
- **窗口（办公室）· 案卷 · 应用**（D 线）：`X-Window` / `INFOCENTER_WINDOW`、`ic windows`、`ic cases` / `ic case --note`、立案 / 转办；离场自动中止未封卷案卷并补 `[自动]` 节点
- **多 coordinator 治理**：单一活跃 coordinator / 禁影子写手的纪律，守护对计划只读
- R10 跨机联邦（B 线）：网盘 JSON 中继传输层，`节点.名字` 身份命名

### Changed
- **coordinator 前台由人驱动**：`ic crew status` 对不配 `launch` / `headless` 的 coordinator 显示「前台（人工驱动）」，不再当成「未配置启动方式」；文档明确 coordinator 一般不给配 `headless`（守护会跟着改计划/派单）、更不要配 `launch`（`crew up` 会在 tmux 会话里另起一个会抢活的前台 actor）。**并有机器兜底**：`ic crew up` 对这样的 coordinator 打印 `[警告]`（非阻断），`ic crew status` 该行标红 `⚠ 配了 headless/launch（会抢活）`
- **术语澄清**：README 术语表 + 协作约定、`skills/infocenter/SKILL.md` 明确 **`human` ≠ coordinator**——`human` 是「人自己的邮箱身份」（意图与拍板以它下发，也是默认 `--as` 与唯一能移除其他成员的特权身份），coordinator 是人驱动的 agent
- **守护对 config 热加载**：每轮重读 `headless` / `heartbeat_mode` / `heartbeat_prompt` / `headless_timeout`（及未显式传入的 `heartbeat`），改配置无需重启守护；清空 `headless` 守护优雅退出
- **成员名册**：`/v1/agents` 带 `roster` 标记，管理台只显示名册成员；项目按绝对路径为锚合并（裸目录名归并到同一项目）
- **身份迁移**：`omp` 退役、由 `pi` 接管；清除幽灵身份 `coordinator`
- 测试数量从 16 增加到 158（A 质量 / B 联邦 / C 发布 / D 窗口·案卷·应用 + M5 最后评审加固），全量零 `ResourceWarning`

### Fixed
- **闸门取路径三处静默失效（pi 实测报的 P1，"假绿灯"）**：`_changed_paths`（`ic lock --changed` 与 `ic guard` 的唯一取路径入口）有三个坑，坏起来**不报错**：① `git status --porcelain` 默认把非 ASCII 路径转义成 `"\351\234\200…"` ⇒ 解析出**不存在的路径**，而 lock 与 guard 用同一个假 key、互相自洽、**照样放行**（互斥却已经没了）——本仓 `需求文档/` 整棵在闸门外；② `--porcelain` 是**仓库根**相对，代码却按 cwd 解析 ⇒ 子目录里跑全是假路径；③ rename 按文本 `" -> "` 猜，文件名含 ` -> ` 会切错。现：`git -c core.quotepath=false … -z`（NUL 记录，天然不转义）+ 以 `git rev-parse --show-toplevel` 为基准 + 显式处理 rename 的第二条记录。**外锚同口径**：新增 `_anchor_path()`，相对路径一律以**仓库根**为准（否则在子目录跑 `ic audit` 会报"外锚：无"＝**锚校验被静默跳过**，`--anchor` 还会另写一个锚）。+ `tests/test_gate_paths.py`（8 项，真 git 仓库：中文/空格目录、含 ` -> ` 的文件名、子目录一致性、锁真实路径后另一身份必须 409）
- **表里放锁不留痕（cursor 实测）**：窗口离场（`leave_window`）与惰性对账判死进程这两条路原来是裸 `DELETE FROM locks` ⇒ 痕迹里只有 `lock`、没有 `unlock`，"这把锁是谁弄没的"查不出来。现统一走 `_release_window_locks()`：先查行、再删、**逐条**写 `unlock` 事件（带 `why`/`window_id`/`held_since`）
- **痕迹与任务行可能不一致（会让 `ic audit` 变红、之后每次提交都被钩子挡）**：`task_set_status` / `task_update` / `task_bump` 原来把**调用方原文**写进痕迹，而行里存的是**规范化后**的值（标题 trim、`owner` 小写、验收截断）⇒ 重放对不上。现统一**照抄写进行里的值**
- **任务状态机是死代码 + 一个真漏洞（cursor 实测）**：`_TASK_OWNER_MOVES` / `_TASK_COORD_MOVES` 没被查过，实际只判"是否终态 + 是否 coordinator" ⇒ `doing→todo` 与 `todo→done` 都能走通。其中 `doing→todo` 是**真漏洞**：倒回 todo 等于**解冻规格**，绕开"开工即冻结"。现状态机 = **两张表的并集，对谁都一样**（含 coordinator）；补 `(todo, stopped)`（没开工就放弃是合法收口）
- **终态任务还能调优先级（cursor 实测）**：`task_bump` 不看终态、也不走 `_task_priority`（曾能到 140）。现终态返回 `task_closed`，优先级统一走 `_task_priority`（封顶 99）
- **`ic audit --anchor` 可能给"伪造过的链"盖章（洗锚，cursor 实测）**：打锚前不验旧锚 ⇒ 改完再重算哈希后打一次锚，就把旧锚作废了。现**先用现有锚验一次**，不通过直接拒打锚（锚文件一字不动）
- **窗口离场／进程死不再自动中止案卷（M7，与宪法一致）**：原实现里 `leave_window` 与惰性对账会把主办窗口名下未封卷案卷**自动转 `aborted` 并封卷**，导致"人还在办事、会话一断案卷就死"——grok 实测 11:55 办公室 `w-beh...` 离场后案卷被 `[自动]` 封卷、随后写节点被 `case_sealed` 拒。现按 human 定稿（**案卷只随"事做完"关闭，与办公室/agent 在不在无关**）：程序只做两件**无争议**的事——摘掉主办窗口指针 + 记一行 `[自动]` 说明（**状态不动、不封卷**）；中止/办结**一律显式**（`abort`/`close`，带交接）。`/v1/windows/<id>/leave` 回执改为 `orphaned_cases`（`aborted_cases` 恒为空，保留字段）
- **`ic lock --force` 不核身份（第 1 道闸的洞）**：CLI 写着"仅 coordinator"，`store.lock_acquire` 只看一个布尔 ⇒ **任意 agent 用 HTTP body 就能抢别人的锁**（与 `lock_release --force` 不对称）。现与放锁一致：`force` 必须 coordinator
- `ic lock` 回执仍在打裸 UTC（`...Z`）——上一版只改了列表视图、漏了顶层 `expires_at`；补 `expires_at_beijing`，并在回执里写明"**续期＝重复执行同一命令**"
- `ic locks` 增加"剩 N 分"与**快到期提醒**（cursor：TTL 到了人还在写 ⇒ sweep 放锁、别人进来，"第 1 道闸自己消失"；默认 TTL 内写不完必须能续）
- **时间统一北京时间（各 agent 机器时区不一致导致对不上表）**：展示层原来用 `.astimezone()`（**机器本地时区**）⇒ 同一封信在不同机器上显示不同时间（实测四家常报四个时刻）。现**固定 UTC+8**：新增 `BEIJING` / `beijing_time()` / `beijing_hhmm()` / `beijing_stamp()`，**存储仍为 UTC**（`...Z`，排序比较用），**所有给人/给 agent 看的字段一律 `*_beijing`**（旧 `*_local` 键**已废弃并改名**：`created_at_local` → `created_at_beijing`、`last_seen_local`、`activity_at_local`、`expires_at_local`、`acquired_at_local`、`updated_at_local`、`first_seen_local` 同理），日志戳、`ic inbox` / `ic send` / `ic lock` / `ic locks` / `ic agents` / `ic task` / `/ui` 看板同步改。+ `tests/test_time_beijing.py`（9 项：**把机器时区改成纽约/伦敦/东京/基里巴斯，展示必须一字不变**）
- **跨窗认领必须按 workspace 作用域（M6-0，高危静默丢信）**：不做 workspace 过滤的长轮询窗口（常见于他项目的 MCP/CLI 会话）以前会把**别的项目**的信一并软认领；一旦被别窗认领，正确项目的守护/窗口在 `/v1/events` 与 `ic inbox` 里就再也看不到它 ⇒ **派单/来信静默丢失**（实测一次事故里 12+ 封如此被扣住）。现：① 窗口只认领属于自己 workspace 的信，他项目窗口**看得见但不认领**；② 每轮惰性对账回收「认领窗口已离场/已死」或「与信 workspace 不相容」的错位认领（历史脏数据自愈）；③ **窗口离场即释放**其名下认领。+ 新增 `tests/test_m6_claim_scope.py`（5 项）
- **`headless_timeout` 配置从未生效**：该键不在 `config._KNOWN_KEYS` 里，`config.load()` 会把它丢掉 ⇒ 守护的 `config.entry()` 永远读不到，单轮墙钟上限一直回落 1800s（`+` 新增 `headless_timeout` 与本次空转治理各键的 config 往返回归）
- **命令注入（高危）**：headless 命令模板把 `{prompt}` 套进引号时的引号逃逸 → 拒绝并提示改法
- 同一项目「绝对路径 + 裸目录名」合并时归档标记随机翻转（改为永远优先锚点元数据）
- `POST /v1/crew`（`up`）指向不存在目录返回 400 而非 500
- `headless_runs` / 守护 meta 遇到损坏（合法 JSON 但非对象）行不再让接口整体 500；负数 `limit` 不再误裁记录
- `add_project` 返回项补齐 `archived` 等字段，与 `list_projects` 同口径

## [0.4.0] - 2026-10-14

### Added
- **团队编排 `ic crew`**：一键拉起全队、发启动信、收信守护进程
- **收信守护进程**：长轮询 `/v1/events`，新信即时触发 headless 运行
- **心跳机制**：无信时定期触发规划运行，支持 `heartbeat`、`heartbeat_mode`、`heartbeat_prompt` 配置
- **状态自报**：`ic register --activity` 或 MCP `ic_activity`，coordinator 据此调配资源
- **多项目并行**：`ic crew up --dir` 按项目隔离守护进程、tmux 会话、信件 workspace
- **kickoff 信**：`ic crew kickoff --plan` 群发 high 启动信，自动附加合作约定

### Changed
- **并发安全**：`store.py` 写事务 `BEGIN IMMEDIATE`、多 SELECT 读快照 `BEGIN DEFERRED`、长轮询 `_seq` 与拆卸保护
- **边界校验**：`limit` 越界、`subject` 空/超长/换行、`body` 空/超 100k、广播归一化、非法名字、非收件人 `mark`、thread 可见性隔离
- **MCP 帧协议**：line-JSON 与 LSP `Content-Length` 双模；`_Watch` 首帧 `since=0` 补报；`--no-notify` 不推未读尾巴
- **crew 守护纪律**：`poll`/`heartbeat` 的 clamp（5–3600 / 0–86400）、workspace 过滤、多项目隔离、`down --dir` 只清本项目

### Fixed
- `watch` 的 `elif` 回归修复：`heartbeat_mode=always` 且同轮既有新信又到心跳点时不再连续双跑 headless
- 内置 member 心跳文案不再写「给 coordinator 发信」，避免凭空身份 `coordinator` 残留

## [0.3.0] - 2026-10-07

### Added
- HTTP API 长轮询 `GET /v1/events`，支持 `since`、`wait`、`workspace` 过滤
- MCP 工具 `ic_agents`、`ic_activity`
- 接入名单 `config.json` 支持 `launch`、`app`、`headless` 字段

## [0.2.0] - 2026-10-05

### Added
- 线程按参与方隔离：只能看到自己发出或收到过的回合
- 时间双份：`created_at`（UTC）与 `created_at_local`（本地时区）
- 广播 `*` 展开为除自己以外的所有已登记 agent

## [0.1.0] - 2026-10-03

### Added
- 基础留言功能：`ic send`、`ic inbox`、`ic read`、`ic reply`
- SQLite 邮箱（WAL 模式）
- HTTP API：`POST /v1/messages`、`GET /v1/inbox`、`POST /v1/messages/{id}/read`
- MCP 工具：`ic_inbox`、`ic_read`、`ic_send`、`ic_reply`

---

[Unreleased]: https://github.com/Robin9989Law/infocenter/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Robin9989Law/infocenter/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Robin9989Law/infocenter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Robin9989Law/infocenter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Robin9989Law/infocenter/releases/tag/v0.1.0
