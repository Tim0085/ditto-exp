#!/usr/bin/env bash
# 阶段三：端到端离线运行。docs\16 第 4 节阶段三；配置由阶段二挑出（docs\17），结果写 docs\18。
#
#   bash scripts/phase3.sh <组名> <场景参数> <布局,布局,...> [核数集合，默认 2核=4,6]
#
# 一组 = 一个场景的一个配置。对每个并发（8、16、37），各布局紧挨着跑（第 1 轮）；并发 16 再按相反顺序跑第 2 轮
# （A B B A，抵消笔记本速度的漂移）。每格 200 题、前 20 题热身；假模型延迟：理解问题中位 1 秒、回答中位 2 秒，对数正态 σ=0.5，
# 同组同轮各布局用同一个种子（同一题同一步等一样久）。
set -euo pipefail
cd "$(dirname "$0")/.."
export RAW_DIR=${RAW_DIR:-$(cd .. && (pwd -W 2>/dev/null || pwd))/results/memctx-e2e-20261119/raw}   # 实验根目录下（上一行已 cd 到本目录）
group=${1:?组名}; args=${2:?场景参数}; IFS=, read -r -a layouts <<< "${3:?布局}"; cores=${4:-4,6}
k=$(( $(tr -cd ',' <<< "$cores" | wc -c) + 1 ))
CONC=${CONC:-8 16 37}
REPEAT_AT=${REPEAT_AT:-16}
mkdir -p "$RAW_DIR"

cell() { # cell <布局> <并发> <轮>
  local layout=$1 c=$2 r=$3 name="$group-k$k-c$2-$1-r$3"
  [ -f "$RAW_DIR/$name.json" ] && { echo "已有 $name，跳过"; return 0; }
  echo "$(date +%H:%M:%S) $name"
  # shellcheck disable=SC2086
  bash scripts/run.sh e2e "$name" "$layout" "$cores" $args --concurrency "$c" --count 200 --warmup 20 \
    --keyword-ms 1000 --answer-ms 2000 --sigma 0.5 --seed "$group-r$r" --label "$group|r$r" > "$RAW_DIR/$name.log" 2>&1 \
    || { echo "失败 $name"; tail -5 "$RAW_DIR/$name.log"; }
  tail -1 "$RAW_DIR/$name.log"
}
for c in $CONC; do
  for layout in "${layouts[@]}"; do cell "$layout" "$c" 1; done
  # REPEAT_AT 可以写几个并发（空格分开），每个都按相反顺序再跑第 2 轮（docs\24：并发 16、37 都做 A B B A）。
  if [[ " $REPEAT_AT " == *" $c "* ]]; then
    for ((i=${#layouts[@]}-1; i>=0; i--)); do cell "${layouts[$i]}" "$c" 2; done
  fi
done
