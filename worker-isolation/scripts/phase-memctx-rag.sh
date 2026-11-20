#!/usr/bin/env bash
# 2026-11-19 晚的离线测量（假模型，不联网、不花钱）。一次只跑一个容器，跑的时候电脑保持空闲。
#
#   1. docs\23：S4 里 MEMORY 和 CONTEXT 放进同一个独立进程（split-memctx）。2 核、并发 37，
#      Monolithic → 放进同一进程 → 两个都拆 → 两个都拆 → 放进同一进程 → Monolithic（“两个都拆”是同一时段的参照）。
#      结果 results\memctx-memctx-20261119\
#   2. docs\24 阶段一：rag 单题测量。Monolithic，1 核，并发 1，模型延迟 0，前 50 题；会写记忆库，所以每遍一个新容器，共 3 遍。
#      结果 results\rag-probe-20261119\raw
#   3. docs\24 阶段二：推算（estimate.py）。结果 results\rag-probe-20261119\
#   4. docs\24 阶段三：rag 端到端。2 核，并发 8、16、37；Monolithic、拆 INTERACTION+RETRIEVAL、再拆 MEMORY；
#      并发 16、37 再按相反顺序跑第 2 轮。结果 results\rag-e2e-20261119\
# 都用新的结果目录和组名（沿用旧目录会把旧格子当成“已有”跳过）。中途断了再跑一次，已有的格子跳过。
#
#   bash scripts/phase-memctx-rag.sh
set -euo pipefail
cd "$(dirname "$0")/.."
SEC=$(cd .. && (pwd -W 2>/dev/null || pwd))   # 实验根目录：本目录的上一级（上一行已 cd 到本目录）
L=/data/longmemeval-s/derived/memctx-lme-first200.jsonl
R=/data/longmemeval-s/derived/rag-lme-first200.jsonl
RAG="--scenario rag --db /store/rag-first200.sqlite --tasks $R --context-store redis"
say() { echo "[$(date +%H:%M:%S)] $*"; }
has() { MSYS_NO_PATHCONV=1 docker run --rm --network none --mount "type=volume,src=sec1new-memctx-store,dst=/store,readonly" --entrypoint /bin/sh 3513d314a207 -c "test -s /store/$1"; }

say "起网络、假模型、Redis"; bash scripts/run.sh infra-up
has users-100.sqlite || { say "建 S4 记忆库"; bash scripts/run.sh build-store > /dev/null; }
has rag-first200.sqlite || { say "建 rag 内部知识库"; bash scripts/run.sh build-rag-store > /dev/null; }

say "1. S4：MEMORY、CONTEXT 放进同一进程"
mkdir -p "$SEC/results/memctx-memctx-20261119/raw"
RAW_DIR=$SEC/results/memctx-memctx-20261119/raw CONC=37 REPEAT_AT=37 \
  bash scripts/phase3.sh s4memctx-u100 "--scenario s4 --db /store/users-100.sqlite --tasks $L" mono,split-memctx,split-both 4,6
python -I src/export.py "$SEC/results/memctx-memctx-20261119/raw" "$SEC/results/memctx-memctx-20261119" > /dev/null

say "2. rag 阶段一"
export RAW_DIR=$SEC/results/rag-probe-20261119/raw
mkdir -p "$RAW_DIR"
for p in 1 2 3; do
  name=rag-lme.p$p
  if [ -f "$RAW_DIR/$name.json" ]; then say "已有 $name"; continue; fi
  say "$name"
  # shellcheck disable=SC2086
  bash scripts/run.sh probe "$name" 4 $RAG --distinct 50 --count 50 --warmup 2 --reps 1 > "$RAW_DIR/$name.log" 2>&1 || { tail -3 "$RAW_DIR/$name.log"; exit 1; }
  tail -1 "$RAW_DIR/$name.log"
done
unset RAW_DIR

say "3. rag 阶段二：推算"
python -I src/estimate.py "$SEC/results/rag-probe-20261119/raw" "$SEC/results/rag-probe-20261119" > /dev/null

say "4. rag 阶段三"
mkdir -p "$SEC/results/rag-e2e-20261119/raw"
RAW_DIR=$SEC/results/rag-e2e-20261119/raw CONC="8 16 37" REPEAT_AT="16 37" \
  bash scripts/phase3.sh rag-u200 "$RAG" mono,split-interaction,split-interaction-memory 4,6
python -I src/export.py "$SEC/results/rag-e2e-20261119/raw" "$SEC/results/rag-e2e-20261119" > /dev/null

bash scripts/run.sh infra-down
say "全部完成"
