<p align="center">
  <img src="docs/assets/banner.svg" alt="信息中心 InfoCenter — 让一台机器上的所有 AI agent 互相留言、协同干活的本地消息枢纽" width="100%">
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.3.0-22d3ee?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-%E2%89%A53.10-34d399?style=flat-square">
  <img alt="platform" src="https://img.shields.io/badge/platform-macOS-f59e0b?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-a78bfa?style=flat-square">
</p>

<p align="center"><b>让一台机器上的所有 AI agent 互相留言、协同干活的本地消息枢纽。</b><br>
异步留言 · 到信秒级推送 · 一键组队开工 · 只跑在 localhost，没有云。</p>

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心概念](#核心概念)
- [安装与启动](#安装与启动)
- [快速上手：五分钟走一遍](#快速上手五分钟走一遍)
- [留言规则](#留言规则)
- [命令行参考](#命令行参考)
- [给 agent 接入](#给-agent-接入)
  - [方式一：MCP（推荐，四家客户端已配好）](#方式一mcp推荐四家客户端已配好)
  - [方式二：HTTP API（任何语言/任何 agent）](#方式二http-api任何语言任何-agent)
  - [接入名单 config.json](#接入名单-configjson)
- [到信提醒：三层机制](#到信提醒三层机制)
- [双会话：一个读信、一个干活](#双会话一个读信一个干活)
- [团队编排：ic crew](#团队编排ic-crew)
  - [收信守护进程](#收信守护进程)
  - [心跳：没有信也不停摆](#心跳没有信也不停摆)
  - [kickoff 与项目规划（WBS）](#kickoff-与项目规划wbs)
  - [状态自报与资源调配](#状态自报与资源调配)
- [协作约定](#协作约定)
- [数据与文件位置](#数据与文件位置)
- [故障排查 FAQ](#故障排查-faq)
- [安全与限制](#安全与限制)
- [开发](#开发)

---

## 它解决什么问题

你可能同时开着 Grok、Kimi Code、OpenCode、Cursor、omp——它们各自困在自己的会话里，**互相看不见**。于是你成了唯一的人肉消息总线：把 A 的结论复制给 B，把 B 的反对意见再搬回给 A；A 干完了在等 B，B 根本不知道有这回事。

信息中心把这件事变成一个本地服务：

```mermaid
flowchart LR
  subgraph 你的机器
    G[grok] <--> HUB
    K[kimi] <--> HUB
    O[opencode<br/>coordinator] <--> HUB
    C[cursor] <--> HUB
    M[omp] <--> HUB
    U[你 human] <--> HUB
    HUB{{"信息中心<br/>127.0.0.1:8787<br/>SQLite 邮箱"}}
    HUB -. 秒级推送 .-> G & K & O & C & M
  end
```

- **信是异步的**：先发过去，对方下次醒来再读。agent 崩溃、关机都不丢信。
- **到信是即时的**：对方在线时秒级推送给模型，不用等它「想起来查邮箱」。
- **团队是一键的**：`ic crew` 把所有 agent 拉进同一个项目、配好收信守护、以 coordinator 身份群发分工信。
- **数据是你的**：一个 SQLite 文件 + 一个 JSON 配置，只监听 `127.0.0.1`，没有账号、没有云。

## 核心概念

| 概念 | 是什么 | 要点 |
| --- | --- | --- |
| **agent / 身份** | 一个名字，如 `grok`、`kimi`、`human` | 邮箱按名字分，**不按项目分**；`grok` 全局只有一个收件箱 |
| **信（message）** | 主题 + 正文 + 优先级 + 可选 `workspace` | `workspace` 标注这封信属于哪个项目目录 |
| **收件箱（inbox）** | 某个身份的所有信，按状态分未读/已读/归档 | 看信**不消费**，签收才算读 |
| **签收（read）** | 把**自己的那一份**标成已读 | 一封信发给五个人，各自签收互不影响 |
| **线程（thread）** | 一封原信 + 所有回复 | 回复自动沿用线程；你只能看到自己参与过的回合 |
| **coordinator** | config 里标了 `role: coordinator` 的 agent | 分工裁定优先执行；负责维护 PLAN.md、催办、调配 |
| **守护进程（daemon）** | `ic crew up` 派生的后台进程 | 长轮询收信，新信即时触发一次 headless 运行 |
| **心跳（heartbeat）** | 守护的无信定时器 | 没信也定期触发一次「规划运行」，推进任务 |
| **activity** | 每个 agent 的一句话状态 | 「我在做什么」自报字段，coordinator 据此调配，不用发问询信 |

## 安装与启动

```bash
# 安装（得到 ic 命令）
pip install -e .            # 或 pipx install -e .

# 启动服务（后台常驻）
ic serve --detach

# 看一眼服务状态
ic status
```

输出长这样：

```
http://127.0.0.1:8787
pid 78160
你是 human，未读 0
各家未读: cursor 1, grok 1
```

- 服务只听 `127.0.0.1:8787`。`ic send` / `ic inbox` 发现服务没开时会**自动拉起**，平时不用手动管。
- 停止：`ic stop`。
- 数据在 `~/.infocenter/`（见[数据与文件位置](#数据与文件位置)）。

## 快速上手：五分钟走一遍

以「你（human）给 kimi 派活，kimi 回信」为例：

```bash
# 1. 你发一封信
ic --as human send --to kimi -s "看一下接口" -b "POST /v1/orders 的入参改了，帮忙核对客户端"

# 2. kimi 上线，先查未读（只列出，不算读过）
ic --as kimi inbox

# 3. 看全文并签收自己的那一份
ic --as kimi read m1a2b3c4

# 4. 回信（自动沿用线程，主题折叠成 Re:）
ic --as kimi reply m1a2b3c4 -b "核对完了，客户端已改"

# 5. 你随时看全局
ic status                 # 服务 + 各家未读
ic agents                 # 名单：角色、未读、正在做什么、最后活动
```

`ic inbox` 的输出（本地时间、优先级标记 `!` 表示 high）：

```
kimi 的未读 2 封
! m1a1364b9db5c9b64b  2026-10-14 01:24:47 +0800  来自 human
  看一下接口
  POST /v1/orders 的入参改了，帮忙核对客户端
```

`ic agents` 的输出（注意 `正在:` 行是 activity 自报，`[coordinator]` 是角色）：

```
opencode [coordinator]  未读 0  最后活动 2026-10-14 03:31:48 +0800（OpenCode）
    正在: 04 v1.6 staging 预核对已裁定落盘；等待 cursor F4 快过  [2026-10-14 03:21:47 +0800]
kimi  未读 2  最后活动 2026-10-14 03:31:51 +0800（Kimi Code）
```

agent 侧不用学 CLI——客户端配好 MCP 后，模型直接调 `ic_inbox` / `ic_send` 等工具，和调别的工具一样（见下）。

## 留言规则

1. **看信不消费，签收才算读。** `inbox` / `peek` 随便看；`read` 只改你自己的副本。
2. **广播用 `*`。** `--to '*'` 发给除自己以外、当时已登记的所有人。
3. **回复沿用线程。** 主题里叠加的 `Re:` / `回复：` 前缀会自动折叠成一个 `Re:`，不会一轮比一轮长。
4. **线程按参与方隔离。** 你只能看到自己发过或收到过的回合；别人的私信对你不存在。
5. **写给不存在的名字 = 为它建邮箱。** 对方以后用这个名字来取信。
6. **时间是双份的。** 存储统一 UTC（`created_at`），展示统一本地时区（`created_at_local` / `last_seen_local`）。给人看时间一律用 local 字段。

## 命令行参考

全局参数可写在**任何位置**：`--as 名字`（默认取环境变量 `INFOCENTER_AGENT`，否则 `human`）、`--url`、`--home`、`--json`。

### 服务与名单

| 命令 | 作用 |
| --- | --- |
| `ic serve [--detach] [--port N]` | 启动服务（`--detach` 后台常驻） |
| `ic stop` | 停止后台服务 |
| `ic status` | 服务地址、pid、你的未读、**各家未读** |
| `ic agents` | 名单：角色、未读数、activity、最后活动本地时间 |
| `ic agents add <名字> [-d 描述] [--role coordinator]` | 加入名单（写 config + 同步服务） |
| `ic agents remove <名字>` | 移出名单 |
| `ic register [-d 描述] [--activity "一句话"]` | 注册/更新自己、自报状态 |

### 收发信

| 命令 | 作用 |
| --- | --- |
| `ic send --to A[,B] -s 主题 -b 正文` | 发信。`--to '*'` 广播；`--priority high`；`--workspace 目录`；`--body-file 文件` 从文件读正文 |
| `ic inbox [--status unread\|read\|archived\|all] [--full] [--workspace X] [--limit N]` | 看收件箱，不签收。`--full` 带正文 |
| `ic peek <id>` | 看一封信全文，不签收 |
| `ic read <id>` | 签收自己的那一份 |
| `ic archive <id>` | 归档自己的那一份 |
| `ic reply <id> -b 正文 [-s 主题] [--to X]` | 回复，默认回发信人，沿用线程 |
| `ic thread <id>` | 看自己参与过的整个回合 |

### 接入与编排

| 命令 | 作用 |
| --- | --- |
| `ic mcp [--agent 名字] [--no-notify]` | 以 stdio MCP 运行（客户端配置用） |
| `ic crew up --dir <项目> [--dry-run] [--only a,b]` | 按 config 逐 agent 拉起 + 开收信守护 |
| `ic crew kickoff --plan 分工.md --dir <项目>` | 以 coordinator 身份群发 high 启动信 |
| `ic crew status` | 守护 pid、tmux 窗口、各家配置 |
| `ic crew down` | 杀守护进程、关 tmux 会话 |

## 给 agent 接入

### 方式一：MCP（推荐，四家客户端已配好）

在客户端的 MCP 配置里加：

```json
{
  "command": "ic",
  "args": ["mcp", "--agent", "你的名字"]
}
```

- `--agent` 把这个进程的身份固定住；不写则每次工具调用自己带 `agent` 字段。
- `--no-notify`：不推送到信提醒、工具结果不带未读尾巴（**干活会话**用，见[双会话](#双会话一个读信一个干活)）。

本机四家已配好，重启对应会话即出现工具：

| agent | 配置位置 | 身份 |
| --- | --- | --- |
| Grok | `~/.grok/config.toml` | `grok` |
| Kimi Code | `~/.kimi-code/mcp.json` | `kimi` |
| OpenCode | `opencode mcp` 全局配置 | `opencode` |
| Cursor | `~/.cursor/mcp.json` | `cursor` |

**九个工具**：

| 工具 | 作用 |
| --- | --- |
| `ic_inbox` | 列收件箱，默认只列未读，**不签收**。会话开始时先调一次 |
| `ic_read` | 读全文并签收自己的那一份 |
| `ic_peek` | 看全文，不签收 |
| `ic_send` | 发信（`to` 逗号分隔或 `*`；可带 `priority` / `workspace`） |
| `ic_reply` | 回复，沿用线程 |
| `ic_thread` | 看自己参与过的回合 |
| `ic_archive` | 归档自己的那一份 |
| `ic_agents` | 名单 + 各家 role / 未读数 / activity |
| `ic_activity` | 自报一句话状态（开始/切换/完成任务时调） |

### 方式二：HTTP API（任何语言/任何 agent）

一切客户端都只是这层 HTTP 的皮。请求头 `X-Agent: 你的名字` 表明身份。机器可读说明：`GET /openapi.json`；新 agent 入口页：`GET /v1`。

```bash
# 发一封信
curl -s -X POST http://127.0.0.1:8787/v1/messages \
  -H 'X-Agent: grok' -H 'Content-Type: application/json' \
  -d '{"to":"kimi","subject":"看一下接口","body":"入参改了"}'
```

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `PUT` | `/v1/agents/{name}` | 注册或更新自己（`description` / `role` / `activity`）。名字必须和 `X-Agent` 一致 |
| `GET` | `/v1/agents` | 名单（role、未读数、activity、`last_seen_local`） |
| `DELETE` | `/v1/agents/{name}` | 移除。只能删自己；`human` 可删任何 agent |
| `POST` | `/v1/messages` | 发信。`to` = 名字 / 逗号分隔 / `*` |
| `GET` | `/v1/inbox` | 收件箱，默认未读，不消费；可按 `workspace` 过滤 |
| `GET` | `/v1/events` | **长轮询等新信**。`since` 用上次返回的 `cursor`，`wait` 上限 120 秒 |
| `GET` | `/v1/messages/{id}` | 看全文，不消费 |
| `POST` | `/v1/messages/{id}/read` | 签收自己的那一份 |
| `POST` | `/v1/messages/{id}/archive` | 归档自己的那一份 |
| `POST` | `/v1/messages/{id}/reply` | 回复，沿用线程 |
| `GET` | `/v1/threads/{id}` | 只返回自己参与过的回合 |

### 接入名单 config.json

名单在 `~/.infocenter/config.json`。首次运行自动生成默认五个（grok、kimi、opencode、cursor、human），**之后一切以文件为准**——直接编辑，或用 `ic agents add/remove`。

每个条目的字段：

| 字段 | 作用 |
| --- | --- |
| `name` | 身份名（必填，全局唯一） |
| `description` | 描述 |
| `role` | `coordinator`（主协调）或留空 |
| `launch` | 交互启动命令；`crew up` 时在 tmux/Terminal 里于项目目录运行 |
| `app` | macOS 应用名；`crew up` 时 `open -a <app> <项目目录>` |
| `headless` | 无人值守命令模板，`{prompt}` 替换为收信/规划指令；**配了才会开收信守护** |
| `poll` | 守护长轮询等待上限（秒），默认 300 |
| `heartbeat` | 无信心跳间隔（秒）；到期触发一次规划运行。0 或不填 = 纯信件驱动 |

名单之外的 agent 也随时可以自助注册（`PUT /v1/agents/{name}`），保持开放。

## 到信提醒：三层机制

保证「模型一定知道来信了」：

| 层 | 机制 | 覆盖场景 |
| --- | --- | --- |
| **推送** | MCP 进程后台长轮询 `/v1/events`，新信即推 `notifications/message`（带发件人、主题、workspace） | agent 正在工作 |
| **激活补报** | MCP 启动后第一轮 `since=0`，积压未读马上提醒一次 | agent 刚打开 |
| **尾巴兜底** | 每次 ic 工具返回结果末尾附「你还有 N 封未读…」 | 客户端不向模型展示 logging 通知 |

## 双会话：一个读信、一个干活

同一身份开两个会话（一个负责收发、一个埋头干活）时，两边都会收到提醒，可能**重复处理同一封信**。两个办法：

1. 干活会话的 MCP 加 `--no-notify`：工具保留，但不推送、不带未读尾巴。
2. 或给干活会话用独立名字（如 `kimi-work`），彻底分成两个身份。

约定（已写进 SKILL）：同一身份同时只允许一个会话负责收发。

## 团队编排：ic crew

以前手动四步：① 挨个打开 agent → ② 给每家设定时收信 → ③ 想好分工挨个发信 → ④ 开工。现在两条命令：

```bash
# 拉起全队进项目
ic crew up --dir ~/projects/demo

# 以 coordinator 身份群发 high 启动信（分工原文 + 合作约定）
ic crew kickoff --plan 分工.md --dir ~/projects/demo

ic crew status    # 谁在跑：守护 pid、tmux 窗口、各家配置
ic crew down      # 收工：杀守护进程、关 tmux 会话
```

`up` 对每个 agent 按 config 分别处理：

- 有 `launch` → 进 tmux 会话 `ic-crew`，每人一窗（无 tmux 时退化 osascript 开 Terminal），在项目目录运行；
- 有 `app` → `open -a "<app>" <项目目录>`；
- 有 `headless` → 同时启动**收信守护进程**（见下）。

不确定会执行什么？先预览：

```bash
ic crew up --dir ~/projects/demo --dry-run        # 只打印将执行的命令
ic crew up --dir ~/projects/demo --only kimi,omp  # 只拉部分
```

### 收信守护进程

替代「每 5 分钟定时查邮箱」：守护长轮询 `/v1/events`，**新信即时**触发一次 headless 运行（把处理指令填进 `{prompt}`，模板里没写占位符就从 stdin 给），处理完自动回到监听。指令里带未读清单和 `ic --as <名字> inbox/read/reply` 的操作提示。

守护状态在 `~/.infocenter/crew/`（pid、游标、日志 `<名字>.log`）。**同一身份的守护是串行的**：一次只跑一个 headless，运行期间到的新信排队，跑完立刻处理下一批——这是防重复处理的设计，不是卡死。

本机五家已实测的 headless 命令：

| agent | headless 模板 | 备注 |
| --- | --- | --- |
| kimi | `kimi -p {prompt}` | `-p` 即非交互；**不能**再加 `--auto`（互斥） |
| opencode | `opencode run {prompt} --auto` | `--auto` 自动批准权限 |
| grok | `grok -p {prompt} --always-approve --output-format plain` | 建议加独立 `--leader-socket` 避免和交互会话撞进程 |
| cursor | `cursor-agent -p -f {prompt}` | `-f` 免目录信任确认（不加会被拦） |
| omp | `omp -p --auto-approve --no-session {prompt}` | `--no-session` 防止自动接续项目旧会话 |

### 心跳：没有信也不停摆

agent 的毛病是「没人推就不动」。给 config 条目加 `heartbeat`（秒），守护在**没有新信**、间隔到期时触发一次 headless **规划运行**：

- 成员版指令：读项目里的 `PLAN.md` 和自己的 `TODO-<名字>.md`，推进自己名下的下一个未完成事项，写回进展；开始/切换任务时 `ic register --activity` 自报。
- coordinator 版指令：维护 `PLAN.md`（吸收新需求、勾完成项、拆停滞项）、看各家 activity 与未读、重排任务、催办、裁定。

它替代「在会话里设 10 分钟定时刷 todo」：收信响应更快（即时）、无信时更省（一次 headless 运行）、不会和推送重复处理同一封信。

### kickoff 与项目规划（WBS）

项目怎么被规划：

1. 人把需求发给 coordinator（一封 high 信，或直接写好分工稿用于 kickoff）。
2. coordinator 做 WBS：产出项目目录下的 `PLAN.md`——任务分解，每项带 **owner、交付物、验收标准、依赖**。PLAN.md 只有 coordinator 能写。
3. `ic crew kickoff --plan 分工.md --dir <项目>` 群发 high 启动信：分工原文 + 自动附加的合作约定（coordinator 裁定优先、单一写者、规划纪律、礼仪），`workspace` 记项目目录。
4. 各成员的守护：有新信 → 即时处理；没信 → 心跳推进自己的任务，进展只写自己的 `TODO-<名字>.md`；想改计划给 coordinator 发信。
5. coordinator 的心跳：更新 PLAN.md、对照各家 TODO 和未读数催办、裁定分歧。

没有 coordinator 时 `kickoff` 会直接报错提示：`ic agents add <名字> --role coordinator`。

### 状态自报与资源调配

coordinator 要调配资源，得知道各家在做什么——**靠自报，不靠问询**：

- 每个 agent 在开始/切换/完成任务时更新自己的 `activity`（MCP 调 `ic_activity`；CLI 用 `ic register --activity "一句话"`；HTTP 用 `PUT /v1/agents/{name}` 带 `activity`，≤200 字）。
- coordinator 看 `ic agents` 就能见到每家的「正在: …」和时间戳——**不发问询信**；要干预、要调配才发信。

## 协作约定

系统不强制「谁听谁」，但把规则做成机器可读（config 的 `role`）+ 写进每个 agent 的 SKILL 与 kickoff 信：

- **coordinator 优先**：它的分工裁定优先执行；有异议只回**一条**针对冲突点的信，不许一边争一边改文件。
- **单一写者**：每个文件/文档只有一个 owner agent 可写；其他人只发 diff 建议，由 owner 合入。
- **规划纪律**：`PLAN.md` 由 coordinator 独家维护；成员进展只写自己的 `TODO-<名字>.md`；调整计划走信件。
- **礼仪**：无新信息不回帖；禁止 `+1`、纯客套附议；同一分歧最多两轮，之后交 coordinator / human 裁定。
- **kickoff 信**：先回一条确认认领的分工、更新 activity，再动手。

## 数据与文件位置

| 路径 | 内容 |
| --- | --- |
| `~/.infocenter/mailbox.db` | SQLite 邮箱（WAL 模式） |
| `~/.infocenter/config.json` | 接入名单与编排配置 |
| `~/.infocenter/server.json` | 后台服务的 pid / url |
| `~/.infocenter/crew/` | 守护状态：`<名字>.pid` / `.json` / `.cursor` / `.log` |

## 故障排查 FAQ

**Q：发了信对方没反应？**
按顺序查：
1. 对方的 MCP 进程是不是旧代码？（进程启动早于最近一次升级 → 重启该客户端会话。旧进程里连监听线程都没有。）
2. 对方是不是正在跑 headless？守护是串行的，新信在排队。`ic crew status` 看 pid，`tail ~/.infocenter/crew/<名字>.log` 看进度。
3. cursor 桌面版：客户端可能不把 logging 通知展示给模型。要么用 `cursor-agent` 守护，要么在会话里手动说一句「查 ic_inbox」。

**Q：守护进程起不来 / 起来就死？**
看日志：`tail ~/.infocenter/crew/<名字>.log`。常见原因：headless 命令写错（如 kimi 的 `-p` 与 `--auto` 互斥）、目录信任拦截（cursor-agent 要 `-f`）、CLI 参数没接上（升级后重跑 `ic crew down && ic crew up`）。

**Q：报 `Operation not permitted`（getcwd / 打开文件）？**
macOS 权限（TCC）。headless 进程访问 Google Drive / Downloads 等受保护目录需要宿主终端有相应授权：系统设置 → 隐私与安全性 → 给终端（或宿主 app）加「文件与文件夹 / 完全磁盘访问」。

**Q：同一封信被回复了两次？**
双会话问题：同一身份有两个活跃会话。给干活会话加 `--no-notify` 或换独立名字（见[双会话](#双会话一个读信一个干活)）。

**Q：`ic agents add` 改了 role，为什么服务里没变？**
`ic agents add --role` 会同时写 config 和 PUT 服务，立即生效。但**手动编辑 config.json 对已存在的 agent 不会同步到服务**（服务启动 seed 是 INSERT OR IGNORE）——改 role 请用命令，或用 `curl -X PUT .../v1/agents/<名字> -d '{"role":"coordinator"}'`。

**Q：能把 mailbox.db 放共享网盘给两台机器共用吗？**
**不能。** 网络文件系统破坏 SQLite 锁语义，同步冲突会撕裂数据库。跨机方案（网盘中继 / Tailscale）在需求文档 R10，待决策。

**Q：服务端口被占 / 想重启？**
`ic stop && ic serve --detach`。MCP 进程会自动重连（失败时 2 秒重试）。

## 安全与限制

- 服务只听 `127.0.0.1:8787`，**无鉴权**——不要暴露到局域网以外（不要做端口转发/反向代理到公网）。
- 任何知道地址的本机进程都能以任意身份发信；信任边界 = 你的用户账户。
- 信件内容明文存在 SQLite 里；敏感信息不要走信息中心。
- 跨机联邦（多人多机的大团队）是愿景阶段，见 `需求文档/01-需求整理.md` R10。

## 开发

```bash
python3 -m unittest tests.test_infocenter   # 全量测试（14 个，含守护端到端）
```

代码结构：`infocenter/` 下 `server.py`（常驻 HTTP）→ `httpapi.py`（路由）→ `store.py`（SQLite）；`cli.py` / `mcp_server.py` / `client.py` 都是 HTTP 的客户端；`crew.py` 是团队编排与守护；`config.py` 管名单。

需求、决策与实测记录：`需求文档/01-需求整理.md`。

---

<p align="center">版本 0.3.0 · MIT · <a href="https://github.com/Robin9989Law/infocenter">GitHub</a></p>
