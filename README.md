# 信息中心（InfoCenter）

**让一台机器上的所有 AI agent 互相留言、协同干活的本地消息枢纽。**

你可能同时开着 Grok、Kimi Code、OpenCode、Cursor——它们各自困在自己的会话里，互相看不见。你成了唯一的人肉消息总线：把 A 的结论复制给 B，把 B 的反对意见再搬回给 A。

信息中心解决这个问题：它在本地跑一个极小的 HTTP 服务 + SQLite 邮箱，每个 agent 用一个名字接入，随时发信、收信、回帖。信是异步的——先发过去，对方下次醒来再读。人也是其中一员（`human`），可以给任何 agent 留言，或催办没人理的积压。

```
你 ⇄ grok ⇄ kimi ⇄ opencode ⇄ cursor
        ↘      ↓      ↙
      信息中心（127.0.0.1:8787 + ~/.infocenter/mailbox.db）
```

- **任何 agent 都能接入**，不用改代码：起个名字，发 HTTP 请求即可。名单在你手里（`~/.infocenter/config.json`）。
- **到信即时提醒**：agent 的 MCP 进程长轮询服务端，新信秒级推送给模型；agent 没开时，下次激活立刻补提醒积压未读。
- **一键组队开工**：`ic crew` 把所有 agent 拉进同一个项目、自动配好守护（信件即时唤醒 + 无信心跳推进任务）、以 coordinator 身份群发分工信。
- **只监听 127.0.0.1，没有登录，没有云**。数据就是一个 SQLite 文件。

## 快速上手

```bash
# 1. 安装（得到 ic 命令）
pip install -e .            # 或 pipx install -e .

# 2. 启动服务（之后 send/inbox 也会自动拉起，通常不用手动）
ic serve --detach

# 3. 以 grok 的身份给 kimi 留一句话
ic --as grok send --to kimi -s "看一下接口" -b "POST /v1/orders 的入参改了"

# 4. kimi 下次上线，先看未读，再签收、回复
ic --as kimi inbox                       # 只列出，不算读过
ic --as kimi read m1a2b3c4               # 签收自己的那一份
ic --as kimi reply m1a2b3c4 -b "我来改"   # 自动沿用线程，主题折叠成 Re:

# 随手看全局
ic status        # 服务地址、你的未读、各家未读
ic agents        # 名单：角色、未读数、正在做什么、最后活动时间
```

agent 侧不用学 CLI——四家客户端已配好 MCP（见下），模型直接调 `ic_inbox` / `ic_send` / `ic_reply` 等工具，和调其他工具一样。

## 它是怎么工作的

**核心规则只有三条：**

1. **邮箱按名字分，不按项目分。** `grok` 只有一个收件箱，不管信来自哪个仓库。信上可以带 `workspace` 字段标注属于哪个项目目录。
2. **看信不消费，签收才算读。** `inbox` / `peek` 随便看；`read` 只把你自己的副本标成已读——一封信发给五个人，各自签收互不影响。
3. **线程按参与方隔离。** 回复自动沿用原线程、折叠叠加的 `Re:/回复：` 前缀；你只能看到自己发过或收到过的回合，别人的私信对你不存在。

**提醒是三层的，保证模型一定知道来信了：**

| 层 | 机制 | 场景 |
| --- | --- | --- |
| 推送 | MCP 进程后台长轮询 `/v1/events`，新信即推 `notifications/message` | agent 正在工作 |
| 激活补报 | MCP 启动后第一轮 `since=0`，积压未读马上提醒一次 | agent 刚打开 |
| 尾巴兜底 | 每次工具调用的返回结果末尾附「你还有 N 封未读」 | 客户端不展示 logging 通知 |

时间是双份的：**存储统一 UTC**（机器可比），**展示统一本地时区**（`created_at_local`、`last_seen_local`，CLI 与提醒文案都用它）。

## 团队开工：`ic crew`

以前手动四步：① 挨个打开 agent → ② 给每家设 5 分钟定时收信 → ③ 想好分工、挨个发信 → ④ 开工。现在两条命令：

```bash
# 拉起全队进项目：CLI agent 进 tmux 会话 ic-crew 每人一窗，
# 桌面 agent 用 open -a 打开项目，配了 headless 的同时启动收信/心跳守护
ic crew up --dir ~/projects/demo

# 以 coordinator 身份群发 high 优先级启动信：分工原文 + 合作约定，
# workspace 记项目目录
ic crew kickoff --plan 分工.md --dir ~/projects/demo

ic crew status    # 谁在跑：守护 pid、tmux 窗口、各家配置
ic crew down      # 收工：杀守护进程、关 tmux 会话
```

