# 最后一轮评审 · 交叉复核合并（coordinator）

> 日期：2026-10-14｜方法：**3 名独立评审（分区）+ 3 名对抗性交叉复核（逐条证伪 + 补漏）**
> 基线：全量 `python3 -W error::ResourceWarning -m unittest discover -s tests` = **82/82 OK 零告警**
> 原则：`CONFIRMED` 才进修复队列；`REFUTED/PARTIAL` 附反例与理由。全程未改生产文件、未动现网 8787。
> 状态：数据层 ✅（本节）；管理台/CLI 交叉复核在途，回填后出最终修复清单。

---

## 区域一 · 数据层（`store.py` / `httpapi.py`）

**评审 8 条 → 交叉复核 8/8 CONFIRMED**（#4 严重度收窄为中低；#3 按 no-auth 设计降为一致性缺陷非漏洞），并补 3 条漏网（M1/M2/M3）。

| # | 裁决 | 严重度 | 位置 | 问题 | 修法 |
|---|---|---|---|---|---|
| D-1 | CONFIRMED | 中 | `store.py:841-862`（`LIMIT`@851，过滤@856-862） | `inbox` 先 `LIMIT` 后按窗口/封卷/暂停过滤，被过滤行吃掉配额 → 本窗口未读被截断（复现：`count=0`，真值 5）；MCP `watch.unread` 取此 `count` | 过滤下推 SQL（子查询 `NOT EXISTS(claimed_by 他人)`）或先取全候选再切片；与 M1 一并修 |
| D-2 | CONFIRMED | 中 | `store.py:1266-1274` + `1925-1953` | `transfer_case` 只改写**目标 agent** 名下投递，原 owner 名下仍挂旧窗口 → 双归属；新信还会继承僵尸认领；`_lazy_reconcile_case` 也清不掉 | 转办须释放**源 owner** 名下投递认领（`claimed_by=NULL` 至少清源窗口）；补跨 agent 转办测试（现测试只覆盖同 agent） |
| D-3 | CONFIRMED（一致性，非漏洞） | 中 | `store.py:1218-1234` + `1956-2091`（`_is_party` 未调） | `claim_case` 回落 `_claim_thread_on_touch` 不校验当事人 → 非参与者可抢先立案抢走他人案子；`_effective_thread_claim` 跨 agent 也看不见既有认领，故不 409 | 立案前要求该 agent 是 sender 或某投递收件人（复用 `_is_party`），否则 404 |
| D-4 | PARTIAL（收窄） | 中低 | `store.py:2126-2131` | `left` 窗口复活为 `active` 时保留旧（死）pid；仅当**携带已死非空 pid**时后续对账再判 left 并误自动封卷；`pid=NULL`（CLI/events 主路径）复活正常 | 复活时 `pid` 置 NULL（除非本次显式传新 pid）；或改用 `last_seen_at` 陈旧度判活 |
| D-5 | CONFIRMED | 低-中 | `httpapi.py:320/324/442/401/417` → `store.py:80-84/1003/1018/1682/2120/2131` | 请求体字段未做 `isinstance(str)` 即进 `normalize_name()/.strip()/int()`，非字符串抛异常 → HTTP **500** 非 400（含 `state` 为 dict 时 `TypeError: unhashable`） | 入口统一类型校验（复刻 `_need_str`/`_workspace`），`state` 先 str 校验再进集合判断 |
| D-6 | CONFIRMED | 低 | `store.py:1007-1016` | `create_case` 仅 `to` 与 `body` **同时**给才发始信，只给一个静默忽略；同时给 `thread_id`+`to`+`body` 会静默覆盖调用方的 thread_id | 二者给其一即报 400，或明确文档化并回显 |
| D-7 | CONFIRMED | 低 | `store.py:1266-1273` / `1462-1473`；另 `1195-1202`、`1402-1410` | 离开 `waiting` 的四条直接改 status 的 SQL 未清 `waiting_for` → 残留「待谁」被复用（`append_case_node`/`send` 已正确清） | 这四条 UPDATE 补 `waiting_for=NULL` |
| D-8 | CONFIRMED | 低 | `httpapi.py:483-494` + `store.py:2132-2150` | `window_id` 与 agent 无绑定，任意请求可凭 `X-Window` 冒用他人窗口号改写 `windows.agent`（`create_case`/`claim_case`/`transfer_case` 亦为重绑定入口） | 首次绑定后拒绝异 agent 改写，或把 `agent` 从 `_upsert_window` 的 UPDATE 拆出 |
| M1 | 漏网 | 中低 | `store.py:2428-2432`（经 `touch`348 / `list_agents`361） | `agents[].unread` 用 `_unread_counts` 完全无视他人认领与封卷 → 角标虚高（与 D-1 同语义不同出口） | 与 D-1 同修：加窗口 + 封卷口径 |
| M2 | 漏网 | 中低 | `store.py:1743` `_is_window_gone` | 无 pid 的窗口崩溃后**永不**判死 → §4.4「惰性补齐」只覆盖有 pid 的窗口（CLI/events 建的窗口 `pid=NULL`） | `events/mark` 等主路径上报/刷新 pid，或为僵尸无 pid 窗口补其它判据 |
| M3 | 漏网 | 提示 | `store.py:1108/1236/1291-1427` + `httpapi.py:178-179` | 案卷写操作（追加节点/暂停/中止/办结/转办）不校验当事人/主办；D 路由缺 `X-Agent` 回落 `human` | 按 no-auth 设计不计为漏洞；如需收紧，与 D-3 一并定调 |

