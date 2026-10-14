# B2 设计：R10 网盘 JSON 中继

- 作者：grok（B 线）
- 日期：2026-10-14
- 状态：**已冻结**（2026-10-14，coordinator 终签 `m1a138f175e4072b7d`；kimi 复核 `_work/verify-b2.md`）。§12 六项照准。下面 5 条复核修正已写入 §4、§5、§7、§10，不重开评审。未配置 `node` 时单机行为不变。
- 前提：B1 已冻结（human 签字；coordinator 信 `m1a138d87de4621267`、`m1a138da164e6eb70e`）。D1 = v1 只做网盘 JSON 中继。D2 = 恰好一个点的 `节点.名字`。
- 依据：`PLAN.md` §三、`_work/federation-decision.md`、`store.py` 的消息表与 `normalize_name`。

## 1. 边界

做：

- 每台机器仍用本地 `~/.infocenter/mailbox.db`（或 `INFOCENTER_HOME`）。库不进网盘。
- 项目目录里的 `<项目>/.infocenter/outbox/<节点>/` 只当传输带。一封信一个 JSON。
- `ic sync export|import|once --dir <项目>`。延迟等于网盘同步周期，至少一次，按消息 id 去重。
- 签收回执走同一路径族，方向相反：阅读方写在自己的节点目录里，发信方下次导入时应用。

不做（本草案不含，冻结时不要把它们算进 B3）：

- Tailscale、节点间 HTTP、加密、自动删旧文件。
- 收紧 `normalize_name`。点号今天就能过，既有名字继续合法。联邦语法只在同步边界上用。
- 在守护进程里调 `ic sync`。`crew.py` 所有权已经交还（coordinator `m1a138dee08167d256`），这一步等 B2 冻结后另开，不挡 B3 的 CLI。
- 新的 HTTP 路由。`ic sync` 直接写本地库，不要求服务已启动。

跨进程限制：导入发生在另一个进程里，叫不到服务进程的 `Condition`。已经挂着的长轮询要等到本次 `wait` 结束才看见新信；下一次轮询会立刻从库里读到。空闲时最坏多一个 poll 间隔。R10 已接受秒到分钟级。

## 2. 名字

`node` 是本机节点段，不是 agent。配了之后，本机裸名 `grok` 的线上身份是 `<node>.grok`。

一段的语法与现有名字相同，但段内不能有点：`^[\w][\w-]{0,31}$`，再 `lower()`。`<node>.<裸名>` 整串必须能通过现有 `normalize_name`（≤64）。通不过的收件人拒绝导出，不写出半个名字。

线上身份（wire name）= 恰好一个点，左右都是合法段。`a.b.c`、`robin.`、`.grok` 不是联邦身份。

| 收件人 | 未配置 node | 已配置 node |
| --- | --- | --- |
| 裸名 `kimi` | 只留本地 | 只留本地，不导出 |
| 他机 `alice.kimi` | 与今天一样，只是本地一个带点的名字 | 本地留投递行（见 §10），并导出 |
| 本机线上名 `<node>.grok` | 同普通名字 | 不导出，避免环回 |
| `*` | 展开除自己以外的全部 agent | 仍只展开本机名单：跳过 wire name。多点的历史本地名仍算本机 |

发给裸名的信不出本机。`*` 不跨节点。一封信可以一部分本地投递、一部分导出。

没配 `node` 时，上表右列全部不生效，`send` 与今天一致。

## 3. 目录与文件名

```
<项目>/.infocenter/outbox/<节点>/<message_id>.json
<项目>/.infocenter/outbox/<节点>/receipts/<message_id>--<裸名>.json
```

- `--dir` 决定项目。默认 outbox 是 `<dir>/.infocenter/outbox`。
- 一个节点只写自己的子目录，不写别人的目录。网盘上每个文件只有一个写者。同一 `node` 不许两台机器同时用。
- `message_id` 沿用现有 `m` + 十六进制。文件名必须等于 `{id}.json`，且 id 匹配 `^m[0-9a-f]{8,40}$`。回执文件名必须等于 `{id}--{裸名}.json`。
- 不读符号链接。不进入 `receipts` 以外的子目录。路径必须落在该节点目录内。
- 下列名字忽略，不当信、也不当节点：以 `.` 开头、含空格、含 `conflict` / `冲突`、复制名如 `(1)`、任何不匹配上面正则的文件或目录。
- 原子写：同目录临时文件 `.{目的名}.{pid}.{随机}.tmp`，写完 `fsync`，再 `os.replace`。只扫掉自己目录里超过 1 小时的 `.tmp`。不碰别人的目录。
- `.gitignore` 今天没有 `.infocenter/`。网盘仍会同步这个目录；git 不应提交它。B4 把这一行 diff 给 omp，B3 不改 README / 需求文档 / `.gitignore`。

