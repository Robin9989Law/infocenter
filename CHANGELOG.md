# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-10-16（v0.6.0 之后）

> 追账：下面四笔当时只进了 commit message，没进本表（pi 承办，文档追码时发现）。`195f578`（重打外锚）属锚点例行，不列。

### Added
- **案卷状态机进既有哈希链**（`9cff5c8`，cursor 落）：立案 / 认领 / 强制接手 / 转办 / 暂停 / 恢复 / 中止 / 办结 / 重开，都在**同一个事务**里追加一行 `case_*` 事件（带 `case_` 前缀，免得和任务自己的 `create` / `status` 撞名），记 `from_status → to_status` + 主办窗口 + 署名。**`task_id` 留空、与 `project_coordinator` / 案卷 `acceptance` 同例** ⇒ 任务重放不看它，而封卷、转办、重开从此**改不掉而不动链**（旧链只管任务与锁，案卷的收局是套「写了就算」的空档）。+ `tests/test_case_chain.py`。

### Fixed
- **`ic reply --to` 是单值 ⇒ 收件人被静默缩窄**（`570c124`，pi 2026-10-15 报）：`send` 的 `--to` 是 `action="append"`，`reply` 不是 ⇒ `ic reply --to A --to B` **只发给 B 且不报错**，两边不对称。现与 `send` 对称：可重复、逗号也吃（服务端 `parse_recipients` 本来就拆逗号，差的只是 CLI 这一头）。+ `tests/test_reply_body_file.py` 3 项。
- **`/openapi.json` 跟不上实码路由，而且漏了不报错**（`7b2a9b4`，cursor 落）：`GET /v1` 和 README 都让新 agent 照这份机器可读说明接，源码里多了一条分发、表里没写 ⇒ 外人照着 404，内部一万次测试也发现不了。现在**精确路径 / 前缀切片 / `action` 子路径**逐条跟源码对齐，另补 `tests/test_openapi_contract.py`：拿分发字面与契约表求差，**漏写即红**（只在 CSRF 闸里出现、没有切片分发的前缀进 `GATE_ONLY_PREFIXES`，确实分发了但不该进契约的进 `NOT_IN_CONTRACT`，**都得写理由**，不许默默删）。
- **静态对账看不见循环生成的子命令**（`ba71662`）：对账靠扫源码字面，`for name in (...): sub.add_parser(name)` 这种写法扫不到 ⇒ 新子命令漏进对账表也不报。现抽出 `build_parser()`（`main` 只留剥全局参数、解析、分发），对账改为**从解析器递归内省**，并断言看得见 `crew pause|resume|abort|close` 与 `sync export|import|once`。

## [0.6.0] - 2026-10-16

> **这一版把「谁说了算」分清了**：全局只有一个人 **`manager`**（＝用户本人，旧名 `human`），**每个项目**另有自己的 **coordinator**（由 manager 指定）。
> 另外补了一个「接活钩子」（`--wait-for`：触发条件不许只写在信里）、`ic status` 的空钥匙灯、
> 看板减噪（默认只列未封卷）、发布卫生（真打一个包再打开看），以及一批重复实体收尾。
> 上一版（0.5.0）是首个公开版。

### Fixed
- **sdist 期最后一条构建告警：`Package 'infocenter.web' is absent from the packages configuration`**（pyproject，pi 承办 / opencode 复核）。控制台单页 `infocenter/web/index.html` 所在子目录 `web` **无 `__init__.py`**，但 `[tool.setuptools.package-data]` 里旧写法 `infocenter = ["web/*"]` 让 setuptools 顺着 glob 把它认成 importable 子包 `infocenter.web`，而 `packages` 里只有 `infocenter`。**只出在 sdist 那一步**：wheel 靠那条 glob 侥幸带上了 `index.html`（单看 wheel 会漏判），sdist 却报 absent 告警**且不打包 `web/index.html`**。改法（零新增文件）：`packages = ["infocenter", "infocenter.web"]` + `package-data` 用带引号的子包名 key `"infocenter.web" = ["*.html"]`（key 不加引号会被 TOML 嵌成 `infocenter` 子表、报 `must be array`）。判据全过：从仓外真打 sdist+wheel **无该告警**、两种产物**都含** `infocenter/web/index.html`；全量 547 测试 OK；按 L5 真装 wheel 后 `ic serve` 打开 `/ui` 返 200（控制台页 73521 B）。
- **协调者分层那晚的漏网（kimi 追审：阻断 4 条，全部落修）**：
  ① **`_human_voices()` 是 SQL 里的角色比较**（`WHERE role = 'coordinator'`）——迁移把 role 改写成 `manager` 之后
  这个查询**永久落空**，看板第 1 条线（「人话 / 人话过期」）**静默退化成只有 human**；
  ② **`seed()` 裸插原始 role** ⇒ **测试库与生产库口径不一致**，绿灯是**假象**（正是它把 ① 盖住的）——现在 seed 也走
  `_role()` 归一，并补一条**走归一化路径**的回归测试（`test_coordinator_scope`：人话判定认 manager）；
  ③ 控制台 3 处 `a.role === "coordinator"`（coord 角标、立案指派下拉的标注）⇒ 改名后**永远不显示**；
  ④ 心跳提示词里那句「`ic agents` 里 role=coordinator 的那位」+ **三处把旧词断言锁死的测试**
  （`test_quality_crew` ×2、`test_idle_guard` ×1）⇒ 成员按提示**找不到主协调**（"自己的测试在护旧词"）。
  另修文案 / 语义四组：kickoff 一串（docstring / 报错 / 回执）、`manager_misconfig` 的返回串、README 8 处、
  `mcp_server` / `httpapi` 注释；以及**启动信的语义问题**——全局 manager 不该在启动信里自称 coordinator
  （项目里的 coordinator 由 human 指定，权源不同）。**教训再记一遍：扫角色比较要连 SQL 谓词一起扫。**