**误报：无**（8 条逐条重跑无一被推翻）。

---

## 区域二 · 管理台 / 服务端 / 联邦 / MCP

**评审 6 条 → 交叉复核 6/6 CONFIRMED**（以 **jsdom 真浏览器引擎**复现 XSS），补 3 条漏网（L1/L2/L3）。

| # | 裁决 | 严重度 | 位置 | 问题 | 修法 |
|---|---|---|---|---|---|
| U-1 | CONFIRMED（jsdom 实测执行） | **高** | `web/index.html:196/201/209-212` + `httpapi.py:324` + `store.py:1018/1047` | **存储型 XSS**：`case.id`(=`thread_id`，零校验) 经 `escapeHtml` 置入 `onclick="selectCase('${cid}')"`；**属性上下文会实体解码**，`&#39;`→`'` 逃逸 JS 串（jsdom：`window.__xss="pwned"`）；`POST /v1/cases` 无 X-Agent 可用、不校验 Origin/CT | ① 服务端校验 `thread_id`（ID_RE/白名单）；② 前端废 `onclick` 串，改 `dataset`+`addEventListener`。**仅加强 escapeHtml 无效** |
| U-2 | CONFIRMED | 低 | `server.py:59-66` | `Content-Length` 无下界：`int("-1")` 过检查，`read(-1)` 读到 EOF，挂死一个线程 | `if length < 0 or length > MAX_BODY: 400/413` |
| U-3 | CONFIRMED（规格偏离） | 低 | `store.py:748-760` + `sync.py:568/603-617` | 导入幂等只比本机裸名收件人，忽略信封 `to` 他机部分（与 `federation-design.md:144` 不符）；现表结构无 `to` 列，完整比较不可实现 | 二选一：用 `envelope["to"]` 补比较，或改注释/降级规格 |
| U-4 | CONFIRMED（实测崩溃） | 低 | `mcp_server.py:20-24/38-46` + `cli.py:147/270` | 非法 `INFOCENTER_WINDOW` 未规范校验、注册 PUT 在 `try` 外，`ApiError` 逃逸 → MCP 进程 traceback exit 1（合法值 exit 0） | 注册纳入 try + 转 `MailError`；`resolve_mcp_window` 规范校验 |
| U-5 | CONFIRMED（jsdom 实测） | 低 | `web/index.html:223-249` | `selectCase` 无请求序号/abort，快速点两卷 → 高亮与详情错配 | 请求令牌比对或 `AbortController` |
| U-6 | CONFIRMED | 低 | `mcp_server.py:344-354` | `ic_read`/`ic_archive` 无条件自减 `watch.unread`，对已读信低估（≤25s 内自愈） | 用服务端未读数校准 |
| L1 | 漏网 | 中 | `httpapi.py:169-182` + `server.py`（全仓无 Origin/CT 校验） | 状态变更端点**全线无 CSRF 防护** + D 路由无 X-Agent 回落 `human`：简单 `text/plain` 表单 POST 可跨站建卷/中止/办结/`leave`（实测 201/200） | 校验 `Origin`/`Sec-Fetch-Site` 或强制 `application/json`；收紧 D 路由 `human` 默认 |
| L2 | 漏网 | 低 | `store.py:1018/1047` + `httpapi.py:324` | case id 无长度/字符集约束（可到 ~1MB、含换行），U-1 上游成因 | 加 ID 白名单/长度限制 |
| L3 | 漏网 | 低 | `web/index.html:253/258/263/268` | 动作按钮 URL 未 `encodeURIComponent(id)`（与 `selectCase` 不一致） | 统一编码 |

