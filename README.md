# ditto-exp

[Ditto](https://github.com/CodeSoul-co/Ditto) 智能体框架的实验代码。这里只放代码：实验结果、数据、密钥都不在仓库里。

| 目录 | 实验 |
| --- | --- |
| [`worker-isolation/`](worker-isolation/) | 实验一 2.0：把重的 Worker 隔离到独立进程（Ditto 官方 HTTP 传输），和全部放在一个进程的 Monolithic、不经过 Ditto 运行时的 Direct 比吞吐、每题用时和正确率。LongMemEval_S 全部 500 题，两个场景：CONTEXT 与 MEMORY 协作（S4）、Ditto 官方 rag-qa（检索交给 RETRIEVAL Worker）。 |

## worker-isolation 的几轮实验

按先后顺序，命令都在 `worker-isolation/` 下运行：

| 实验 | 内容 | 运行 | 出结论 |
| --- | --- | --- | --- |
| 种子对照 | rag-qa 换成种子 42 的题序，8 格 × 200 题，和原种子那一轮比 | `bash scripts/real.sh run rag-s42` | `python -I report/seed_compare.py` |
| 离线并发扫描 | 模拟模型，并发 2–64，选定正式实验的并发 | `bash scripts/sweep.sh run` | `bash scripts/sweep.sh report` |
| 低负载对照 | S4，真实模型，并发 2，4 种布局 × 2 轮，每格 200 题 | `bash scripts/real.sh run low-s42` | `python -I report/low_criteria.py` |
| 全量实验 | 并发 36，每格 500 题：S4 6 种布局 × 2 轮，rag-qa 3 种布局 × 2 轮 | `bash scripts/real.sh run full-s4`、`run full-rag` | `python -I report/full_criteria.py <结果目录>` |
| 阶段汇报 | 以全量实验为主的结果表格和图 | — | `python -I report/build_report_1121.py <实验根目录> <汇报目录>` |

- 真实模型的运行先用 `bash scripts/real.sh rehearse <计划>` 做离线演练：模拟模型（`src/fake-model.ts`），不联网、不花钱。
- 更早的几轮（种子 20261119，每格 200 题）是 `scripts/real.sh` 里的 `mem`、`ctx`、`ctx2`、`rag`。
- 模型：DeepSeek deepseek-flash（关闭思考，temperature 0）。判分：GPT-4o（gpt-4o-2024-08-06），LongMemEval 官方判分提示词。
- 准备、运行和检验见 [`worker-isolation/README.md`](worker-isolation/README.md)，程序结构见 [`worker-isolation/代码导读.md`](worker-isolation/代码导读.md)。

## Ditto 版本

npm 包 `@codesoul-co/ditto` 0.1.5、`@codesoul-co/ditto-retrieval` 0.1.0；官方示例代码固定在上游提交 `9cde78b`。
上游没有声明许可证，所以仓库里不放官方文件的副本：`worker-isolation/scripts/fetch-official.sh` 从上游同一个提交取，再套上本实验的几处小改动（`worker-isolation/patches/`），逐个核对 SHA-256。
