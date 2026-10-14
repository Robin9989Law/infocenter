# D8 重启交接单（给 opencode 守护 / coordinator）

> 背景：现网服务端（`bin/ic serve --detach`，2026-10-14 04:46 启动）与各项目守护（pid 27466–27469 / 56260 等，13:12 启动）运行的是**旧代码**。D 线（窗口/案卷/`/ui`）与 A/B/P0/P1（及 P2）修复需重启才在生产生效。
> **触发条件**：待 **P2 补齐**（`_work/final-review.md` 的 P2 + `_work/verify-debug.md` 的 N1–N4 落地、全量测试绿）之后再执行，避免重启两次。
> **执行者**：**交互式（非无头）coordinator 窗口**——守护已明确拒绝（`PLAN.md:211`：`crew down` 会经 `_run_headless` 的 `except BaseException`（C-7/D2-a 设计）`killpg` 自身进程组 → 自杀；且需重启共享 8787 服务端、打断 grok/cursor 在飞 headless）。

## 步骤
1. 记录现状：`ic status`、`ic crew status --dir /Users/robinlaw/Downloads/信息中心`。
2. 确认无重要在跑 headless（`ps -Ao pid,etime,command | grep -E 'opencode run|bun.*omp -p|cursor-agent -p'`）。
3. 停守护：`ic crew down --dir <项目>`（信息中心 / LLM思维链 / 01_ICLR2027 各自）。
4. 重启服务端：`ic stop` → `bin/ic serve --detach`（确认进程加载工作区新码）。
5. 重启守护：`ic crew up --dir <项目>`。
6. 校验（新旧对比，关键 3 条）：
   - `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/ui` → **200**（旧码会 404）
   - `POST /v1/windows` → **非 404**
   - 各守护日志不再出现「登记办公室 … 失败，继续收信」
7. 把 D8 完成写入 `PLAN.md`（勾掉 D8）与 `TODO-opencode.md`。

## 注意
- 重启会打断在跑 headless；按契约离场会自动中止其未封卷案卷——确认无重要在跑任务再执行。
- 重启后 `ic agents` 的 activity/office 会重置，属正常。
- 重启后请复跑一次全量测试确认基线（预期 P2 后 >118 全绿）。