误报：无。分歧：U-3 定级归因为「规格偏离」而非完整性漏洞。

---

## 区域三 · CLI / 守护 / 客户端 / config

**评审 16 条 → 交叉复核：16 条无一被整体推翻**（#1 降为中、#2 降为低-中、#11 半条修正 PARTIAL），补 5 条漏网（M1–M5）。

| # | 裁决 | 严重度 | 位置 | 问题 | 修法 |
|---|---|---|---|---|---|
| C-1 | CONFIRMED（触发需 env 有值） | 中 | `crew.py:274-277` → `816-824` | `_start_daemon` 不 pop `INFOCENTER_WINDOW`，`_adopt_window` 复用 env → 多守护/项目共用窗口；`leave_window` 按 `owner_window` 中止**全部**未封卷案卷 | `_start_daemon` 里 `env.pop("INFOCENTER_WINDOW", None)`；`_adopt_window` 只认显式 `--window` |
| C-2 | CONFIRMED（需脏 pid 文件） | 低-中 | `crew.py:84-97/398-403` | pid 无范围校验，`0` 被判活 → `os.kill(0,SIGTERM)` 打整个进程组（`_signal_group` 有 `pid<=1` 守卫，`down` 没有） | `_pid_alive/_read_pid/down` 加 `pid>1` |
| C-3 | CONFIRMED | **高** | `crew.py:768-789`（`text=True` strict）+ `729-744`（无捕获） | headless 输出非 UTF-8 → `UnicodeDecodeError` 穿透 watch → 守护线程退出且 `finally _leave_office` **自动封卷该窗口全部案卷** | `Popen(..., errors="replace")`；`watch` 里包 `try/except` 记日志续跑 |
| C-4 | CONFIRMED | 中 | `crew.py:283/84-89` | 守护从不删 pid 文件，判活仅 `os.kill(pid,0)` → pid 复用后 `up` 误判「已在运行」（静默失去收信）、`down` 打到无关进程 | pid 文件记 `started_at`/命令行校验，或正常退出 unlink |
| C-5 | CONFIRMED | 中 | `client.py:99-108` | 只捕 `HTTPError`/`URLError`；服务端读超时抛裸 `TimeoutError`/`OSError`（`RemoteDisconnected`）→ 所有 CLI 命令裸 traceback、退出码 1 | 统一把 `TimeoutError/OSError` 包成 `ApiError` |
| C-6 | CONFIRMED | 中低 | `crew.py:727-732`+`790-806` | 触发信在跑 headless **前**写 cursor，超时/失败不回退 → 这批信守护永久跳过（现网日志 ~10 次 30 分钟超时） | 超时/失败回退 cursor 到本轮起点，并补 `[自动] 中断` 节点 |
| C-7 | CONFIRMED（现网正在发生） | 中低 | `crew.py:452-459` | `_interrupt_headless` 排在两个只读 API（GET `/v1/windows`、`/v1/cases`）之后，任一失败即 raise → 「暂停要快」失效（现网旧服务这两条 404，pause 第一句就失败） | 先打断（或 `try/finally`）再做状态流转 |
| C-8 | CONFIRMED（现网已发生） | 中低 | `crew.py:834-851`+`563-571` | 登记办公室失败被吞后守护照跑但办公室无窗口 → `_window_paused` 恒 False → pause 只打断一轮，之后照起 headless | 登记失败 N 次后退出守护，或「登记成功才进收信循环」，至少显眼告警 + `crew status` 标红 |
| C-9 | CONFIRMED | 低 | `crew.py:206/272-287` | `up` 判活→Popen→写 pid 无锁且有 TOCTOU → 并发 `crew up` 起两个守护 | pid 文件 `O_EXCL` 占位后再 Popen |
| C-10 | CONFIRMED | 低 | `crew.py:494-508` | `_signal_group` 的 `ProcessLookupError` 先 `return False`，`kill` 回落成死代码（非组长 pid 杀不掉） | `ProcessLookupError` 分支也尝试 `os.kill` |
| C-11 | PARTIAL | 低 | `crew.py:511-539/804` | 成立：「只发一次 SIGTERM、无 SIGKILL 升级、不校验真死」；**反例**：`headless_pid` 由 daemon 的 `finally _note_headless_pid(None)` 清理，非「不清」 | 补 SIGTERM→短轮询→SIGKILL 升级 |
| C-12 | CONFIRMED | 低 | `crew.py:450/125-134` | `_select(home,"")` 返回全部 → `ic crew pause ""` 作用到第一个 agent | 入口校验非空 |
| C-13 | CONFIRMED | 低 | `crew.py:273/342-343/391` | `key.split("--",1)[0]` 反解身份，名字含 `--` 时身份错 | 改 `rsplit("--",1)[0]` 或取 `meta["agent"]` |
| C-14 | CONFIRMED | 低 | `config.py:70-73/98-102/163-183` | `agents add/remove` 走 `load()` 归一化后整表回写，静默丢条目未知字段；RMW 无锁 | 按 name 做最小补丁而非整表覆盖 |
| C-15 | CONFIRMED（潜在，现网安全） | 低-中 | `crew.py:761-766` | 模板把 `{prompt}` 包进引号时 `shlex.quote` 失效 → 发信人可控 subject 触发 RCE（现网模板全裸，**当前不可利用**） | `shlex.split` 成 argv 执行，不经 `/bin/sh -c` |
| C-16 | CONFIRMED | 低 | `cli.py:277-307` | `_peel_globals` 任意位置剥离全局参数，值恰为 `--json/--as/--url/--home` 时传不进去 | 只在子命令前剥离 |
| M1 | 漏网（coordinator 已复核） | 中-高 | `store.py:440/453/493/494` | `events` cursor 取全表 `MAX(rowid)` 但只回最新 `limit` 条 → 一次 >limit 新信时，最旧的若干条**永久丢**（现网无此日志但逻辑成立） | 取最旧 `limit` 条（`[:limit]`）且 `cursor` = 实际返回的最大 seq |
| M2 | 漏网 | 中低 | `crew.py:304/311` | `crew.kickoff` 直用 `request` 不捕获，`--plan` 文件缺/serve 4xx → 裸 traceback | 捕获并转 `MailError` |
| M3 | 漏网 | 中低 | `store.py:2401` | `_workspace_scope` basename 回退（casefold 相等即命中）→ 同名目录跨项目串信 | 多项目并行下收紧匹配 |
| M4 | 漏网 | 低-中 | `crew.py:458-488` | `abort`/`close` 不打断正在跑的 headless | 同 pause 一样打断 |
| M5 | 漏网 | 低 | `client.py` | 同 C-5，扩到 `RemoteDisconnected`/其他 `OSError` | 并入 C-5 |

---

## 修复计划（按优先级）

**P0（本轮必修）**：U-1 XSS｜C-3 非 UTF-8 自杀+误封卷｜C-7 暂停排序｜C-6 + M1 信件丢失｜C-1 窗口串号｜C-5/M5 裸 traceback
**P1（应修）**：D-1 inbox 截断 + M1-data 未读口径｜D-2 转办双归属｜D-3 非当事人立案｜D-4 死 pid 复活｜D-5 500→400｜C-4 pid 复用｜C-8 登记失败降级｜L1 CSRF｜U-2 长度下界｜U-4 MCP 崩溃
**P2（可修/记录）**：D-6/D-7/D-8、C-2/C-9~C-16、M2/M3/M4、U-3、U-5、U-6、L2/L3

**执行方式**：因现网服务为旧码且「暂不启动」，改为**由 coordinator 直接改**（不依赖守护派单），按文件分区避免并发写，改完补回归测试并复跑全量。
