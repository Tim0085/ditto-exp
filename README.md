# ditto-exp

[Ditto](https://github.com/CodeSoul-co/Ditto) 智能体框架的实验代码。这里只放代码：实验结果、数据、密钥都不在仓库里。

| 目录 | 实验 |
| --- | --- |
| [`worker-isolation/`](worker-isolation/) | 实验一 2.0：把重的 Worker 隔离到独立进程（Ditto 官方 HTTP 传输），和全部放在一个进程的 Monolithic、不经过 Ditto 运行时的 Direct 比吞吐、每题用时和正确率。LongMemEval_S 全部 500 题，两个场景：CONTEXT 与 MEMORY 协作（S4）、Ditto 官方 rag-qa（检索交给 RETRIEVAL Worker）。 |

Ditto 版本：npm 包 `@codesoul-co/ditto` 0.1.5、`@codesoul-co/ditto-retrieval` 0.1.0；官方示例代码固定在上游提交 `9cde78b`。
上游没有声明许可证，所以仓库里不放官方文件的副本：`worker-isolation/scripts/fetch-official.sh` 从上游同一个提交取，再套上本实验的几处小改动（`worker-isolation/patches/`），逐个核对 SHA-256。
