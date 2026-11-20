#!/usr/bin/env bash
# 2026-11-20：rag-qa 切法 (a)（一段对话一个来源）的离线测量。假模型，不联网、不花钱；一次只跑一个容器。
# 和 phase-memctx-rag.sh 的第 2–4 步做法相同，只换数据（rag-a-first200）和结果目录：
#
#   1. 阶段一（单题测量）：Monolithic，1 核，并发 1，模型延迟 0，前 50 题；会写记忆库，所以每遍一个新容器，共 3 遍。
#      结果 results\rag-a-probe-20261120\raw
#   2. 阶段二（推算）：estimate.py。结果 results\rag-a-probe-20261120\
#   3. 阶段三（端到端）：2 核，并发 8、16、37；Monolithic、拆 INTERACTION+RETRIEVAL、再拆 MEMORY；
#      并发 16、37 再按相反顺序跑第 2 轮（A B B A）。结果 results\rag-a-e2e-20261120\
# 新的结果目录和组名，不和切法 (b) 的混。中途断了再跑一次，已有的格子跳过。
#
#   bash scripts/phase-rag-a.sh [probe|e2e|all]     默认 all
set -euo pipefail
cd "$(dirname "$0")/.."
SEC=$(cd .. && (pwd -W 2>/dev/null || pwd))   # 实验根目录：本目录的上一级（上一行已 cd 到本目录）
R=/data/longmemeval-s/derived/rag-a-first200.jsonl
RAG="--scenario rag --db /store/rag-a-first200.sqlite --tasks $R --context-store redis"
PROBE_DIR=$SEC/results/rag-a-probe-20261120 E2E_DIR=$SEC/results/rag-a-e2e-20261120
WHAT=${1:-all}
say() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] 停下：$*" >&2; exit 1; }
has() { MSYS_NO_PATHCONV=1 docker run --rm --network none --mount "type=volume,src=sec1new-memctx-store,dst=/store,readonly" --entrypoint /bin/sh 3513d314a207 -c "test -s /store/$1"; }

# 和 real.sh 共用一把运行锁：两边用同名的假模型、Redis 容器，同时跑会互相删掉对方的。写法同 real.sh 的 take_lock。
LOCK=$SEC/_tmp/real-sh.lock
mkdir -p "$SEC/_tmp"
if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then die "real.sh 或另一项测量正在跑（$(cat "$LOCK/what" 2>/dev/null)，进程 $pid）"; fi
  if [ -z "$pid" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then die "另一项运行正在启动（运行锁刚建好）"; fi
  rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || die "拿不到运行锁 $LOCK"
fi
echo $$ > "$LOCK/pid"; echo "phase-rag-a.sh $WHAT，$(date '+%F %T') 开始" > "$LOCK/what"
trap 'if [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$LOCK"; fi' EXIT
[ -z "$(docker ps -q --filter name=sec1new-memctx-)" ] || die "还有 sec1new-memctx- 开头的容器在跑：$(docker ps --filter name=sec1new-memctx- --format '{{.Names}}' | tr '\n' ' ')"

say "起网络、假模型、Redis"; bash scripts/run.sh infra-up
has rag-a-first200.sqlite || { say "建切法 (a) 的内部知识库"; bash scripts/run.sh build-rag-store a > /dev/null; }

if [ "$WHAT" = all ] || [ "$WHAT" = probe ]; then
  say "1. 阶段一：单题测量"
  export RAW_DIR=$PROBE_DIR/raw
  mkdir -p "$RAW_DIR"
  for p in 1 2 3; do
    name=raga-lme.p$p
    if [ -f "$RAW_DIR/$name.json" ]; then say "已有 $name"; continue; fi
    say "$name"
    # shellcheck disable=SC2086
    bash scripts/run.sh probe "$name" 4 $RAG --distinct 50 --count 50 --warmup 2 --reps 1 > "$RAW_DIR/$name.log" 2>&1 || { tail -3 "$RAW_DIR/$name.log"; exit 1; }
    tail -1 "$RAW_DIR/$name.log"
  done
  unset RAW_DIR
  say "2. 阶段二：推算"
  python -I src/estimate.py "$PROBE_DIR/raw" "$PROBE_DIR" > /dev/null
fi

if [ "$WHAT" = all ] || [ "$WHAT" = e2e ]; then
  say "3. 阶段三：端到端"
  mkdir -p "$E2E_DIR/raw"
  RAW_DIR=$E2E_DIR/raw CONC="8 16 37" REPEAT_AT="16 37" \
    bash scripts/phase3.sh raga-u200 "$RAG" mono,split-interaction,split-interaction-memory 4,6
  python -I src/export.py "$E2E_DIR/raw" "$E2E_DIR" > /dev/null
fi

bash scripts/run.sh infra-down
# 核对：phase3.sh 某一格失败时只打印、不停，导出也只看现有的文件。这里点名核对每一格都在、0 失败、窗口有效。
python -I - "$PROBE_DIR/raw" "$E2E_DIR/raw" "$WHAT" <<'EOF' || die "核对没过：缺格、有失败或窗口无效，见上面"
import json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
probe, e2e, what = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
want = []
if what in ("all", "probe"):
    want += [probe / f"raga-lme.p{p}.json" for p in (1, 2, 3)]
if what in ("all", "e2e"):
    L = ["mono", "split-interaction", "split-interaction-memory"]
    want += [e2e / f"raga-u200-k2-c{c}-{l}-r{r}.json" for c, rounds in ((8, [1]), (16, [1, 2]), (37, [1, 2])) for r in rounds for l in L]
bad = []
for p in want:
    if not p.exists():
        bad.append(f"缺 {p.name}"); continue
    d = json.loads(p.read_text(encoding="utf-8"))
    if d["meta"]["failedTasks"]: bad.append(f"{p.stem} 失败 {d['meta']['failedTasks']} 题")
    if d["meta"]["mode"] == "e2e" and not d.get("window"): bad.append(f"{p.stem} 窗口无效")
print(f"    核对 {len(want)} 个结果：" + ("全部齐全、0 失败、窗口有效" if not bad else "；".join(bad)))
sys.exit(1 if bad else 0)
EOF
say "全部完成"