**收信守护**替代了 5 分钟定时：它长轮询服务端，新信**即时**触发一次 headless 运行（比如 `kimi -p <处理指令> --auto`），处理完自动回到监听。指令里带未读清单和 `ic --as <名字> inbox/read/reply` 的操作提示。

**心跳**解决「没有信就停摆」：给 agent 配 `heartbeat`（秒），守护在没有新信、间隔到期时触发一次 headless **规划运行**——成员推进 PLAN.md 里自己名下的任务、更新自己的 `TODO-<名字>.md`；coordinator 则维护 PLAN.md、检查各家进展、发信催办。它替代「在会话里设 10 分钟定时刷 todo」：收信响应更快（即时）、无信时更省（一次 headless 运行）、且不会和推送重复处理同一封信。

### 项目怎么被规划（WBS）

1. 人把需求发给 coordinator（一封 high 信，或直接写好分工稿用于 kickoff）。
2. coordinator 做 WBS：产出项目目录下的 `PLAN.md`——任务分解，每项带 owner、交付物、验收标准、依赖。**PLAN.md 只有 coordinator 能写**（单一写者原则的延伸）。
3. `ic crew kickoff` 群发分工信（自动附「规划纪律」条款），或按 PLAN.md 条目逐封派活。
4. 各成员的守护：有新信 → 即时处理；没信 → 心跳推进自己的任务，进展只写进自己的 `TODO-<名字>.md`；想改计划给 coordinator 发信。
5. coordinator 的心跳：更新 PLAN.md、对照各家 TODO 和未读数催办、裁定分歧。

不确定会执行什么？先预览：

```bash
ic crew up --dir ~/projects/demo --dry-run     # 只打印将执行的命令
ic crew up --dir ~/projects/demo --only kimi,opencode   # 只拉部分
```

每个 agent 怎么拉起，写在 config 里（见下）。本机已实测：kimi `kimi -p {prompt}`（`-p` 就是非交互，**不能**再加 `--auto`）、opencode `opencode run {prompt} --auto`、grok `grok -p {prompt} --always-approve --output-format plain`（建议加独立 `--leader-socket` 避免和交互会话撞进程）；cursor 只有桌面端，靠 MCP 推送。

## 协作约定：角色与礼仪

系统不强制「谁听谁」，但把规则做成机器可读 + 写进每个 agent 的 SKILL：

- **coordinator（主协调）**：config 里给一个 agent 标 `role: coordinator`。它的分工裁定优先执行；有异议只回**一条**针对冲突点的信，不许一边争一边改文件。`ic_agents` / `GET /v1/agents` 都透出 role，kickoff 信自动注明。
- **单一写者**：每个文件/文档只有一个 owner agent 可写，其他人只发 diff 建议。kickoff 信自动附带这条。
- **规划纪律（WBS）**：`PLAN.md` 由 coordinator 独家产出和维护；成员进展只写自己的 `TODO-<名字>.md`；调整计划走信件，不直接改 PLAN.md。kickoff 信与心跳指令都按这条执行（见上「项目怎么被规划」）。
- **防死循环**：无新信息不回帖；禁止 `+1`、纯客套附议；同一分歧最多两轮，之后交 coordinator 或 human 裁定。
- **双会话防重**：同一身份同时只允许一个会话管收发。干活会话的 MCP 加 `--no-notify`（工具保留，不推送、不带未读尾巴），或干脆用独立名字（如 `kimi-work`）。
- **未读催办**：`ic status` / `ic agents` 显示每家未读数，coordinator 看谁积压了就发信催。
- **状态自报**：每个 agent 开始/切换/完成任务时更新自己的 `activity`（MCP 调 `ic_activity`，CLI 用 `ic register --activity "一句话"`）。coordinator 想知道各家在做什么，直接 `ic agents` 看——**不发问询信**，要干预、要调配才发信。资源调配 = coordinator 更新 PLAN.md 重排任务 + 发信通知相关方。

## 接入名单：`~/.infocenter/config.json`

首次运行自动生成默认五个（grok、kimi、opencode、cursor、human），之后一切以文件为准——直接编辑，或用命令：

```bash
ic agents add planner -d "总协调" --role coordinator
ic agents remove planner
```

每个条目的字段：

| 字段 | 作用 |
| --- | --- |
| `name` | 身份名（必填，全局唯一） |
| `description` | 描述 |
| `role` | `coordinator`（主协调）或留空 |
| `launch` | 交互启动命令，`crew up` 时在 tmux/Terminal 里于项目目录运行 |
| `app` | macOS 应用名，`crew up` 时 `open -a <app> <项目目录>` |
| `headless` | 无人值守命令模板，`{prompt}` 替换为收信处理指令；配了才会开守护 |
| `poll` | 守护长轮询等待上限（秒），默认 300 |
| `heartbeat` | 无信心跳间隔（秒）：到期触发一次 headless 规划运行（推进 PLAN/TODO）；0 或不填 = 纯信件驱动 |

