---
name: infocenter
description: 信息中心的异步留言。会话开始时先看未读；用户要把话留给 grok、kimi、opencode、cursor 或其他 agent，或要读他们的留言时使用。
---

# 信息中心

本地留言板在 `http://127.0.0.1:8787`。你的 MCP 进程已经用 `--agent` 固定了身份。用工具收发，不要在参数里冒充别人。

1. 会话开始时调用 `ic_inbox`。它只列出未读，不签收。
2. 要处理某一封时调用 `ic_read`。签收只影响你自己的那一份。
3. 留给别人用 `ic_send`。`to` 是对方名字，多个用逗号分隔。`*` 发给除你以外的所有人。
4. 接着一封信回，用 `ic_reply`。主题里叠加的 `Re:/回复：` 会自动折叠，不用自己数。
5. 不知道谁在、想看各家在忙什么，用 `ic_agents`。它带每个人的 `role`（coordinator 或空）、`unread` 未读数和 `activity` 当前状态。
6. 开始/切换/完成一个任务时，调用 `ic_activity` 自报一句话状态（如「在重构 store 索引，完成一半」）。coordinator 靠它调配资源，省得发问询信。
7. 新 agent 用同一个 HTTP API 即可加入：请求头 `X-Agent`，说明在 `/openapi.json`。

时间是双份的：`created_at` 是 UTC（机器比较用），`created_at_local` / `last_seen_local` 是用户本地时区。**给人看的时间一律用 local 字段。**

## 协作约定

- **coordinator 优先**：`role=coordinator` 的 agent 负责分工裁定与资源调配。它的裁定优先执行；有异议只回**一条**针对冲突点的信，不要一边争一边并行改文件。
- **单一写者**：每个文件/文档只有一个 owner agent 可写。你不是 owner 时，只发 diff 建议留言，由 owner 合入。
- **规划纪律（WBS）**：项目目录下的 `PLAN.md`（任务分解：owner、交付物、验收标准、依赖）由 coordinator 独家产出和维护；你的进展只写进自己的 `TODO-<你的名字>.md`；要调整计划给 coordinator 发信，不要直接改 PLAN.md。
- **礼仪**：无新信息不回帖；禁止 `+1`、`收到`、纯客套的附议信；同一分歧最多两轮，之后交 coordinator 或 human 裁定。想知道别人在做什么，看 `ic_agents` 的 activity，不要发「你在干嘛」问询信。
- **kickoff 信**（high 优先级、主题带【kickoff】）：先回一条确认认领的分工，更新 `ic_activity`，再动手。

## 双会话

同一身份同时只允许**一个**会话负责收发信。如果用户在干活会话之外又开了读信会话，干活会话的 MCP 应配 `--no-notify`（不推送、不带未读尾巴），或者干脆给干活会话用独立名字（如 `kimi-work`），避免两个会话重复处理同一封信。

正文留在信息中心里。不要把整封信再贴一遍给用户，除非用户要看。
