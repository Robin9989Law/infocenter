# C1 文档一致性审查 · 偏差清单

> 审查日期：2026-10-14  
> 审查范围：README.md、SKILL.md vs 实际代码（cli.py、mcp_server.py、httpapi.py、store.py、config.py、crew.py）

---

## 偏差列表

### 1. 测试数量不一致（高优先级）

**位置**：README.md 第 454 行  
**文档声称**：`python3 -m unittest tests.test_infocenter   # 全量测试（14 个，含收信守护端到端）`  
**实际情况**：实际运行 `python3 -m unittest discover tests` 得到 **33 个测试**（PLAN.md 声称 31/31，但 unittest 输出显示 Ran 33 tests）  
**偏差**：文档说 14 个，实际 33 个  
**修正**：更新 README 第 454 行为 `# 全量测试（33 个，含收信守护端到端）`

---

### 2. config 字段不完整（中优先级）

**位置**：README.md 第 272-284 行  
**文档声称**：config 字段表只列出 `name`、`description`、`role`、`launch`、`app`、`headless`、`poll`、`heartbeat`  
**实际情况**：config.py 第 33 行 `_KNOWN_KEYS` 包含 `heartbeat_mode` 和 `heartbeat_prompt`；crew.py 第 471-472 行实际读取这两个字段  
**偏差**：文档缺少 `heartbeat_mode` 和 `heartbeat_prompt` 两个字段  
**修正**：在 README 第 284 行后追加两行：

| `heartbeat_mode` | 可选：`"idle"`（默认，新信会重置心跳计时）或 `"always"`（严格按间隔调度，新信不重置） |
| `heartbeat_prompt` | 可选：替代内置的心跳规划提示词（支持 `{agent}` `{project}` 占位符）；不填用内置模板 |

---

### 3. MCP 工具数量正确（无偏差）

**位置**：README.md 第 225-238 行  
**文档声称**：九个工具（ic_inbox、ic_read、ic_peek、ic_send、ic_reply、ic_thread、ic_archive、ic_agents、ic_activity）  
**实际情况**：mcp_server.py 第 129-193 行 `_tools()` 返回 9 个工具，名称与文档一致  
**结论**：✅ 无偏差

---

### 4. CLI 命令表完整（无偏差）

**位置**：README.md 第 166-199 行  
**文档声称**：列出 serve、stop、status、agents、register、send、inbox、peek、read、archive、reply、thread、mcp、crew（含 up/kickoff/status/down）  
**实际情况**：cli.py 第 24-94 行定义的子命令与文档一致  
**结论**：✅ 无偏差

---

### 5. HTTP API 表完整（无偏差）

**位置**：README.md 第 254-266 行  
**文档声称**：列出 PUT /v1/agents/{name}、GET /v1/agents、DELETE /v1/agents/{name}、POST /v1/messages、GET /v1/inbox、GET /v1/events、GET /v1/messages/{id}、POST /v1/messages/{id}/read、POST /v1/messages/{id}/archive、POST /v1/messages/{id}/reply、GET /v1/threads/{id}  
**实际情况**：httpapi.py 路由与文档一致（已验证）  
**结论**：✅ 无偏差

---

### 6. 版本号一致（无偏差）

**位置**：README.md 第 6 行、第 463 行；infocenter/__init__.py 第 3 行；pyproject.toml 第 7 行  
**文档声称**：version 0.4.0  
**实际情况**：
- README badge: `version-0.4.0`
- README footer: `版本 0.4.0`
- __init__.py: `__version__ = "0.4.0"`
- pyproject.toml: `dynamic = ["version"]` + `version = { attr = "infocenter.__version__" }`

**结论**：✅ 三处一致，无偏差

---

### 7. SKILL.md 工具列表正确（无偏差）

**位置**：skills/infocenter/SKILL.md 第 10-16 行  
**文档声称**：提到 ic_inbox、ic_read、ic_send、ic_reply、ic_agents、ic_activity  
**实际情况**：与 mcp_server.py 工具名一致  
**结论**：✅ 无偏差

---

## 修正计划

1. **README.md 第 454 行**：`14 个` → `33 个`
2. **README.md 第 284 行后**：追加 `heartbeat_mode` 和 `heartbeat_prompt` 字段说明

---

## 验收标准

- [ ] README 测试数量与实际一致
- [ ] README config 字段表完整
- [ ] 版本号三处一致（已确认）
- [ ] MCP 工具九项一致（已确认）
- [ ] CLI 命令表一致（已确认）
- [ ] HTTP API 表一致（已确认）
