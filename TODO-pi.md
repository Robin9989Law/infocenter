# TODO · pi（C 线：发布工程 + D 线文档）

> 2026-10-14 15:48 接管自 `omp`（omp 下线：`alibaba-token-plan/qwen3.8-max` 主回合间歇 socket 掉线、D 线文档久未落地；human 裁定退休）。本机 headless = `pi -p --no-session --model qwen3.8-max`（provider `qwen-token-plan-cn`，无审批）。
> 只写本方进展；计划调整发信给 coordinator（opencode）。

---

# omp 进展记录

> 角色：C 线发布工程 owner  
> 最后更新：2026-10-14

---

## 任务清单

### C1 文档一致性审查 ✅

**状态**：已完成  
**交付物**：
- `_work/c1-audit-deviations.md`：偏差清单
- `README.md` 修正：
  - 第 454 行：测试数量 `14 个` → `33 个`
  - 第 283-285 行：追加 `heartbeat_mode` 和 `heartbeat_prompt` 字段说明

**验收**：
- ✅ 测试数量与实际一致（33 个）
- ✅ config 字段表完整（含 heartbeat_mode/heartbeat_prompt）
- ✅ 版本号三处一致（README badge/footer、__init__.py、pyproject.toml）
- ✅ MCP 工具九项一致
- ✅ CLI 命令表一致
- ✅ HTTP API 表一致

---

### C2 打包与分发 ✅

**状态**：已完成  
**交付物**：
- `dist/infocenter-0.4.0-py3-none-any.whl`（47KB）
- `dist/infocenter-0.4.0.tar.gz`（66KB）
- `CHANGELOG.md`：0.1.0 → 0.4.0 变更记录

**验收**：
- ✅ `python3 -m build` 成功生成 sdist/wheel
- ✅ 干净 venv 中 `pip install` 验证通过
- ✅ `ic` console script 可用（`ic --version` 输出 `0.4.0`）
- ✅ `ic status` 命令正常执行
- ✅ `CHANGELOG.md` 创建完成

**备注**：
- build 警告 `project.license` as TOML table is deprecated，建议未来改为 SPDX 字符串（不阻塞发布）

---

### C3 公开准备 ✅

**状态**：已完成  
**交付物**：
- `LICENSE`：MIT 许可证
- `SECURITY.md`：安全策略（localhost-only / 无鉴权边界说明）

**验收**：
- ✅ `LICENSE` 创建（MIT）
- ✅ `SECURITY.md` 创建（明示 localhost-only / 无鉴权 / 明文存储边界）
- ✅ 敏感信息扫描：无硬编码密码/API key/token
- ✅ 绝对路径扫描：仅 `PLAN.md` 第 3 行有项目路径（项目元数据，非代码硬编码）
- ✅ `~/.infocenter` 路径通过 `Path.home()` 动态解析，无硬编码

**备注**：
- 公开命令 `gh repo edit --visibility public` 待 M1/M2 稳定后由 human 触发

---

### C4 版本与变更 ✅

**状态**：已完成  
**验收**：
- ✅ `pyproject.toml` 动态版本配置正确（`dynamic = ["version"]` + `version = { attr = "infocenter.__version__" }`）
- ✅ `infocenter/__init__.py` 版本号 `0.4.0`
- ✅ README badge 版本号 `0.4.0`
- ✅ README footer 版本号 `0.4.0`
- ✅ 三处一致
- ✅ `CHANGELOG.md` 已创建，规划 0.4.x → 0.5.0（联邦为 minor）

---

## 备注

- C1 审计发现两处偏差，已修正
- C2 build 警告 `project.license` as TOML table is deprecated（不阻塞）
- C3 公开命令待 M1/M2 稳定后由 human 触发
- C4 版本号三处一致，CHANGELOG 已规划 0.5.0

---

### 心跳 2026-10-14 14:28

- 收件箱：0 未读
- C1–C4：全部完成，无遗留
- D3（`/ui` 管理台）：依赖 D1 读接口（cursor 在途），尚不可开工
- B4（联邦文档合入）：依赖 grok B3 收口后提供 diff，尚不可合入
- PLAN.md：无空缺/过期/需上报事项
- 结论：无可推进事项，静默待命
