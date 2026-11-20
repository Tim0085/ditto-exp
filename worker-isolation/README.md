# worker-isolation：Worker 的隔离与合并（实验一 2.0）

问题：Ditto 里把一个或几个重的 Worker 隔离到独立进程（官方 HTTP 传输），和全部放在一个进程的 Monolithic 比，吞吐、每题用时、正确率怎么变。每类 Worker 只有 1 个实例，不复制。另有两个对照：

- **合并对照**：MEMORY 和 CONTEXT 合进同一个独立进程。隔离的收益如果来自“每个重的 Worker 各占一条主线程”，合并以后收益应当消失。
- **Direct**：不经过 Ditto 的 Runtime、Graph、Loop 和 Worker 调度，同样的步骤写成普通函数在一个进程里直接调用（`src/direct.ts`），底层（CONTEXT、MEMORY、INFER 的处理函数，官方 SQLite 记忆、RagAdapters、BM25）和 Monolithic 相同，发给模型的请求逐字相同。Monolithic 对 Direct 是框架本身的代价。

数据：LongMemEval_S 全部 500 题（按固定种子打乱的顺序）。两个场景：

| 场景 | 流程 | 布局 |
| --- | --- | --- |
| S4 | CONTEXT 与 MEMORY 协作（Ditto 文档的流程）：每段历史对话追加进 CONTEXT → 挑出用户消息写进 MEMORY → 压回预算；提问时模型给检索词 → 检索 MEMORY → 映射进 CONTEXT → 挑选 → 模型回答（LongMemEval 官方回答模板） | Monolithic、Direct、隔离 MEMORY、隔离 CONTEXT、两个都隔离、合并 |
| rag | Ditto 官方 rag-qa 示例（一段对话一个来源，只放开两个上限）：理解问题 → 建索引、核对来源 → 检索（RETRIEVAL Worker，BM25）→ 挑证据 → 回答 → 核对 | Monolithic、Direct、隔离 INTERACTION + RETRIEVAL |

模型：DeepSeek deepseek-flash（关闭思考，temperature 0），经本实验的出口转发（`src/egress.ts`：记账、限额、防缓存编号、完整记录）。判分：GPT-4o（gpt-4o-2024-08-06）按 LongMemEval 官方提示词。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/` | 全部程序。先看 [代码导读.md](代码导读.md) |
| `scripts/real.sh` | 真实模型运行的一键脚本：`check`、`run`、`judge`、`rehearse`（离线演练）、`down` |
| `scripts/run.sh` | 全部容器命令（网络、假模型、Redis、建库、单格运行） |
| `scripts/fetch-official.sh` | 从上游取 Ditto 官方文件（仓库里不放副本），套上 `patches/` 里的改动，逐个核对 SHA-256 |
| `scripts/sync-repo.sh` | 实验目录 → 本仓库的同步（只复制代码，同步后查敏感内容） |
| `tests/` | 测试（不联网、不碰 Docker）：Direct 和 Monolithic 请求逐字相同、出口记账和重试、rag 的数据和失败分类等 |
| `report/` | 阶段汇报用的结果表格的生成程序 |
| `patches/` | 本实验对官方文件的改动（`MANIFEST` 列出每个文件从上游哪里来、套哪个补丁；`SHA256SUMS` 是实验时用的文件的摘要） |
| `official/SOURCE.md`、`rag-a/CHANGES.md`、`relaxed/CHANGES.md` | 官方文件的来源和改动说明 |

## 准备

本实验在 Windows 11 + Docker Desktop + Git Bash 上跑。脚本按自己的位置找其他目录：本目录的上一级是实验根目录，里面要有 `bench_data/`（数据）、`results/`（结果）、`_tmp/`（临时文件）。

1. Node 24.14（本机和容器里），Python 3.12（判分、汇总；汇总要 openpyxl）。容器镜像：任何带 `/usr/local/bin/node`（24.14）的镜像，把 `scripts/run.sh`、`scripts/real.sh` 里的 `IMAGE` 换成它；rag 另要 `redis:7-alpine`。
2. 依赖：`npm install`（`@codesoul-co/ditto` 0.1.5、`@codesoul-co/ditto-retrieval` 0.1.0、`yaml` 2.9.1；类型检查用 `typescript` 5.9.3、`@types/node`）。
3. 官方文件：`bash scripts/fetch-official.sh`。
4. 数据：LongMemEval_S（`longmemeval_s_cleaned.json`）放进 `../bench_data/longmemeval-s/`，然后依次跑 `src/prepare.ts`、`src/sample.ts`、`src/sample500.ts`、`src/prepare-rag.ts --cut a --prefix rag-a-all500`（各文件开头写了参数）。
5. 本机配置：把 `local.env.example` 复制成 `local.env` 填好（DeepSeek 密钥开头几位、密钥文件和 GPT-4o 文件的位置等；`local.env` 不进 git）。

## 运行

```bash
bash scripts/real.sh check full-s4      # 开跑前检查，不花钱
bash scripts/real.sh rehearse full-s4   # 离线演练：假模型，不联网、不花钱
bash scripts/real.sh run full-s4        # S4 全量：12 格 × 500 题
bash scripts/real.sh run full-rag       # rag 全量：6 格 × 500 题
bash scripts/real.sh down               # 中途停
```

中途断了再跑 `run` 就接着跑（已验收的格子跳过）。每轮的结果目录里有：每格的原始记录（每题、每个模型请求、每次调用的用时、每 5 秒的资源读数、rag 每题的完整报告）、出口的账本和完整请求、回复、判分原文、汇总表和详细分析、运行环境（`run-info.json`）。

## 检验

```bash
node ../01_bfcl_multiturn/node_modules/typescript/bin/tsc -p tsconfig.json   # 实验目录里的写法；装了 typescript 的话用 npx tsc -p tsconfig.json
node --test tests/*.test.ts
```