- **提示词里补上「有 N 卷在等你」**（cursor 追审的建议：补进**已经会跑**的收信 / 心跳提示词，和「未读数」同类；
  **不做成通知、不发信、不点名**）。来源：`--wait-for` 那盏灯只在"有人看 `ic status`"时起作用——
  成员的心跳提示词里不提它，那盏灯在守护路径上就永远看不到（当初那个"不会响的触发"在心跳上还在）。
  现在 `crew._waiting_count()` 查一次共享状态，收信提示词与心跳提示词（含自定义模板）都会带一行
  `有 N 卷在等你：ic cases --wait-for <我> 看是哪些——要么接着办，要么转出去`。

> **这一版把「谁说了算」分清了**：全局只有一个人 **`manager`**（＝用户本人，旧名 `human`），**每个项目**另有自己的 **coordinator**（由 manager 指定）。
> 另外补了一个「接活钩子」（`--wait-for`：触发条件不许只写在信里）、`ic status` 的空钥匙灯、
> 看板减噪（默认只列未封卷）、发布卫生（真打一个包再打开看），以及一批重复实体收尾。
> 上一版（0.5.0）是首个公开版。

- **`pyproject.toml` 的 `license` 改成 SPDX 字符串，清掉打包弃用告警**：原先写 `license = { text = "MIT" }`（表形式），每次构建都刷
  `SetuptoolsDeprecationWarning: project.license as a TOML table is deprecated`，setuptools 给的期限是 **2027-02-18**；更要紧的是它把 wheel 元数据落成旧的自由文本字段
  `License: MIT`。现在写 `license = "MIT"`（PEP 639），元数据变成规范字段 `License-Expression: MIT`，`LICENSE` 文件照旧进 `dist-info/licenses/`。
  这条写法要求 **setuptools>=77**，故 `[build-system] requires` 一并从 `setuptools>=68` 提到 `setuptools>=77`（构建走隔离环境，本地无需装）。
  改前/改后各构建一次对照验证：改前那次告警两条、改后那次告警为零（`Metadata-Version` 两版都是 2.4，差别只在 license 字段本身）。
  顺手记一笔**没动的既有告警**：`Package 'infocenter.web' is absent from the packages configuration`——改前就有，wheel 里 `web/index.html` 靠
  `[tool.setuptools.package-data]` 正常带上，功能不受影响；要清它得碰 `[tool.setuptools]` 的包发现配置，属另一笔，不夹在这里。

### Added
- **锁续期的新规矩：盘上没动过就不给第二把租约（堵「拿着钥匙不干活也能一直续」的结构洞）**（manager 2026-10-16 裁定四件事之一，pi 承办 §〇 文案：README / SKILL / CHANGELOG；判定与错误码由各承办人落 `store.py`/`cli.py`/`httpapi.py`，与上一笔改名分开提交）。一把钥匙默认 TTL **1800 秒＝一次会话一把租约**（＝一次 headless 墙钟上限，好记）。续期时服务端对**本次要续的、当前由持有者持有的每条路径**看盘上自该锁 `acquired_at` 起**有没有写入痕迹**（`os.stat(p).st_mtime >= acquired_at`；文件不存在＝无痕迹；秒级比较、宁松勿误杀）：**全部有痕迹 ⇒ 照旧正常续期（`acquired_at` 刷新）**；**任一没痕迹（整把语义 ⇒ 整笔拒）**——普通续期返 **409 `no_progress`**（已有锁不动、不自动放，文案照现有 409：别硬写 / 记进待办 / 先做别的 / 到期自然回到可拿），加 `--stay` 允许但 `acquired_at` **不刷新**且 `expires_at` 不得晚于 `acquired_at + 2×TTL`（拿锁后最多 1 小时），已到则 **409 `stay_exhausted`**（先 `ic unlock` 放一次再拿），无进度续期（`stay=true`）在链上 `lock` 行带记号。**零新增表/列/通知机制**：只用现成的 `acquired_at` + 盘上 `stat`（`crew.py` 的 `_progress_snapshot` 同一手法）。
- **办结门槛：办结前必须先写「怎样算完」，否则服务端拒**（同上派单，§〇 文案）。`close`（含控制台「办结」）前没写 acceptance ⇒ **409 `no_acceptance`**；**中止（abort）不要求**（没做完的事不该被逼着编一个验收）。判据口径**说死**：那行「怎样算完」要写成**第三个人能照着复跑的命令或测试名**，**不许写「已经差不多了」这类形容词**（机器判不了、人也复现不了——这是产品里现成的「锚定测试」位置；任务侧开工即冻结规格本来已要求）。未封卷且没写时列表 / 看板先标 **⚠ 没写怎样算完** 提醒（与续期那笔同盘上痕迹机制、不新增表）。
- **治理动作留痕：指定/更换项目的 coordinator 进哈希链**（2026-10-16，晚于分层落地的自查）。分层做完才发现：**「谁是协调者」这件事改起来没留痕**——痕迹（`task_events`）只覆盖任务 / 案卷 / 锁，项目不在内。指定协调者是**治理**动作（它决定谁能改里程碑、谁抢得了锁），查不出来不行。现在 `add_project(coordinator=…)` 在同一事务里追加一行 `project_coordinator`（`from → to` + 工作目录 + 署名），**空 task_id 与案卷的 `acceptance` 事件同例**；`ic audit` 的重放与哈希链照旧认（现场换了一次人复验：链完整、重放一致）。+ `tests/test_coordinator_scope.py` 1 项。

