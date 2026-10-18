---
name: infocenter
description: 信息中心轻薄多 Agent 协作底座（消息队列、案卷与任务队列、资源锁与控制）。充分释放 Agent Harness 自由度，注入大局观与架构观，通用于代码研发、科研攻关与商务协作。
---

# 信息中心（InfoCenter）协同与治理指南

本地消息中枢：`http://127.0.0.1:8787`。MCP 进程已用 `--agent` 固定身份。用工具或 CLI 交互，严禁冒充他人。
权威规范详见项目文档：[WORKFLOW_SPEC.md](file:///h:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/%E4%BF%A1%E6%81%AF%E4%B8%AD%E5%BF%83/docs/WORKFLOW_SPEC.md)。

---

## 一、轻薄协作底座与「大局观 · 架构观」

具体执行能力归于你自身的 **Harness**（工具链、领域技能、子智能体）；信息中心只做**最简协作底座**，不限制你的执行自由，但要求你时刻保持**大局观**与**架构观**：

* **大局观（Big-Picture Awareness）**：动手前先看所属案卷（Case）的终局目标与同项目同伴正在推进（`doing`）的并行任务态势，主动对齐接口与边界，不重复造轮子、不偏离主线。
* **架构观（Architectural Stewardship）**：对代码库、科研手稿或商务文档坚持**正交解耦、做减法优于打补丁**——杜绝局部防御性硬编码、杜绝重复定义与巨石文件膨胀，保持整体结构清晰精炼。

| 实体 | 所属核心 | 核心回答的问题 | 状态流转 / 生命周期 | 典型操作 |
|---|---|---|---|---|
| **Message** | 消息队列 | 「对方需要知道什么？分歧如何协商？」 | 发送 ➔ 接收 ➔ 签收 (read) ➔ 归档 | `ic send`, `ic inbox`, `ic reply`, `ic thread` |
| **Case** | 任务与案卷队列 | 「这个业务主题/课题谁在办？办到哪了？」 | 待办 ➔ 在办 ➔ 待谁 ➔ 暂停 ➔ 办结/中止 | `ic case <id>`, `ic cases`, `ic case --note` |
| **Task** | 任务与案卷队列 | 「这个具体交付单元是谁负责？怎样算干完？」 | `todo ➔ doing ➔ blocked ➔ done / stopped` | `ic task add`, `ic task start`, `ic task set`, `ic task verify` |
| **Lock / Window**| 资源与控制模块 | 「当前共享文件/文档是否安全？如何防互踩？」 | 申请 (lock) ➔ 续期 (stay) ➔ 释放 (unlock) | `ic lock`, `ic unlock`, `ic queue`, `ic guard` |

### 跨场景交付与验收口径
* **代码研发 (Code)**：交付物为代码提交；`acceptance` 为测试命令（如 `python -m unittest ...`）且通过架构适应度门禁。
* **科研攻关 (Research)**：交付物为论文草稿、定理证明、实验数据或文献综述；`acceptance` / `deps` 为 `file:<稿件或数据路径>` 或复现实验命令。
* **商务工作 (Business)**：交付物为商业方案、调研报告、合同修订或表格；`acceptance` / `deps` 为 `file:<交付文档路径>` 及核查清单。

---

## 二、标准作业程序：Agent 五步工作法（5-Step SOP）

1. **进场看盘（Look & Align）**：
   * `ic inbox`：看发给自己的未读留言。
   * `ic status`：查看全局指示灯（是否有案卷在等我 `--wait-for`、名下锁状态、待改排队）。注意：`ic status` 的等待数为全局总账，守护提示词中的「只计本项目」需带 `--workspace` 查看，两处不混。
2. **认领开工（Claim & Engage）**：
   * `ic read <msg_id>`：签收信件，自动认领关联线程案卷。
   * 若有分配给你的 Task 执行单：`ic task start <task_id>` 将状态推入 `doing`。
3. **持锁修改防踩踏（Lock Before Touch）**：
   * 修改共享目录文件前先拿写锁 `ic lock <路径...>`。若被占用（409 Conflict），立即执行 `ic queue <路径> --note "想改什么"` 记入待办，**立刻转向做其他事情，严禁原地死等或轮询！**
   * **无进度不续期**：租约默认 1800 秒。盘上无写入改动时续期会被拒 `409 no_progress`；确需空盘占位使用 `ic lock --stay`（最长累计 1 小时）。
4. **核验与释放（Verify & Release）**：
   * 核验交付物：`ic task verify <task_id>`（支持校验 `file:<路径>` 存在且非空、或 `<路径>@sha256:<哈希>` 一致性）。
   * 若在 Git 仓库提交代码，`pre-commit` 钩子自动运行 `ic guard`；修改完成后立即释放锁：`ic unlock`。
5. **闭环收尾（Close the Loop）**：
   * 案卷留痕：`ic case <id> --note "已完成交付 ➔ 凭证（commit 或交付文件路径）"`。
   * **交付绝不再另发信件**：案卷一行节点 + 交付物落盘就是最可靠的交付凭证，严禁发「我已经完成了请查收」等客套信件。
   * 任务单闭环：提请 coordinator 核验后关闭（`done`）。

---

## 三、通信与收发信（Message Bus）

1. **未读不消费**：`ic inbox` 仅列出未读，不改变状态；处理时调用 `ic read` 签收。
2. **workspace 回退补齐**：发信时显式传入 > 父信 > 该线程案卷 > 当前窗口登记 workspace > 客户端 cwd。解析后仍留空的信不会被长轮询/守护自动认领、`claimed_by` 空、谁显式 `read`/`mark` 谁认领。
3. **重发幂等**：同一发件人 + 同一父信 + 同一主题 + 正文逐字节相同 ⇒ 静默返回既有信（同文换收件人重发不会投给新收件人）。
4. **时区规范**：机器比较用 `created_at`（UTC，带 `Z`）；给人类/Agent 看的一律使用 `*_beijing` 字段（固定 UTC+8），并标明「北京时间」。

---

## 四、宏观案卷追踪（Case & WBS & Milestone）

1. **一线程一案卷**：回答「这件事谁在办、办到哪了」。状态：`pending(待办) / open(在办) / waiting(待谁) / paused(暂停) / aborted(中止) / done(办结)`。
2. **办结双门槛（Close Gate，缺一不可）**：
   * ① **硬性验收条件**：`ic case <id> --acceptance "可复现命令或交付物核验标准"`（单行 ≤ 200 字节，禁止主观形容词），否则拒 **409 `no_acceptance`**。
   * ② **无未读回信**：该线程上发给自己的回信必须全部签收，否则拒 **409 `unread_replies`**。
3. **接活与等谁（Wait-For Hook）**：
   * 需要他人输入时：`ic case <id> --note "在等什么" --wait-for <身份>`。

---

## 五、微观任务单（Task Machinery）

```bash
ic task list [--workspace <dir>] [--owner <name>] [--status <todo|doing|...>] # 查看任务列表
ic task show <task_id>                                                       # 查看任务详情与事件链
ic task start <task_id>                                                      # 开工：todo -> doing
ic task verify <task_id>                                                     # 核验 acceptance 与 deps 交付物（支持 file:<path>）
ic task set <task_id> blocked --reason "上游数据缺失"                         # 卡住：doing -> blocked
ic task set <task_id> done [--verify]                                        # 结项（需 coordinator）
ic task set <task_id> stopped --handoff "中间进度已保存在 draft.md"           # 停工（强制交接说明）
```
* **终态不可逆**：`done` 与 `stopped` 一旦落地绝不可再次更改。

---

## 六、角色权力与六大禁令红线

* `manager`（用户本人，旧名 `human`）：最高主权，唯一能更改名册、指定项目 coordinator；两名字在收件箱、往来线程、案卷等待态、名册未读数上完全等价。
* `coordinator`（项目协调 Agent）：项目内唯一有权更新 `PLAN.md`、派发任务、验收任务、修改里程碑。
* `worker`（普通执行 Agent）：专注推进分配给自己的任务。`ic agents` 与 `ic windows` 默认不列已离场的办公室，`--all` 看全。

### 六大禁令红线
1. ❌ **严禁形式主义台账内耗**：禁止无派单自发追写 `CHANGELOG.md` / `README.md`（日常「D线追码」），以 SQLite 库内状态为唯一事实源。
2. ❌ **严禁客套回帖**：禁止发送「收到」、「好的」、「+1」等无实际增量信息的废话。
3. ❌ **严禁把触发条件留在信里**：等谁动手必须落到案卷 `ic case <id> --wait-for <agent>` 上。
4. ❌ **严禁绕过锁硬写公共文件**：在共享目录直接改文件必须持锁，严禁私自篡改。
5. ❌ **严禁形容词验收**：`acceptance` 必须是可自动执行或可客观核验的交付物标准。
6. ❌ **严禁因会话关闭私自封卷**：没做完的案卷保持开放，等下一轮接力。
