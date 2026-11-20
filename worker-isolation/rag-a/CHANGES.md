# rag-a\：切法 (a) 用的官方 rag-qa 副本

来源：`..\official\`（Ditto 上游提交 `9cde78b` 的原样副本，见那里的 SOURCE.md）。2026-11-20 复制，保持上游的相对路径。切法 (a)：一段对话一个来源。

| 文件 | 和官方比 | SHA256 前 16 位 |
| --- | --- | --- |
| `examples/patterns/rag-qa/index.ts` | 一字未改 | `16207b73ae73993f` |
| `examples/_shared/tools/rag/adapters.ts` | 一字未改 | `2b0cbf0ed80c8524` |
| `examples/_shared/tools/evidence.ts` | 一字未改 | `4bde180250b76c98` |
| `examples/_shared/tools/execution/files.ts` | 一字未改 | `1c9debae77e7b0b3` |
| `examples/_shared/tools/rag/domain.ts` | 改了两个数字（下表） | `c47a207fb4940ed0`（官方 `5a343e6f3042677e`） |

| 位置 | 官方 | 现在 | 为什么 |
| --- | --- | --- | --- |
| 第 21 行 `request()`：一次请求最多几个来源 | 20 | 100 | 一段对话一个来源；前 200 题每题 47 段（中位）、最多 61 段 |
| 第 123 行 `chunks()`：一个来源最多几块 | 200 | 400 | 前 200 题的 9524 段对话里有 2 段超过 200 块，最多 301 块 |

其余上限一条不变：来源清单 ≤100 个、每个来源 ≤10 万字、每行 ≤1600 字、问题 ≤1200 字、检索池 ≤45000 字、证据 ≤10000 字，以及每一步模型输出的格式检查。超过 1600 字的行照官方报错的要求（“Normalize long lines before ingestion”）在准备数据时折行，只插入换行、不改文字（`src\rag-sources.ts` 的 `wrapLongLines`）。

`index.ts`、`adapters.ts` 一字未改，复制过来只是为了让它们引用同目录下的 `domain.ts`。引用这里的程序：`src\rag.ts`、`src\run.ts`、`src\rag-sources.ts`。
