# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- R10 跨机联邦（B 线）：网盘 JSON 中继传输层，`节点.名字` 身份命名

### Changed
- 测试数量从 16 增加到 33（A 线质量加固）

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