## 4. 消息 JSON

`v` 固定为 1。未知的 `v` 或未知的 `kind` 跳过，不记入永久拒绝（以后认版本）。多出来的字段忽略。缺必填字段、或必填字段通不过现有校验：按文件字节记入本地拒绝，字节不变就不再试。

```json
{
  "v": 1,
  "kind": "message",
  "id": "m1a138d87de4621267",
  "thread_id": "m1a138d3e2db277508",
  "reply_to": null,
  "from": "alice.grok",
  "to": ["robin.kimi", "alice.opencode"],
  "subject": "主题",
  "body": "正文",
  "priority": "normal",
  "workspace": "/Users/alice/Drive/信息中心",
  "workspace_name": "信息中心",
  "created_at": "2026-10-14T05:17:54Z"
}
```

| 字段 | 规则 |
| --- | --- |
| `from` | 导出时把本地裸名写成 `<node>.<裸名>`。已是带点的 `from` 不导出 |
| `to` | 线上名字列表。本地裸名写成 `<node>.<裸名>`。不含 `*`。多点本地名不进这份列表。导入只给本机 node 段建投递；他机条目不产生投递，仅作记录 |
| `id` / `thread_id` / `reply_to` / `created_at` | 原样带走。导入不得另发新 id，不得改成导入时间 |
| `subject` / `body` / `priority` | 沿用现有限制：主题 1–200 且无换行；正文非空且 ≤100000；priority 只是 `normal` 或 `high` |
| `workspace` | 发信方机器上的绝对路径，只作记录 |
| `workspace_name` | 该路径的目录名 |

导入时 SQLite 的 `workspace` 改写为本次 `--dir` 的绝对路径。这样收信方的 `inbox --workspace`（等值匹配）和守护的 basename 匹配都能命中。文件落在哪个项目的 outbox，就属于哪个项目；目录名对不上只记警告，不拒信。

没有 workspace 的信不导出。无主的信继续只留本机。

单文件上限 1000000 字节，与 `httpapi.MAX_BODY` 相同。超过的不读进内存、不解析。上限按本地正文上限反推：100000 字符在 JSON 里最坏是控制字符的 `\u00XX`（6 字节/字）= 600000，UTF-8 汉字约 300000；1 MB 装得下合法信，也和 HTTP 正文预算对齐。导出使用 `ensure_ascii=False`。

超限不再跳过重扫。导入按路径 + 大小 + mtime 记本地拒绝，三者不变不再试，原因 `envelope_too_large`。导出若序列化结果超过上限，不写文件，同样计 `rejected` / `envelope_too_large`，发送方在 `ic sync` 的计数里看得见。

## 5. 回执 JSON

只表示「签收成 read」。归档不跨节点。

```json
{
  "v": 1,
  "kind": "receipt",
  "message_id": "m1a138d87de4621267",
  "by": "robin.kimi",
  "status": "read",
  "read_at": "2026-10-14T05:20:00Z"
}
```

- 回执只从 `outbox/<节点>/receipts/` 读。
- 文件名里的 id、裸名必须与 `message_id`、`by` 的右段一致，否则拒绝。
- `by` 必须是写这个文件的节点的线上名字。导入时除了文件名与 `message_id`、`by` 的右段一致，还必须校验 `by` 的左段（节点段）严格等于所在目录名 `<peer>`。不符则拒绝，不按文件内容里的 `by` 决定来源。
- 只有 `from` 是他机 wire name 的信，本地签收后才会在下次 export 写出回执。本机发出的信不给自己写回执。
- 发信方导入时：本地有这条消息、投递行的 `to_agent` 等于 `by`、且当前是 `unread`，才把它标成 `read` 并写上 `read_at`。已是 `read` 的保持原来的 `read_at`。`archived` 不动。没有这条投递就跳过，下次再试，不永久拒绝（回执可以先于导入到达对方，尽管正常顺序下发信方自己有原信）。
- 回执不为发信方产生新的未读，不 `_signal`。

## 6. 导出

选出同时满足这些条件的本地消息：

1. `workspace` 按现有 `_workspace_scope` 属于 `--dir`。
2. `from_agent` 不含点（本机裸名）。导入进来的信不再导出。
3. 至少有一个投递对象是他机 wire name。

