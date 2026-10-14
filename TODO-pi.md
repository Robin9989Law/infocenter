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

---

## C 线 · D 线文档收口 ✅（心跳 2026-10-14 23:1x）

**背景**：M5 冻结后，PLAN §九 记 pi 残余 = `SKILL.md`（已落盘未提交）+ `需求文档/01-需求整理.md`（R14 节仍写「写操作下移 待做」、M5+ 簇未覆盖）。本轮把三份 pi 名下文档全部与冻结码对齐并提交。

**交付**（commit `90e6a1f`，仅含 pi 名下文件，未触碰他人 index.html/PLAN/TODO-*/\_work）：
- `README.md`（+45/−4）：窗口/案卷/管理台 HTTP 接口表（claim/transfer/reopen/restart/open/daemons/headless-runs/windows/projects/activity）、config 热加载(F-1)、`/ui` 三栏级联 + 二级页面写操作、crew 状态键 `<名字>--<项目sha1[:8]>`、测试数 144→156、FAQ（热加载/一键重启）、测试前清 `INFOCENTER_*` env 提示。
- `skills/infocenter/SKILL.md`（+30/−7）：窗口号/线程认领/案卷（`ic_case` 只读）/`ic case|cases|windows`/`crew pause|resume|abort|close`/封卷重开语义/管理台。
- `需求文档/01-需求整理.md`（+80/−16）：新增 R15 窗口认领 / R16 案卷 / R17 管理台 `/ui` / R18 M5+ 增强（M5+1~8 + F-1），R14 三栏级联状态改「已全量收口」。

**对码核验（C 线 owner 职责：文档与实码一致）**：
- 引用的 19 个 commit 全部存在（`4bc9baa`…`ba7f650`）。
- 端点齐：`/v1/cases/{id}/{pause|resume|abort|close|reopen|claim|transfer}`、`/v1/{windows,projects,activity,crew,restart,open,daemons,headless/runs}` 均在 `httpapi.py`。
- 窗口号格式 `w-<hex>`（自检案卷自证 `w-38392b`）、`INFOCENTER_WINDOW`、`X-Window`、MCP `ic_case`、`crew pause|resume|abort|close`、`ic case|cases|windows` 与实码逐条一致。
- 全量回归：`env -u INFOCENTER_AGENT -u INFOCENTER_WINDOW python3 -W error::ResourceWarning -m unittest discover -s tests` = **156/156 OK 零 ResourceWarning**。

**上报 coordinator（opencode）**：PLAN §九 `·` 行 / 22:3x / 22:4x 补登中「`需求文档` 仍 18:15 未动、`SKILL.md` 未提交」现已过期，随本次提交（`90e6a1f`）闭合，请其更新 PLAN（pi 不改 PLAN）。

**自模型迁移**：本次 headless 运行于 `qwen3.8-flash`（`PI_MODEL=qwen3.8-flash`，provider `qwen-token-plan-cn`），迁移自检案卷 `m1a13ad7cc69c391c3` 状态「待 human」，非我方可推进。

**结论**：pi 名下 C 线（C1–C4）+ D 线文档（README/SKILL/需求文档）全部收口并提交。

---

### 心跳 2026-10-14 23:5x · 追码 da3377c（C 线文档滞后闭合）

**触发**：HEAD 出现上次收口后的新 commit `da3377c`（23:34，human 直改 `crew.py`：身份配置 `headless_timeout`，单次 headless 墙钟上限，缺省 1800s，钳制 10s–7d，到期 SIGKILL 进程组，热加载；+2 回归 `HeadlessTimeoutTest`）——属 pi 名下文档与实码一致性范围。

**交付**（commit `bac9f2d`，仅 pi 名下文件，未触碰他人 PLAN/index.html/TODO-*/\_work）：
- `README.md`：config 字段表新增 `headless_timeout` 行；热加载段、FAQ（热加载 Q 补字段 + 新增「headless 挂死怎么办」Q）；测试数 156→158。
- `需求文档/01-需求整理.md`：R18 标题 54→55 commit、时间窗延至 23:34；新增 M5+9 行（`da3377c`）；基线口径补「`da3377c` 后 158/158（pi 独立复跑）」。
- `SKILL.md` 无需改：其面向使用约定，不含 config 字段清单（grep 验证），非遗漏。

**核验**：全量回归 `env -u INFOCENTER_* python3 -W error::ResourceWarning -m unittest discover -s tests` = **158/158 OK 零 ResourceWarning**（23:5x，pi 独立复跑）；`_headless_timeout_seconds` 缺省/覆盖/非法/下限与 README 描述逐条对码一致。

**上报 coordinator**：PLAN §九/§一 M4 行「54 commit（截至 22:17）· 基线 156/156」及 22:4x 口径已因 `da3377c` 过期，已发信提请刷新（pi 不改 PLAN）。

**新增 backlog（C 线，待 release-cut）**：`CHANGELOG.md` [Unreleased] 严重滞后—0.4.0（`8daf1bc`）后已 59 commit（D 线窗口/案卷/管理台、M5 加固、M5+ 簇均未入账）；且版本号仍 `0.4.0`、D4 公开未拍板。全量补账应随 0.5.0 release-cut 一次做准（联邦/D线为 minor），不宜在 human 迭代未停笔期间碎片式追写。已列入待发，待 D4/发版指令。

**结论**：本轮可推进事项已闭合；pi 名下无其余未完成在办事项（CHANGELOG 补账为显式挂起项，依赖发版时机裁定）。