- **发布卫生：真打一个包再打开看**（2026-10-16 定，PLAN §14.4 的 L5）。现场：v0.5.0 发布前 `pip wheel .` 出来的 wheel 只有 18 个文件、**没有 `infocenter/web/index.html`** ⇒ 外人 `pip install` 完能起服务、但**控制台 `/ui` 404**；而这一条**内部跑一万次测试都发现不了**（测试全从源码树读文件，只有"装出来的那份"才有这问题）。现在 `tests/test_packaging.py` 不查配置、**真的打个 wheel 再打开看**：控制台单页在不在、版本号一不一致、`ic = infocenter.cli:main` 入口在不在、六个关键模块齐不齐（构建不了就跳过并说明，不假装绿）。README 的收尾判据另加一句：**发布前真装一遍**（venv 装 wheel → `ic --version` → `/ui` 要 200）。
- **接活钩子：`--wait-for`——「触发条件不许只写在信里」**（2026-10-16 定，PLAN §14.4 的 L4）。`ic case <id> --note "为什么" --wait-for <身份>` 把「我在等谁」落成案卷状态「待 X」（`waiting_for` 这一列**本来就有**——回复时还会自动维护——缺的只是"人怎么设、被等的人怎么看"）；**必须带 `--note`**（同 `--status` 的口径：状态变更要留一行，不许静默）。被等的人敲 **`ic status` 多一盏灯**：`⚠ 有 N 卷在等你：<卷号> <标题>…`，只算**未封卷**的（封卷＝归档，不该再催人），`--json` 带 `waiting_on_you`；`ic cases --wait-for <身份>` 可以只看等他的。来源是实测：SKILL 那卷的验收写着「触发 = kimi 宣布修订冻结」，而 **kimi 的 TODO 里没有这一项** ⇒ 那是个**永远不会响的触发**，事就那么悬着。现场验过：给 SKILL 那卷写上 `--wait-for kimi` 后，kimi 敲 `ic status` 立刻看到那一行。+ `tests/test_wait_for.py`（10 项）。
- **CLI 表面对账（`tests/test_cli_surface.py`）**：①**静态**——每个 `add_argument` 定义的开关都要在 `cli.py` 里被 `args.<dest>` 读到（白名单写明理由，目前只有 argparse 自己消化的 `--version`）；②**冒烟矩阵**——每个子命令一条最小 argv，桩掉 HTTP 客户端后必须**真的打到**那条路（HTTP 类打到 `call`，`serve/stop/status/probe/mcp/crew/sync` 打到各自入口），不许抛非 `MailError` 异常；新加子命令忘了进表会红。**拦不住的那类**（开关只在某个分支里被读，`--status` 正是这种）已登记：要机器化得先把 parser 抽成可构造的，单开一笔。
- **`ic status` 空钥匙灯**：盘上有改动、却没有任何未过期写锁覆盖它 ⇒ 一行 `⚠ 盘上有改动没人持钥匙：…（要么提交，要么 ic lock 再动。只提示，不拦）`，`--json` 带 `unlocked_dirty`。来源：实测两次——写了文档/测试、放了钥匙、**忘了提交**，那几行躺在工作区里，别人又不能替你提交 ⇒ 收口卡住。读不到 git / 读不到锁就闭嘴（灯不挡看状态）。
- **看板减噪：默认只列未封卷的，没实质内容的打 `[信]` 标（不藏）**。任何 thread 都能当案卷 ⇒ 每封派单/回信/报告都长成"卷"、还各自挂着「⚠ 没写怎样算完」（实测 148 条里大半是**已封卷的历史**，混在一起才显得吵）。现在：`_case_view` 出一个**派生标记** `substantive`（验收 / 里程碑 / WBS 三选一，**不给信加字段、不建表**）；`ic cases` 默认只列未封卷、把没实质内容的打 `[信]` 标、封卷的折成一行（`--all` 看全；`--json` 不动），控制台的案卷栏同一口径（折已封卷、给信打标签）。**判据被真数据修正过两次**，两次都记在案：①第一版按 `substantive` 过滤——查数据发现【触发 S6】那类**派单线程是活**（在办、5 封信，却啥也没有）⇒ 改成不藏；②"92% 是信"这个数我一开始读错了（里面大半是已封卷的历史，不是活）⇒ 才有"默认只列未封卷"。**已知缺口（登记）**：展示层没法可靠区分"真案卷 vs 派单线程"（三条立案路径的列完全一样）⇒ 要么立案时留显式标记、要么给派单线程一等公民身份，属改数据模型，**单开一笔**。