一封信一个文件。序列化用 `json.dumps(..., ensure_ascii=False)` 再 UTF-8。超过 1000000 字节不写文件，计 `rejected` / `envelope_too_large`。语义不变就不重写。本地账本丢了也没关系：目标文件已在且语义相同则跳过。

`peers` 不限制导出。配了也只限制导入。发给尚未出现目录的节点，文件先写在自己的 outbox 里，对方目录出现后他们来取。

## 7. 导入、幂等、乱序

`once` 的顺序：抢锁 → 清自己的旧 tmp → export 消息 → export 回执 → import 他机消息 → import 他机回执。

导入来源：`peers` 非空则只扫这些节点目录；缺目录不算错误。`peers` 空或省略则发现 outbox 下每一个合法节点段，跳过自己。

每封信一个事务。幂等键是 `id`，不是文件字节：

- 没有这个 id：插入原 id、原 `thread_id`、原 `reply_to`、原 `created_at`。只给「`to` 里等于 `<本机 node>.<裸名>`」的裸名建投递，状态 `unread`。不把发信方 upsert 进 `agents`（否则以后的 `*` 会把他们算进本机名单）。不 `_signal`。
- `to` 里没有任何 `<本机 node>.<裸名>`（`local_bare_names` 为空，例如第三个节点扫到 A 发给 B 的信）：直接 `skipped`，不插入 0 投递行的孤儿记录。
- 已有这个 id，且不可变字段相同：空操作。不可变字段 = `thread_id`、`reply_to`、`from`、`subject`、`body`、`priority`、`created_at`，加上 `to`。`workspace` 不算。`messages` 没有 `to` 列，接收方 `deliveries` 只存本机裸名，所以库内 `to` 的等价是 `sorted(_recipients(conn, id)) == sorted(local_bare_names)`。两份信封之间仍比排序后的完整 `to`。
- 已有这个 id，不可变字段不同：保留先到的那封，计数 `conflict`，不覆盖。

乱序：回复可以比父信先到。`reply_to` 没有外键，先插入回复；父信后到后，`thread` 按原来的 `thread_id` 合上。不在导入时调用 `send` / `reply`，那会另发新 id，也会在父信不可见时失败。

环回：`from` 的节点段等于自己的，不导入。

本地账本 `~/.infocenter/federation.sqlite` 只记导出、导入、拒绝（路径 + 字节 sha256 + 原因）。正确性以邮箱里的 `messages.id` 为准。账本删了可以重放。

## 8. 失败与重放

| 情况 | 结果 |
| --- | --- |
| 未配置 `node`，或节点段不合法 | `ic sync` 退出码 2，`node_not_configured` / `invalid_node`。不建目录，不写库 |
| `--dir` 不是目录 | 退出码 2 |
| 他机目录还不存在 | 跳过 |
| 0 字节、半截 JSON、解析失败 | 跳过，下次再试。不永久拒绝 |
| 文件大于 1000000 字节 | 不读内容、不解析。按路径 + 大小 + mtime 本地拒绝，三者不变不再试。导出侧同一上限，超了不写文件 |
| 模式不合法且字节已稳定 | 本地拒绝，字节不变不再试 |
| 未知 `v` / `kind` | 跳过，不拒绝 |
| 冲突副本、临时文件、符号链接 | 忽略 |
| 同 id 同语义 | 空操作 |
| 同 id 不同语义 | 留先到的，计 `conflict` |
| 回复先于父信 | 照常导入 |
| 回执时本机还没有对应投递 | 跳过，下次再试 |
| 本机该投递已归档 | 回执不改它 |
| 两进程同时 sync | `~/.infocenter/federation.lock` 独占，最多等 30 秒，超时退出码 1，`sync_busy` |
| 写 tmp 后崩溃 | 点号文件被忽略；自己的、超过 1 小时的才扫 |
| rename 之后、账本之前崩溃 | 下次按 id 再对一次，不会重复投递 |
| 同一 node 两台机器 | 不支持。后写的文件赢，语义冲突走上面的 conflict 行 |

没有自动 GC。磁盘会随信增长。要删只能人工删自己节点目录里的文件；删了之后至少一次的副本已经在对方库里。

## 9. CLI 与 config

```
ic sync export --dir <项目>
ic sync import --dir <项目>
ic sync once   --dir <项目>
```

