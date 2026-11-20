# relaxed\：对官方文件的改动

## storage/sql-memory.ts（本轮新加，只改一处）

| 官方 | 现在 |
| --- | --- |
| 第 1 行 `import { randomUUID } from "node:crypto";` | `import { createHash, randomUUID } ...`，并加入 `memoryPointId(key)`：和官方 `qdrant-memory.ts` 第 19–22 行**逐字相同** |
| 第 66 行 `write` 里 `id: randomUUID()` | `id: memory.key ? memoryPointId(memory.key) : randomUUID()` |

**为什么要改**（2026-11-19）：官方 SQLite 记忆每写一条就随机生成记录编号，检索又按编号排序取前 10 条（`ORDER BY id LIMIT ?`）。于是：

- 同一份数据建两次库，检索到的记录不同；
- S4 在对话中途写记忆，两种布局各写一遍，记录编号不同，后面检索到的记忆、发给模型的内容也就不同。

这会破坏“两种布局做的事完全一样”。官方 Qdrant 适配器本来就用 key 算记录编号（`memoryPointId`），这里照搬到 SQLite 适配器。没有 key 时仍然随机；查询、检索、写入的 SQL 和事务都没变。

改完后实测：S1、S2（默认、BM25）、S4（拆 MEMORY、拆 CONTEXT、两个都拆）各 10 题，拆开与 Monolithic 发给模型的请求体 SHA-256 全部相同（各 20/20）。

## storage/sqlite-memory.ts

原样复制，一字未改（放在这里只是为了让它引用同目录下改过的 `sql-memory.ts`）。

## rag/domain.ts、evidence.ts

从 `..\..\02_load_probe\relaxed\` 原样复制（只放开示例上限的版本，改动见那里的 CHANGES.md）。本轮只用其中的切块函数 `chunks()`（切 LongBench 文档）和分词函数 `tokens()`（BM25）。