### Changed
- **身份改名：`human` → `manager`（`human` 作为旧名继续被接受，不做迁移）**（manager 2026-10-16 拍：manager 与 human 合一，人的身份改名为 `manager`）。下面「coordinator 分两层」那条把**全局叫 `manager`、人叫 `human`** 分成两码事；今天裁定二者**合一**——**全局只有一个人 `manager`（＝用户本人）**，`role=manager` 的名册条目就是这个人。**两层口径定为**：全局一个 `manager`（人），每个项目一个 `coordinator`（项目内路径的写锁/派单归它，项目外归 manager）。`human` = **旧名，仍被接受**（CLI 默认 `--as`、`_must_human` 护栏、历史痕迹与哈希链里仍保留 `human`），**不做数据迁移**（`.py` 各归 cursor/grok，本轮只动 §〇 文案：README / SKILL / CHANGELOG）。配套：`--as human` 示例→`--as manager`；「请 human 裁定 / 给 human 发信」→「给**本项目的 coordinator**（查不到回落 `manager`）」；`ic agents --role` 说明同步。（真相源：名册 `manager` role=manager、`opencode` role 清空——他仍是信息中心/01_ICLR2027 的 coordinator；`~/.infocenter/config.json` 已同步；探针验过 opencode 抢项目外路径被拒、manager 放行。）
- **重复实体收尾（L6）+ 一条纪律落地（L7）**：
  - **`crew._parse_utc` 与 `store._parse_utc` 是「同名两义」**（一个返回 `datetime`、一个返回 epoch 秒），
    而且**两边各抄了一遍时间格式串** `%Y-%m-%dT%H:%M:%SZ`——正是「改一处忘一处」那种。现在合并：
    `crew._started_at_epoch()` 只做换算，**解析只有一个真相**（`store._parse_utc`），顺带删掉没人用的 `calendar` import。
  - **机器扫全仓**（零引用函数 / 同名函数 / 重复长字面量）：零引用只剩 5 个 `BaseHTTPRequestHandler` 框架钩子；
    `server.stop`（模块级）与 `serve` 内的信号闭包是**误报**（不同作用域，不是重复）；
    store 里那些 SQL 字面量重复（`SELECT * FROM windows…` ×12、deliveries ×4 等）**判定不抽**——
    抽成 helper 等于每个查询加一个函数、行数并不省（跟减法轮当时的结论一致，这次复核仍然成立）。
  - README 立**第四条纪律：动手 / 删除前先读台账**——删掉的东西对后来的人是不可见的，不读台账就容易
    把别人刚删的又加回来（减法轮那晚的经验）。


- **coordinator 分两层：全局叫 `manager`，每个项目的叫 `coordinator`（由 human 指定）**（human 2026-10-16 拍，四条裁定逐条落码）。以前 `coordinator` 是**一个全局角色**（config/库里 `role=coordinator`），一个身份管所有项目——**一个词管两个范围**，正是这一晚一直在打的病（一个概念两套口径）。现在：
  - **`manager`**（旧名 coordinator，配置里写旧值也认，读进来一律记成 `manager`）：**全局**管理者，管名册与 hub 级；**不再自动拥有某个项目里的权**。
  - **`coordinator`**：**某个项目**的那个人，落在 `projects.coordinator`（新列，在线迁移自愈）。管这个项目的里程碑 / WBS / 建任务 / 任务的 done·stopped / 抢锁与替成员落地。**由 human 指定**（只有 human 能设：`ic --as human projects --project <路径> --coordinator <身份>`，或控制台 → 项目设置 → 协调者；空值＝清空）。
  - **没指定就拒**（`no_coordinator` 409，报文说清怎么指定）——不默认兜底；**项目在、`coordinator` 为空**才拒。**不属于任何项目**的卷（空 workspace / 没登记过的目录）**回落 manager**；锁与提交闸只有路径 ⇒ 按路径找项目（取最长匹配），找到按那项目的 coordinator 判、找不到回落 manager。
  - **`human` 天然算**（它是定这两个角色的人），否则你自己的控制台按钮会被自己拒。
  - **一次性填充**：迁移时把已有项目填成当时的全局那个（从库里 `agents.role` 取，不写死名字）⇒ 落地那一刻不断档。
  - **顺带补上一条旧账**：`agents remove` 的「只有 human 能移除成员」以前**只在文档里**、机器没兜（CLI 直接调删除、不查身份）——现在 `_must_human` 是真护栏（新口径的两条 human-only 也用它）。
  - 面：`projects` 表新列 + `PUT /v1/projects` 收 `coordinator`（按调用者判 human）+ CLI `ic projects`（列项目、指定协调者）+ 控制台项目设置一个下拉；README 术语表/mermaid/kickoff/WBS 负责人/`human ≠ coordinator` 那几处、`config.py` 字段说明、`SKILL.md` 8 处一并改齐（**改了行为就必须改说法**——这一晚最大的教训）。
  - + `tests/test_coordinator_scope.py`（**12 项**：没指定就拒 / 指定后能做 / 别人项目不能插手 / **manager 不再自动有项目权** / human 天然算 / 清空＝回到没指定 / 无项目回落 manager / 只有 human 能指定 / 名册里没有的名字拒 / 锁按路径判 3×3 / 旧值兼容）。

## [0.5.0] - 2026-10-16

> **首个公开版。** 这一版把「一台机器上多个 AI agent 互相派活」做成了真能跑的东西：任务队列（工作包）、
> **一把钥匙的写锁**（锁＝只读，落到文件系统权限位）、**提交闸**（每个改动都得有未过期写锁）、
> **留痕哈希链 + 外锚**（链头钉进 git，防整表重做）、**案卷 / WBS 看板**、**空转采样**（触发数×产出并排看）、
> **「怎样算完」的收尾判据**。整条线上还有一个刻意的取舍：**能不沟通就不沟通**——协调尽量走共享状态
> （库里的台账 + 看板），而不是互相发信。