不需要 `--as`。不拉起服务。成功退出码 0，stdout 给出 `exported`、`imported`、`receipts_out`、`receipts_in`、`skipped`、`rejected`、`conflict`。`--json` 时输出这份计数。

`~/.infocenter/config.json` 顶层增加（都可选）：

| 字段 | 含义 |
| --- | --- |
| `node` | 本机节点段。缺省、空、null = 联邦关闭 |
| `peers` | 节点段列表。缺省或 `[]` = 发现目录。只约束导入 |
| `outbox_dir` | 绝对路径，或相对 `--dir` 的路径。缺省 = `<dir>/.infocenter/outbox`。多项目不要设它 |

现有 `load()` 只返回 `agents`，`add` / `remove` 再 `save` 会把顶层键写丢。B3 在 `config.py`（本方文件）修：改名单时保留 `node` / `peers` / `outbox_dir`。`load()` 的返回形状不变，调用方仍只读 `agents`。另加 `federation_settings(home)` 给 sync 用。

## 10. 需要 cursor 合入的 diff（B3，已在工作区）

`store.py`、`httpapi.py` 不是本方文件。未配置 `node` 时行为不变。`sync.py` 调用下面的方法，不自己写 `messages` / `deliveries`。

1. `Store.send(..., local_node: str | None = None)`。`None` 时与今天相同。有值时，`*` 的展开结果去掉 wire name（恰好一个点、左右都是合法段）。多点的历史本地名仍留下。点名发给 wire name 仍 upsert 并留下投递行，这样发信方的 `to` 和回执有地方落。过滤后没有人则仍是 `delivers_to_nobody`。
2. `Store.import_remote(envelope, local_bare_names) -> {status: imported|duplicate|conflict|skipped}`。按 §7 插入原 id / `thread_id` / `reply_to` / `created_at`。`local_bare_names` 为空时返回 `skipped` 且不插入。不 upsert 发信方，不 `_signal`。库内比对 `to` 用 `sorted(_recipients(conn, id)) == sorted(local_bare_names)`。
3. `Store.apply_receipt(message_id, by, read_at) -> {status: applied|duplicate|absent|archived}`。按 §5：`to_agent == by` 且当前 `unread` 才标成 `read` 并写上这份 `read_at`。已是 `read` 返回 `duplicate`，保留原来的 `read_at`。`archived` 不改。没有这条投递返回 `absent`。不 `_signal`。
4. `Store.reply(..., local_node: str | None = None)` 把 `local_node` 传给 `self.send`（覆盖 `reply --to *`）。`httpapi` 在 `POST /v1/messages` 和 reply 上用 `config.federation_settings(store.path.parent).get("node")` 同时传给 `send` 与 `reply`。没配置、空、null 就不传（`None`）。

`normalize_name` 不动。质量测试里 `node1.grok` 仍须通过。

## 11. B3 测试（`tests/test_federation.py`，新文件）

两个临时 home，一个临时共享目录，不听端口。

- 未配置 `node`：退出码 2，共享目录与库都不变。
- 两节点互发：收方 inbox 是原 id / 主题 / 正文；发方本地 `from` 仍是裸名。
- 同一文件导入两次：一行。
- 同 id 不同正文：不覆盖。
- 先写半截、再原子换成完整文件：只导入一次。
- 冲突副本文件名不导入。
- 回复先于父信：两条都在，`thread_id` 与 `reply_to` 还在。
- `*` 不产生 outbox 文件；只有裸名收件人也不产生。
- 混合收件人：本地有投递，outbox 恰好一个文件，`to` 里两边的线上名字都在。
- 签收后 `once`：发信方对应投递变为 `read`；再跑一次计数不变。
- 无 workspace 的信不导出。
- 不导入自己的 outbox。
- 既有 `tests/test_infocenter.py` 16 项不改、不回归。质量测试一并跑。

## 12. 拍板结果

coordinator 终签照准（`m1a138f175e4072b7d`）：

1. 未配置 `node` 时 `ic sync` **拒绝**（退出码 2），而不是成功空操作。其他命令没有联邦副作用。
2. 回执目录用 `outbox/<节点>/receipts/`，一节点只写自己的目录。
3. 导入把 `workspace` 改写成本地 `--dir`。
4. 幂等比较不可变字段；同 id 冲突留先到的。
5. 已配置 `node` 时，`*` 跳过 wire name。实现是给 cursor 的 §10 diff。
6. v1 不 GC、不加密、不在 crew 里调 sync。`crew.py` 虽已交还，这次冻结仍不包含它。
