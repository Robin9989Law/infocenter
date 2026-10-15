import { Plugin } from "@opencode/plugin"

/**
 * 写锁插件：把「改文件前先拿钥匙」变成**机器行为**（human 2026-10-15 定稿的规矩）。
 *
 * 规矩（原话）：**所有文件都锁住。谁要改文件拿钥匙开锁。一旦开锁，其他 agent 就不行修改这个文件。
 * 改后继续锁上。**
 *
 * 落地：在 `edit` / `write` 真正落盘**之前**，自动向信息中心 `POST /v1/locks` 拿该文件的钥匙：
 *   - 拿到钥匙 → 放行这次写（重复拿＝续期，所以连续改同一个文件不会自己把自己锁死）
 *   - 别人正开着锁 → **直接拒绝这次写**（工具报错，模型看得见；不会"以为改了、其实没改"）
 *   - 信息中心不可达 / 其它异常 → **放行**（离线不阻断，与 git 钩子的口径一致）
 *
 * 边界（诚实说明，别当成万能）：
 *   - 只罩得住**本 OpenCode 会话**里的 `edit` / `write`；`shell`（`> file`、`sed -i`）绕得过去。
 *   - 别的 agent（cursor / grok / kimi 等各自的 CLI）没有这个插件，他们靠"自觉拿锁 + git 提交闸"。
 *   - 只对**本项目目录下**的文件生效（默认取插件所在目录）；否则会给别的项目乱上锁。
 *
 * 钥匙什么时候还？——**提交即放**：`git commit` 之后 `ic unlock`（锁 TTL 也是兜底）。
 */

const WRITE_TOOLS = new Set(["edit", "write", "patch", "multiedit", "create"])
const DEFAULT_URL = "http://127.0.0.1:8787"

function pickPaths(input: unknown): string[] {
  const out: string[] = []
  const push = (value: unknown) => {
    if (typeof value === "string" && value.trim()) out.push(value)
  }
  if (input && typeof input === "object") {
    const record = input as Record<string, unknown>
    push(record.filePath)
    push(record.path)
    push(record.file)
    for (const key of ["paths", "files", "edits", "changes"]) {
      const value = record[key]
      if (Array.isArray(value)) {
        for (const item of value) {
          if (typeof item === "string") push(item)
          else if (item && typeof item === "object") {
            const entry = item as Record<string, unknown>
            push(entry.filePath ?? entry.path ?? entry.file)
          }
        }
      }
    }
  }
  return out
}

export default Plugin.define({
  id: "infocenter-lock",
  async setup(ctx) {
    const options = (ctx.options ?? {}) as Record<string, unknown>
    const url = String(options.url ?? process.env.INFOCENTER_URL ?? DEFAULT_URL).replace(/\/+$/, "")
    const agent = String(options.agent ?? process.env.INFOCENTER_AGENT ?? "opencode")
    const scope = String(options.scope ?? ctx.location?.directory ?? "").replace(/\/+$/, "")
    const ttl = Number(options.ttl ?? 1800)

    const inScope = (path: string) => (scope ? path.startsWith(scope) : true)

    async function takeKey(paths: string[]): Promise<{ ok: boolean; status: number; detail: string }> {
      const response = await fetch(`${url}/v1/locks`, {
        method: "POST",
        headers: { "X-Agent": agent, "Content-Type": "application/json" },
        body: JSON.stringify({ paths, ttl }),
      })
      let detail = ""
      if (!response.ok) {
        detail = await response.text().catch(() => "")
      }
      return { ok: response.ok, status: response.status, detail }
    }

    await ctx.tool.hook("execute.before", async (event) => {
      if (!WRITE_TOOLS.has(String(event.tool))) return
      const paths = pickPaths(event.input).filter(inScope)
      if (paths.length === 0) return
      let result: { ok: boolean; status: number; detail: string }
      try {
        result = await takeKey(paths)
      } catch {
        return // 拿不到服务（离线/崩了）：不阻断，和 git 钩子的口径一致
      }
      if (result.ok) return
      if (result.status === 409 || result.status === 403) {
        // 别人正开着锁 ⇒ 这次写直接失败（这就是"一旦开锁，其他 agent 就不能改"）
        throw new Error(
          `写锁：这些文件正被别人开着锁，现在不能改。\n${result.detail}\n` +
            `不要干等、不要发信。把要改的事记下来：ic queue <路径> --note "要改什么"（会进你自己的 TODO），然后去做别的。`,
        )
      }
      // 其它状态（400 等）不阻断：插件不该比人更懂
    })
  },
})