### Removed
- **减法轮（勿增实体）落在别处的部分**（human 2026-10-16 下令：四家各自**独立**审全仓、**只删不加**；coordinator 收口）：`cursor` 删 8 个无调用方的函数/导入（`paths.default_url`、`client.running_meta`、`config._save_agents`、`sync._utcnow` 这类"纯转调的壳"）、看板里 4 个点不到的 JS 函数、`sync.is_wire_name` 的第二份实现（并入 `store._is_wire_name`）；`pi` 删 `EXPERIMENT-独立评审.md`（实验已完成、零引用）、`需求文档/02-服务中心调研报告.md`（416 行、零引用、文件自述"不构成本期排期"）、`cli._SYNC_COUNT_KEYS` 与 `sync.SEGMENT_RE` 两处重复常量；`kimi` 删 `cli._parse_utc` 的本地副本（与 `store._parse_utc` 逐输入等价）；`opencode` 删控制台死 CSS `.dmon` / `.dmon.on` / `.dmon.off`（正文只有 `dmon-btn` 在用）、README 里过期的「当前 158 个」（实测 468）与 `pytest` / `unittest` 两种口径二选一。四家落地部分**净减 597 行**（+23 / −620，14 个文件）；全量 468 项绿，coordinator 另在**仓外**独立复跑两次（`c285821`、`903f5c9`）确认。**可回滚点**：起点 tag `pre-subtraction-round` + 分支 `backup/pre-subtraction-59dcb1e`（均已 push），本机快照 `~/.infocenter/backups/减法轮前-20261016-0301/`。四份**清单**在 `_work/减法-<名字>.md`（不在 git 里），案卷 `m1a140f2e6cc41e95b` 逐条记着"删了什么·凭什么·哪个 commit"。
- **减法轮里"有证据但没删"的**（都是「改」不属「删」，登记待办）：**pid 判活有 4 份拷贝**（`crew` / `server` / `store`×2）——四份语义确实分叉（`<=0` 还是 `<=1` 判死、`None` 的含义正相反）；其中 `server.pid_alive` 少了 `<=1` 护栏，而 `os.kill(0, 0)` 实测会成功 ⇒ 单看这个函数，pid 0 会被判成"活着"（眼下两个调用点都被别处挡住：`stop()` 先查 `meta.get("pid")` 真值、健康检查那条还要求服务真的能应答）。**原子写 JSON 有 4 份**（只有 `sync` 那份带 fsync + 失败清理；另抽 helper 要带序列化参数，行数并不省）。**`crew._api` 与 `client.call`** 里是同一段 `ApiError→MailError` 翻译（合并会把 `ensure_server` 带进守护循环，改变"服务不可达"时的行为）。**`.githooks/pre-merge-commit` 与 `pre-commit` 内容相同** —— **决定不软链**：非快进 merge 确实不回落 `pre-commit`，两个文件各有必要；软链省一份字节却让"读钩子"依赖 git 的符号链接，面更大、收益更小。（四项里 **pid 判活、原子写 JSON、`ApiError→MailError` 翻译**已于 2026-10-16 分别由 `63ddacf` / `7213615` / `befb5f3` 收口，见下面 Fixed；只剩 githooks 那一项是**故意不合**）
- **视图里同值别名**（减法轮，没人读的那几个）：案卷不再另给 `subject`/`owner`/`claimed_by`/`wbs`/`lines`（留下 `title`/`owner_agent`/`owner_window`/`wbs_path`/`text`）；窗口不再另给 `id`/`first_seen`/`last_seen`（留下 `window_id`/`first_seen_at`/`last_seen_at` 和 `*_beijing`）；项目汇总不再另给 `cases_total`/`doing_cases`/`cases_active`/`window_count`，`counts.doing` 也不再复制 `counts.open`。看板、`ic case`/`ic cases` 读的是留下的那些字段，输入侧仍接受 `wbs` 作为 `wbs_path` 的写法。控制台顺手去掉没人挂上的样式 `has-stat`，以及项目行对不存在的 `counts.active` 的回退。

