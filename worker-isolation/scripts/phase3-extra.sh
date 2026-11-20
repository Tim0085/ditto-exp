#!/usr/bin/env bash
# 阶段三的补充检查（方案第 4 节“时间不够时按这个顺序砍”里的第 4、5 项，加 S4 500 个用户）。只在并发 16、跑一轮。
# 都只比 Monolithic 和拆 MEMORY（S4 推算最好的拆法）。
set -euo pipefail
cd "$(dirname "$0")/.."
L=/data/longmemeval-s/derived/memctx-lme-first200.jsonl
S4="--scenario s4 --db /store/users-100.sqlite --tasks $L"
export CONC=16 REPEAT_AT=none
bash scripts/phase3.sh s4-u100 "$S4" mono,split-memory 4                 # 1 核
bash scripts/phase3.sh s4-u100 "$S4" mono,split-memory 4,6,8,10          # 4 核
bash scripts/phase3.sh s4-u100-fast "$S4 --keyword-ms 500 --answer-ms 1000" mono,split-memory 4,6    # 模型快一倍
bash scripts/phase3.sh s4-u100-slow "$S4 --keyword-ms 2000 --answer-ms 4000" mono,split-memory 4,6   # 模型慢一倍
bash scripts/phase3.sh s4-u500 "--scenario s4 --db /store/users-500.sqlite --tasks $L" mono,split-memory 4,6
echo "phase3 extra done"
