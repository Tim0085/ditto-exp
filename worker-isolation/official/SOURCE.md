# official\：Ditto 官方文件的原样副本

来源：Ditto 上游 https://github.com/CodeSoul-co/Ditto 的提交 `9cde78b`（9cde78bbac882cc0467bfc03437c15698d99cc3a；本机取自 `sec1\ref\Ditto-upstream` 克隆）。公开仓库 ditto-exp 里不放这些副本（上游没有声明许可证），用 `scripts\fetch-official.sh` 从上游取，`patches\SHA256SUMS` 核对。2026-11-19 复制，内容一字未改，保持上游的相对路径。`docs\12` 第 1 节核对过：rag 相关文件和 v0.1.5（提交 `d95ded5`）逐字节相同。

`examples/_shared/tools/storage/dependencies/node_modules/` 是按官方锁文件 `npm ci --ignore-scripts` 装的（`redis` 6.2.1、`pg` 8.16.3），npm 缓存放在 `..\_tmp\npm-cache`。`redis-context.ts` 按官方写法从这里加载 `redis`。

| 文件 | SHA256 前 16 位 |
| --- | --- |
| `ditto.yaml` | `7d0d42e65eac7ef8` |
| `docs/worker-communication.zh-CN.md` | `6963d5f882b91b78` |
| `docs/worker-api/context.zh-CN.md` | `423cf2cbb3ba63b4` |
| `docs/worker-api/memory.zh-CN.md` | `37747f4ca17fe414` |
| `examples/capabilities/context/shared.ts` | `2b2755a12c96ebc8` |
| `examples/capabilities/memory/shared.ts` | `7027c076471fd5ec` |
| `examples/patterns/rag-qa/index.ts` | `16207b73ae73993f` |
| `examples/_shared/tools/context/adapters.ts` | `b6ae57ee2e5bcea7` |
| `examples/_shared/tools/context/domain.ts` | `a2f535f390bf48df` |
| `examples/_shared/tools/memory/storage.ts` | `c842f4366311d4ef` |
| `examples/_shared/tools/rag/adapters.ts` | `2b0cbf0ed80c8524` |
| `examples/_shared/tools/rag/domain.ts` | `5a343e6f3042677e` |
| `examples/_shared/tools/evidence.ts`（2026-11-19 晚补，rag 场景要用） | `4bde180250b76c98` |
| `examples/_shared/tools/execution/files.ts`（同上） | `1c9debae77e7b0b3` |
| `examples/_shared/tools/storage/compose.memory.yaml` | `71be81a4edc4adb7` |
| `examples/_shared/tools/storage/qdrant-memory.ts` | `3b470b66bbc09d91` |
| `examples/_shared/tools/storage/redis-context.ts` | `dd5c1dfb1630dae1` |
| `examples/_shared/tools/storage/scoped-memory.ts` | `73973087e5bb0e37` |
| `examples/_shared/tools/storage/sql-memory.ts` | `9ebfd4eadd82b056` |
| `examples/_shared/tools/storage/sqlite-memory.ts` | `b8f8cd8767dfe94f` |
| `examples/_shared/tools/storage/workers.ts` | `5d396416ccf7c239` |
| `examples/_shared/tools/storage/dependencies/package.json` | `9415f916eb9cf9e2` |
| `examples/_shared/tools/storage/dependencies/package-lock.json` | `c9b26c0b833f5dbe` |

程序直接引用的：`scoped-memory.ts`、`redis-context.ts`、`ditto.yaml`（读入后只改 `runtime.timeoutMs`）。rag 场景（docs\24）11-19 原样引用这里的 `patterns/rag-qa/index.ts`（流程）、`rag/adapters.ts`、`rag/domain.ts`、`evidence.ts`、`execution/files.ts`（切法 (b)，官方上限一条都没放开）；2026-11-20 起改引 `..\rag-a\` 下的副本（切法 (a)：只有 `domain.ts` 放开两个数字，见那里的 CHANGES.md），这里的 5 个文件保留作原样参照，测试会逐字比对。切法 (b) 的数据校验（`src\rag-sources.ts` 的 `buildRagTask`）仍用这里的 `domain.ts`。`sqlite-memory.ts`、`sql-memory.ts` 用的是 `..\relaxed\` 下的副本（见那里的 CHANGES.md）。其余文件只用来核对。