### Fixed
- **JSON 落盘统一走 `store.atomic_replace`（fsync + 失败清临时文件）**（human 2026-10-16 对账轮，`7213615`；即「减法轮里‘有证据但没删’」登记的原子写那一份）：`config` / `server` / `crew` 原先各自固定 `*.json.tmp` 再 `replace`，而且**不 fsync**；`crew` 的 pid 记录更是直接 `write_text`——掉电 / 崩溃在半截时留下截断的 JSON。现在都先写同目录临时文件、`fsync`、`os.replace`，失败删临时文件。**成功后的 JSON 字节不变**（`indent` / `ensure_ascii` 仍由各调用方决定）；**不 fsync 目录项**，与旧 `sync` 同口径。+ `tests/test_atomic_replace.py`（6 项）
- **`ApiError→MailError` 的翻译只留一处**（human 2026-10-16 对账轮，`befb5f3`；即上面登记的 `crew._api` ↔ `client.call` 那一份）：两处曾是同一段翻译，但 `client.call` 先 `ensure_server` 再翻译 ⇒ 若把守护也并过去，**守护循环会顺手把服务拉起来**，改变「服务不可达」时的行为（那是真分叉，不是重复代码）。现在只有 `client.request_mail` 带翻译：`client.call` 仍先 `ensure_server`，`crew._api` 只调 `request_mail`，**守护不自动拉服务，语义逐字保持**。+ `tests/test_request_mail.py`（3 项）
- **`test_m2_client_sends_x_pid` 在 agent 会话里单跑必红**（pi 2026-10-16 查心跳时的红，测试隔离）：守护给 headless 子进程注入 `INFOCENTER_NO_PID=1`（防短命进程改写宿主窗口 pid），`client.request` 见到这个开关就把 `X-Pid` 与请求体里的 pid 一起丢掉 ⇒ 用例里显式上报的 5432 根本没上路，`get_window(...)["pid"]` 是 `None`。全量绿只是因为 `test_d2_commands` 先行 `pop` 了这个变量（与已修的「测试宿主 env 泄漏三处」同一类：“单跑即现形”）。现 `P2TestBase.setUp` 进场先清 `INFOCENTER_NO_PID` / `INFOCENTER_WINDOW` / `INFOCENTER_AGENT`（要测「抑制」本身的用例自己设变量，不靠宿主漏进来）。不改产品代码、不新增用例：**HEAD 482/482 仍绿**，并在带 `INFOCENTER_NO_PID=1` 的会话环境下单跑该用例与整模块（29 项）均绿
- **`ic case <id> --status` 单独用默默不看**（human 2026-10-16 点名，`a887350`，实测连踩两次）：`--status` 只在「写节点」那条路上被带上（状态变更天生要留一行节点，看板才知道你为什么改），单独给就静默落到 `else: GET` 分支 ⇒ **看着像改了，其实只把案卷显示了一遍**。现在不在那条路上（没给 `--note`，或同时给了 `--milestone-add` / `--milestone-done` / `--acceptance`）直接报 `status_needs_note` 并在报文里给出正确写法，**不猜、不静默**；列表侧 `ic cases --status` 是筛选，不受此限。+ `tests/test_hardening_p2.py` 3 项（单独给 ⇒ 退出码 1 + 报文含 `--note` + **没打服务端**；带 `--note` ⇒ 照旧送出；`--milestone-done` + `--status` 一样不许悄悄吞）
- **pid 判活收敛到 `store.pid_alive`，并补上 `server` 缺的 `<=1` 护栏**（human 2026-10-16 对账轮，`63ddacf`；即上面「减法轮里‘有证据但没删’」登记的那一份）：旧 `server.pid_alive` 没有 `<=1` 护栏，而 `os.kill(0, 0)` 实测会成功 ⇒ 单看这个函数，**pid 0 会被当成服务还活着**（眼下两个调用点都被别处挡住，属潜伏而非现网可触）。现在 `crew` / `server` / `store` 里的四份拷贝收敛为一处，`<=1` 一律判死；`not_dead` / `running` 与旧 `_pid_dead`、`_pid_running` **逐输入保持原语义**（`None` 一边「不算死」、一边「没在跑」，没顺手改变行为）。+ `tests/test_pid_policy.py`（11 项逐输入对账）
- **黄灯对「带目录的相对路径」静默失效**（kimi 2026-10-15 追审阻断①，已复现）：`_preexisting_dirt` 用 `git -C <文件所在目录> status -- <原始路径>` 嗅「拿钥匙前盘上有没有别人的改动」——从仓根 `ic lock infocenter/cli.py` 时，原始相对路径会被 git 当成 `infocenter/infocenter/cli.py`（**路径错位**）⇒ `rc=0`、输出为空 ⇒ 被当成「干净」，**黄灯不亮也不登记**（提交时那句提醒一起消失）。现路径先转绝对再问 git（旧测试全用绝对路径，所以没暴露）。+ 3 项回归（相对路径 / 带目录的相对路径 / 从仓库外跑）
- **一次拿多个文件、中途失败 ⇒ 前面已成可写的留在「没钥匙却能写」**（kimi 2026-10-15 追审阻断③）：`lock_not_writable` 的回滚只回滚了 DB，**盘上的权限位没收回**——第一个文件已经 chmod 成可写、第二个失败整笔回滚，于是第一个文件「没有锁却可写」，**锁＝只读被静默破坏**且没人知道。现失败时先把事务回滚、再按「这个文件回滚后还有没有活锁」决定是否收回只读（我续期时已持有的那把不动，否则会把别人的写权限打掉）。+ 2 项回归（多文件失败收回 / 续期失败保住自己的）
- **放钥匙没能改回只读时静默吞掉 `OSError`**（kimi 2026-10-15 提示级）：`_chmod_locked` 只管去 chmod、失败就算了 ⇒ ACL / 只读挂载 / 别的进程又改回去时，「锁＝只读」只剩嘴上说说。现它**返回失败原因**，`ic unlock` 回执带 `stuck`、命令行当场说一句（**只提示不拦**）。+ 1 项回归
- **`ic queue --merge --id ""` 静默变成「合全部」**（kimi 2026-10-15 提示级）：`--id` 拆完是空列表，跟「没给 id」长得一样，而「没给 id」的语义正是「把还没落的都落掉」——手一滑就把台账全合了，**且不可撤销**。现显式空 id 给干净的 `invalid_id`（不写 id 仍照旧全合）。+ 1 项回归
- **续期会把登记过的 `dirty_at_lock` 抹掉**（kimi 2026-10-15 提示级）：`--changed` 只嗅命令行点名的路径，续期带进来的 `dirty` 是空的 ⇒ 旧代码照样覆盖，「本来就有别人的改动」这条登记凭空消失、提交那一刻的提醒也没了。现 `ON CONFLICT` 只在**这次嗅到了**才覆盖，空的保留原值。+ 1 项回归
- **旧排队模型的话还留在注释/报错里**（kimi 2026-10-15 提示级）：「做完通知下一个／30 秒不应就叫下一个顶上／队列里第一个」这些字**模型早拆了**，字还在 ⇒ 后来人照着字去理解就全错。现改成现行口径（「不排队、不通知：记一笔，各人照自己的 TODO 轮」）
- **冷却一刀切：被冷却的 agent 连「新指令」都收不到**（human 2026-10-15 点名）。冷却本意是治「模型不可用」，但旧实现在冷却期内**完全不收信**（`continue` 跳过 `GET /v1/events`）⇒ 别人发来的高优先级活被一起关在门外。现在冷却期内仍用 `wait=0` 探一句（`_urgent_mail_waiting`），**有高优先级新信就提前结束冷却**、立刻处理（并留日志、清冷却元数据）。**只认「进入冷却之后新到」的信**：失败时游标不前进，那封没投出去的信会一直被看见，拿它当新指令就是「进冷却 → 立刻被自己打断 → 再失败」的死循环（实测：pi 的模型名有歧义时每分钟 36 次 headless、1399 次触发 0 产出、564 次进冷却、279 次提前结束）。
- **超时被判死 ⇒ 必然空转的死循环**（human 2026-10-15 点名）。旧实现把「连续超时」当「连续失败」⇒ 进 1 小时冷却；而判定超时可以配得比 `HEADLESS_TIMEOUT`(1800s) 短得多（pi 配了 300s）⇒ **慢模型必被判死**。现在**超时不算失败**（`_cooldown_failure`/`_should_cooldown`）：超时不涨失败计数、**永不进冷却**，只按指数退避重试（游标留在原地、下一轮原样重投，封顶 `_HEADLESS_RETRY_MAX`=300s）；额度/鉴权（quota）仍立刻冷却，真失败累计到阈值仍冷却。另把 pi 的 `headless_timeout: 300` 改回默认（1800s）。+ `tests/test_crew_newmail_cooldown.py`（13 项）
- **提交后自动放钥匙的身份取错（多 agent 共仓时形同不存在）**：`post-commit` 先读 repo-local 的 `infocenter.agent`（一个仓只有一份，多 agent 会互相覆盖）⇒ 别人提交后放不掉自己的钥匙。现**优先取 `INFOCENTER_AGENT`**（守护给每次 headless 运行设的环境变量，才认得出"这次是谁在提交"），repo-local 只作兜底
- **`ic queue` 往只读 TODO 写那行不再自己 chmod**（q416）：被锁过又放回的文件停在只读态，包括提出者自己的 `TODO-<名字>.md`。库里提议落成后，`_todo_add` 若发现该文件已是 444，先 `lock` 自己的 TODO（拿到钥匙后服务端把它改成可写），写入再 `unlock` 放回只读；拿不到钥匙就 `todo_unwritable` 停下，**不再临时加 u+w**。文件还不存在、或本来就可写：仍直接写、不拿锁。+ `tests/test_todo_handoff.py`（只读时先拿钥匙、写完放回只读；钥匙被占则停且不改权限）与 `tests/test_review_kimi.py`（没钥匙不 chmod；可写路径不变）
- **`ic lock --ttl` 的 30 秒下限只写在码里**：`_lock_ttl` 一直是 `max(30, …)`，`--ttl 3` 实际拿 30s，但 help 只说「默认 1800，最长 12h」。help 补「最短 30」
- **「接手别人没干完的活」看不见**（独立评审收尾实测）：钥匙只声明"这文件现在谁能改"，不声明"盘上哪一行是谁写的"。上一手改到一半就还了钥匙（或锁到期被回收）⇒ 内容还留在盘上没提交；谁后拿到钥匙、谁提交，`git commit -- <文件>` 提交的是**整个文件的当前内容**，那半截活就记在后来的提交名下（提交信息、作者都认不出真正是谁的——本仓 git 作者全场同一个用户）。现在两个当口都亮黄灯（**不拦**）：① `ic lock` 拿钥匙前先嗅一眼**命令行点名**的路径（`git diff`），盘上本来就有未提交改动 ⇒ 当场说明「改了哪几条、+几/−几、第一行是什么」并登记在锁上（新列 `dirty_at_lock`）；② `ic guard` 提交那一刻再说一遍。`--changed` 带进来的路径不嗅（那是自己刚改的）；新文件 / 非 git 目录一律跳过。+ `tests/test_lock_inherited_dirt.py`（9 项）
- **测试宿主 env 泄漏三处**（在 agent 会话里跑测试必红）：被测子进程/请求继承宿主的 `INFOCENTER_AGENT`/`INFOCENTER_WINDOW` ⇒ 身份被预绑或窗口被别人先占。`test_quality_mcp._env()`、`test_lock_queue`、`test_m9_locks` 现在进场先清这两个变量（原先靠 test_d2 先行清环境的执行顺序遮蔽，单跑即现形）
- **`ic lock` 拿了钥匙文件还是 444（chmod 静默失败）**（grok 实测）：`_chmod_writable` 以前是「尽力而为 + 把 `OSError` 悄悄吞掉」⇒ 钥匙在手却写不了、且没人知道为什么，只能再锁一次碰运气。现在它**返回失败原因**、拿钥匙时**复核**是否真的可写；做不到就**整笔回滚 + 报 `lock_not_writable`（带路径 + 原因）**——钥匙要么能用、要么明说不能用（文件还没建、先锁后建仍照旧放行）。同时**放钥匙不再按「这一行的路径」盲写 444**：同一个文件（换写法 / 硬链接 / 大小写不敏感卷）若还有**活锁** ⇒ 保持可写，只有真没人拿了才回只读（否则会把另一个持有者正拿着的写权限当场打掉）。过期回收、窗口离场同一套规矩。+ `tests/test_lock_key_writable.py`（7 项）
- **`ic queue --merge --id q1,q2` 直接 500**（实测）：`--id` 是 `action="append"`，逗号写法整串被当成一个 id ⇒ store `int()` 抛 `ValueError` ⇒ 未处理异常 ⇒ 500，人只看到「服务器内部错误」。现在 CLI 拆逗号（`q1,q2` / 重复给 / 单条都认），store 坏输入给 **400 `invalid_id`** 并说清该写成什么样。+ `tests/test_queue_merge_id.py`（6 项）
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