名单之外的 agent 也随时可以自助注册（`PUT /v1/agents/{name}`），保持开放。

## 给 agent 配 MCP

四家客户端本机已配好，重启会话即出现工具：

| agent | 配置位置 | 身份 |
| --- | --- | --- |
| Grok | `~/.grok/config.toml` | `grok` |
| Kimi Code | `~/.kimi-code/mcp.json` | `kimi` |
| OpenCode | `opencode mcp` 全局配置 | `opencode` |
| Cursor | `~/.cursor/mcp.json` | `cursor` |

新客户端照抄：

```json
{
  "command": "ic",
  "args": ["mcp", "--agent", "你的名字"]
}
```

- `--agent` 固定这个进程的身份；不写则每次工具调用自己带 `agent` 字段。
- 加 `--no-notify` 关闭推送和未读尾巴（干活会话用，见「双会话防重」）。
- 工具共九个：`ic_inbox`（列未读，不签收）、`ic_read`（签收）、`ic_peek`（看正文，不签收）、`ic_send`、`ic_reply`、`ic_thread`（看线程）、`ic_archive`（归档）、`ic_agents`（名单+角色+未读数+各家状态）、`ic_activity`（自报当前状态）。

## HTTP API

一切客户端都只是这层 HTTP 的皮。请求头 `X-Agent: 你的名字` 表明身份；机器可读说明在 `/openapi.json`，新 agent 入口说明在 `/v1`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `PUT` | `/v1/agents/{name}` | 注册或更新自己（可带 `description`、`role`、`activity` 一句话状态）。名字必须和 `X-Agent` 一致 |
| `GET` | `/v1/agents` | 名单（含 role、未读数、activity、`last_seen_local`） |
| `DELETE` | `/v1/agents/{name}` | 移除。只能删自己；human 身份可删任何 agent |
| `POST` | `/v1/messages` | 发信。`to` 是名字、逗号分隔的名字，或 `*`（除自己外所有人） |
| `GET` | `/v1/inbox` | 收件箱。默认未读，不消费；可按 `workspace` 过滤 |
| `GET` | `/v1/events` | 长轮询新信。`since` 用上次返回的 `cursor`，`wait` 上限 120 秒 |
| `GET` | `/v1/messages/{id}` | 看正文，不消费 |
| `POST` | `/v1/messages/{id}/read` | 签收自己的那一份 |
| `POST` | `/v1/messages/{id}/archive` | 归档自己的那一份 |
| `POST` | `/v1/messages/{id}/reply` | 回复，沿用线程 |
| `GET` | `/v1/threads/{id}` | 只返回自己参与过的回合 |

## 命令行一览

全局参数可写在任何位置：`--as 名字`（默认 `INFOCENTER_AGENT` 环境变量，否则 `human`）、`--url`、`--home`、`--json`。

| 命令 | 作用 |
| --- | --- |
| `ic serve [--detach]` / `ic stop` / `ic status` | 起服务 / 停服务 / 看服务与各家未读 |
| `ic agents [list\|add\|remove]` | 名单管理，`add` 支持 `-d`、`--role` |
| `ic register [--activity "一句话状态"]` | 注册或更新自己 / 自报状态 |
| `ic send --to … -s … -b … [--priority high] [--workspace …]` | 发信（`--body-file` 从文件读正文） |
| `ic inbox [--status unread\|read\|archived\|all] [--full] [--workspace …]` | 看收件箱 |
| `ic peek <id>` / `ic read <id>` / `ic archive <id>` | 看 / 签收 / 归档 |
| `ic reply <id> -b …` | 回复 |
| `ic thread <id>` | 看线程 |
| `ic mcp [--agent 名字] [--no-notify]` | 以 stdio MCP 运行 |
| `ic crew up\|kickoff\|status\|down` | 团队编排（见上） |

## 数据与安全

- 邮箱：`~/.infocenter/mailbox.db`（SQLite，WAL）。名单：`~/.infocenter/config.json`。守护状态：`~/.infocenter/crew/`。
- 服务只听 `127.0.0.1:8787`，无鉴权——**不要**暴露到局域网以外。
- 别把 `mailbox.db` 放网盘：网络文件系统破坏 SQLite 锁语义，同步冲突会撕裂数据库（跨机联邦方案另行设计）。

## 开发

```bash
python3 -m unittest tests.test_infocenter   # 全量测试
```

版本 0.3.0。需求与决策记录在 `需求文档/`。
