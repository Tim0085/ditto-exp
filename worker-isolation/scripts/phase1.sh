#!/usr/bin/env bash
# 阶段一：单题测量。docs\16 第 4 节阶段一；结果写 docs\17。
#
# 每个配置一个 Monolithic 容器，绑 1 个核（4 号），并发 1，模型延迟 0；50 题（固定顺序的前 50 个用户 / 前 50 篇文档），
# 先热身 2 题。**一次只跑一个容器**：第一次（11-19 02:00）6 个容器同时跑，结果同一配置三遍之间差到 2 倍以上，
# 单独重跑的对照稳定（S1 问答 100 个用户：同时跑 296/148/148 毫秒，单独跑 115/115/115），那批数据只作为“同时跑的干扰”的证据，
# 放在 raw-parallel\，不用于推算。
#
# - 只读或不写库的配置（S1 问答、S2、S3）：一个容器计时 3 遍，每题取中位的那一遍，再单独一遍量跨进程字节。
# - 会往库里写的配置（S1 写入、S4）：每遍一个新容器（库从同一份副本开始），共 3 个容器，每个计时 1 遍 + 量字节 1 遍。
#   同一容器里连跑 3 遍时，库每遍长大，S4 的检索一遍比一遍慢（48→71→95 毫秒）。
set -euo pipefail
cd "$(dirname "$0")/.."
export RAW_DIR=$(cd .. && (pwd -W 2>/dev/null || pwd))/results/memctx-probe-20261119/raw   # 实验根目录下（上一行已 cd 到本目录）
L=/data/longmemeval-s/derived/memctx-lme-first200.jsonl
S=/data/longbench-v2/derived/memctx-lb-short.jsonl
M=/data/longbench-v2/derived/memctx-lb-medium.jsonl
CORE=${CORE:-4}
mkdir -p "$RAW_DIR"

one() { # one <名字> <参数...>
  local name=$1; shift
  [ -f "$RAW_DIR/$name.json" ] && { echo "已有 $name"; return 0; }
  echo "$(date +%H:%M:%S) $name"
  bash scripts/run.sh probe "$name" "$CORE" "$@" --count 50 --warmup 2 > "$RAW_DIR/$name.log" 2>&1 || { echo "失败 $name"; tail -3 "$RAW_DIR/$name.log"; return 1; }
  tail -1 "$RAW_DIR/$name.log"
}
fresh() { # fresh <名字> <参数...>：每遍一个新容器
  local name=$1; shift
  for p in 1 2 3; do one "$name.p$p" "$@" --reps 1; done
}

for u in 50 100 250 500; do one "s1-qa-u$u" --scenario s1-qa --db /store/users-$u.sqlite --tasks $L --distinct 50 --reps 3; done
for u in 100 500; do fresh "s1-write-u$u" --scenario s1-write --db /store/users-$u.sqlite --tasks $L --distinct 50; done
one s2-short-default  --scenario s2 --tasks $S --distinct 50 --reps 3
one s2-short-bm25     --scenario s2 --select bm25 --tasks $S --distinct 50 --reps 3
one s2-medium-default --scenario s2 --tasks $M --distinct 50 --reps 3
one s2-medium-bm25    --scenario s2 --select bm25 --tasks $M --distinct 50 --reps 3
one s2-medium-redis   --scenario s2 --context-store redis --tasks $M --distinct 50 --reps 3
one s3-compress       --scenario s3 --tasks $L --distinct 50 --reps 3
one s3-full           --scenario s3-full --tasks $L --distinct 50 --reps 3
one s3-compress-redis --scenario s3 --context-store redis --tasks $L --distinct 50 --reps 3
fresh s4-u100 --scenario s4 --db /store/users-100.sqlite --tasks $L --distinct 50
fresh s4-u500 --scenario s4 --db /store/users-500.sqlite --tasks $L --distinct 50
echo "phase1 done"