### Added
- **封卷前的一盏灯：还有里程碑没勾就念出来**（human 2026-10-16 点头；**只提示、不拦**）：封卷不可逆（要改只能 `reopen`），里程碑封卷后同样改不动。实测踩过——先封卷、后勾最后一个里程碑 ⇒ 被 `case_sealed` 拒，只能 `reopen` → 补勾 → 再封，白折腾三下。现在 `ic case <id> --note "…" --status 办结|中止` 会**先**把没勾的里程碑逐个念出来，再照旧封卷。非封卷状态不多问一句；案卷读不到（不存在 / 服务不在）就闭嘴——黄灯不挡动手。+ `tests/test_hardening_p2.py` 4 项（念出来但照旧封 / 都勾了不吭声 / 非封卷不多问 / 读不到也不挡）。**控制台那排封卷按钮也补上了同一盏灯**（点「办结 / 中止」时把没勾的里程碑列出来，可取消——不是硬拦）。
- **死信拒收：写给「不存在的身份」直接拒（第 5 道闸）**（kimi 2026-10-15 提、human 2026-10-16 拍「要」）。缺口长这样：`--to` 里写个打错的名字或者已经退役的身份，程序照样报「已送出」、库里也留一封信，但**那份投递谁都读不到**——话说了没人听，而且看起来还是成功的。这是不可逆的损失，所以机器在这里兜底：收件人不在册 ⇒ **422 `no_such_recipient`**，报文里直接写清怎么办（让他 `ic register --as <名字>`，或把人加进 `config.json` 名册），**且不留任何残迹**（不建身份、不落信）。**在册 = 库里注册过的 ∪ 名册里列着的**（后一半必须有：`ic crew kickoff` 是**按名册群发**开工信，名册里的新同事可能一次都还没开口）；带点的跨节点名（`robin.kimi`）不归本机名册管，照发给同步层。+ `tests/test_dead_letter.py`（11 项：打错/退役/名册新人/注册过/给自己/广播/立案始信同受管且整笔回滚/回复不受影响/路由 422/配置坏掉也不误拒）
- **SKILL 副本漂移检查：`ic status` / `ic guard` 各出一行**（kimi 2026-10-15 追审阻断②：README 写了这句承诺，**代码里没有**——先落码兑现）。交互会话读的是**安装副本**（`~/.agents/skills/infocenter/SKILL.md`），仓库那份改了它看不到；两份真相靠人记一定再犯（实测安装副本停在 10-14）。现在两边内容不一致就出一行（**优先软链**：指向仓库那份的不算漂移）；**只提示不拦**（SKILL 正在修订时本来就该先漂着：判断「该不该现在同步」要靠人）。`ic status --json` 里带 `skill_drift`。+ `tests/test_skill_drift.py`（10 项）
- **`ic probe`：空转采样（只读）——触发数和产出必须并排看**（human 2026-10-15 定）。空转**只看一个数一定会被骗**：实测 pi 每分钟触发 36 次 headless，而从外面看它的"心跳空转轮"一动不动（4→4）；是"触发数 vs 产出"并排看才抓出来的，`outcome` 分布是同一件事的另一半（那 60 次里 57 次 fail ⇒ 模型/配置坏了，不是"它很忙"）。命令：`ic probe [--minutes 60] [--history N] [--no-log] [--watch 秒]`；每次采样往 `~/.infocenter/crew/probe.jsonl` **只增**一行（看趋势），`--history` 只看不采。**产出** = 信 + 案卷节点 + 语义痕迹；**拿钥匙 / 放钥匙 / 锁到期这类机械动作不算产出**（否则锁 churn 会被当成在出活）。**触发数**从守护运行记录里数（`crew.run_stats`），日志每家只留最近 60 条 ⇒ 它只是**下限**，会自己标 `truncated`。判语 `多半坏了` 优先于 `空转`（触发多、产出零）优先于 `出活太稀`；管理台每个身份的气泡上挂一对 `触发 N/产出 M`（`GET /v1/agents?probe_minutes=60`，不正常的标黄）。+ `tests/test_probe.py`（15 项）
- **案卷的「怎样算完」：一件事的收尾判据**（human 2026-10-15 定：任务一多，没人写清"做成什么算做成"，"做完没有"就靠事后吵）。案卷新增 `acceptance`（**一列**，≤400 字，`ic case <id> --acceptance "…"`；**owner 或 coordinator 能写**，封卷后不可改），立案（`POST /v1/cases`）也能带。它**不替代里程碑**：里程碑是路标，这一行是"做成什么算做成"；**空着不拦**——`ic cases` 与控制台标 **⚠ 没写怎样算完**。README 另立「收尾判据：什么样算做完」一节：四件事（全量绿 + 工作区干净 + 看板记一笔 + 无残留 TODO）核对着收尾；另记两条纪律：**减法轮**（每若干个新增任务来一轮"只删不加"）、**账别混**（想测机制就别插手）。+ `tests/test_case_acceptance.py`（13 项）
- **拿不到钥匙怎么办：记进自己的 TODO，照自己的轮**（human 定稿，**能不沟通就不沟通**）：「沟通效率太低，而且随 agent 数量递减；agent 多了，反正就是无需乱做」。
  - `ic queue <路径> --note "要改什么"` ⇒ 库里**只增**记一笔（谁提的、要改什么，可查）**＋自动写进提出者自己的 `TODO-<名字>.md`**（这才是"别忘了"；心跳提示词本来就是"读自己的 TODO → 推进下一件"）。`--patch-file` 可带补丁；`--no-todo` 只记库里。
  - **不排队、不通知、不点名、不按顺序**：轮到那条时 `ic lock`——**能拿到就做，拿不到就跳过做下一条**；那一笔留在台账里等下次。放钥匙只报"这个文件还有 N 条待改"，**不点下一位、不发信**（点名＝沟通，能免则免）。落地后 `ic queue <路径> --merge --id qN` 把自己那笔勾掉（只有拿钥匙的人／coordinator 能勾）。
  - 期间曾实现过"通知下一个 + 30 秒超时顶上"（确实跑通了），按 human 裁定**已拆掉**——收敛为共享状态上的静默协调。+ `tests/test_lock_queue.py`（15 项）与 `tests/test_todo_handoff.py`（8 项）
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

[Unreleased]: https://github.com/Robin9989Law/infocenter/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Robin9989Law/infocenter/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Robin9989Law/infocenter/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Robin9989Law/infocenter/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Robin9989Law/infocenter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Robin9989Law/infocenter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Robin9989Law/infocenter/releases/tag/v0.1.0
