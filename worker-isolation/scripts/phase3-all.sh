#!/usr/bin/env bash
# 阶段三的全部组，按阶段二的结果（docs\17）选配置。一次只跑一格。
#   S4（唯一达到 10% 门槛）：2 核下四种布局；3 核下 Monolithic 和两个都拆。
#   S1、S2、S3（都不到门槛）：按方案“也挑最好的一个跑端到端，用实测确认”。
#     S1 问答 100 个用户（推算最好）；S3 不压缩（推算最好）；S2 各配置推算都在 ±0.2% 以内，选更重的中段默认挑选。
set -euo pipefail
cd "$(dirname "$0")/.."
L=/data/longmemeval-s/derived/memctx-lme-first200.jsonl
M=/data/longbench-v2/derived/memctx-lb-medium.jsonl
bash scripts/phase3.sh s4-u100 "--scenario s4 --db /store/users-100.sqlite --tasks $L" mono,split-memory,split-context,split-both 4,6
bash scripts/phase3.sh s4-u100 "--scenario s4 --db /store/users-100.sqlite --tasks $L" mono,split-both 4,6,8
bash scripts/phase3.sh s1-qa-u100 "--scenario s1-qa --db /store/users-100.sqlite --distinct 100 --tasks $L" mono,split-memory 4,6
bash scripts/phase3.sh s3-full "--scenario s3-full --tasks $L" mono,split-context 4,6
bash scripts/phase3.sh s2-medium-default "--scenario s2 --tasks $M" mono,split-context 4,6
echo "phase3 done"
