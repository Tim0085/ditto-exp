#!/usr/bin/env bash
# 离线并发扫描（docs\34）：定全量实验的并发。全程假模型，接在出口后面，提示词、请求和真实运行相同；
# 不联网、不读真实密钥、不花钱。数据用原种子 20261119（derived\ 下的 500 题；S4 的记忆库是这个顺序的前 100 个用户）。
# 在 Git Bash 里跑：
#
#   bash scripts/sweep.sh run      全部：建库 → 新旧程序对照 → rag 扫描 → S4 扫描（需要时补测）→ 核对、汇总 → 收尾
#                                  中途断了再跑 run 就接着跑：已验收而且重新验收也过的格子跳过，作废的格子挪进 failed\ 以后重跑
#   bash scripts/sweep.sh report   只重新汇总（raw\ 里有没验收的结果文件时不汇总）
#   bash scripts/sweep.sh down     停：不再开始新的格子，删掉本实验的容器（正在跑的那一格作废，再跑 run 会重跑它）。
#                                  运行锁是 real.sh 的（真实运行在跑）就不动
#
# 每格：2 核（4、6），并发 c，跑 200 + c 题、热身 20 题，最后一题在第 200 次做完时派出。吞吐用整波平均判断（src\sweep_report.py）。
# 并发 2、4、8、16、24、36、37、48、64。8 起每个（布局，并发）跑两遍：第 1 遍并发从低到高，第 2 遍从高到低、各并发里布局的顺序也倒过来；
# 2、4 只跑第 1 遍。某个布局在 36 只比 48 或 64 慢一点（超出 3% 的部分在两遍的差别以内），就在 36、48、64 再补两遍。
# 每个场景开头、新旧程序对照之前，各跑一格不计入的热身（每次开跑的头一格偏慢）。假模型的延迟、种子固定，同一题同一步在各格等一样久。
# 新旧程序对照（docs\33 第 5 节）：按 11-20 凌晨 rag 那一轮的条件（原种子前 200 题、它的知识库、200 题、并发 37、Monolithic），
# 那一轮用的旧版程序（docs\31_附录\程序备份）和现在的程序按 旧 新 新 旧 各跑一格。四格要么都通过验收，要么整组重做。
# 结果在 results\conc-sweep-s20261119\：sweep-plan.json（计划；续跑时对不上就停）、rag\、s4\（raw\ 每格的原始记录、egress\ 出口的记录、
# warmup\ 热身格、cells.csv 等，和真实运行的结果目录一样；sweep-check.txt/.json 各格逐字相同的核对）、prog-check\（新旧程序对照）、
# 扫描结果.md（汇总和判断）、sweep-summary.json、curves.csv、扫描结果.xlsx；运行日志 sweep.log；作废的格子 failed-cells.txt。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."
say() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] 停下：$*" >&2; exit 1; }

# 不读 local.env 的其他内容（离线扫描用不着真实的配置），只取代码仓库的位置给 run_info.py 记提交；PROBE_DIR 只由新旧程序对照自己设。
unset PROBE_DIR
DITTO_EXP_REPO=${DITTO_EXP_REPO:-$(sed -n 's/^DITTO_EXP_REPO=//p' local.env 2>/dev/null | head -1 || true)}
export DITTO_EXP_REPO
# 密钥前缀一律用演练专用的假前缀，真的前缀不进扫描的文件
export DEEPSEEK_KEY_PREFIX=sk-rehearse MEMCTX_REAL=1
PROBE=$(pwd -W 2>/dev/null || pwd)
SEC=${PROBE%/*}
OFFICIAL_OUT=$SEC/results/conc-sweep-s20261119
OUT=${SWEEP_OUT:-$OFFICIAL_OUT}
# 冒烟试跑（docs\34 第 4 节）可以改设定，但必须用 SWEEP_OUT 另换结果目录，不能写进正式的扫描结果
if [ -n "${SWEEP_LOW+x}${SWEEP_LEVELS:-}${SWEEP_WINDOW_END:-}${SWEEP_PROG_COUNT:-}${SWEEP_SCENS:-}" ]; then
  [ -n "${SWEEP_OUT:-}" ] || die "改了扫描的设定（SWEEP_LOW、SWEEP_LEVELS、SWEEP_WINDOW_END、SWEEP_PROG_COUNT、SWEEP_SCENS）就要用 SWEEP_OUT 另换结果目录"
  same=$(python -I -c "import os,sys; a,b=(os.path.normcase(os.path.abspath(x)) for x in sys.argv[1:3]); print(int(a==b))" "$OUT" "$OFFICIAL_OUT")
  [ "$same" = 0 ] || die "改了扫描的设定就不能写进正式的结果目录 $OFFICIAL_OUT"
fi
IMAGE=3513d314a207
NET=sec1new-memctx-net VOL=sec1new-memctx-store
EGRESS=sec1new-memctx-egress FAKE=sec1new-memctx-fake REDIS=sec1new-memctx-redis
EGRESS_CORES=28-31 REDIS_CORES=26 CORES=4,6     # 出口、假模型、Redis 在别的核上，不占被测的 2 个核（和真实运行相同）
D=/data/longmemeval-s/derived
WARMUP=20 WINDOW_END=${SWEEP_WINDOW_END:-200}
LOW=${SWEEP_LOW-"2 4"} LEVELS=${SWEEP_LEVELS:-"8 16 24 36 37 48 64"}
SCENS=${SWEEP_SCENS:-"rag s4"}
# 新旧程序对照：11-20 凌晨 rag 那一轮的条件（原种子前 200 题、它的知识库、200 题、并发 37）
PROG_COUNT=${SWEEP_PROG_COUNT:-200} PROG_ORDER="old-p1 new-p1 new-p2 old-p2"
PROG_ARGS=(--scenario rag --db /store/rag-a-first200.sqlite --tasks "$D/rag-a-first200.jsonl" --context-store redis)
CELL_TIMEOUT=${CELL_TIMEOUT:-1800}               # 一格最多 30 分钟（最慢的 rag 并发 2 约 9 分钟），超时就作废这一格、接着跑
OLD_TAR=$SEC/docs/31_附录/程序备份/memctx-probe-before-full-20261120.tar.gz
OLD_DIR=$SEC/_tmp/scratch/prog-check-old
LOCK=$SEC/_tmp/real-sh.lock                     # 和 real.sh 共用运行锁：容器名、网络别名相同，不能同时跑

scen_conf() { # scen_conf s4|rag：这一场景的布局、场景参数、库、假模型延迟
  case "$1" in
    s4) LAYOUTS=(mono direct split-memory split-context split-both split-memctx)
        SCEN_ARGS=(--scenario s4 --db /store/users-100.sqlite --tasks "$D/memctx-lme-all500.jsonl")
        # 以前最慢的一轮真实 DeepSeek（11-19 下午 mem：检索词中位 755 毫秒、回答 1451 毫秒）；离散度 0.35（docs\34 2.1 节）
        FAKE_ARGS=(--keyword-ms 755 --answer-ms 1451 --sigma 0.35) ;;
    rag) LAYOUTS=(mono direct split-interaction)
        SCEN_ARGS=(--scenario rag --db /store/rag-a-all500.sqlite --tasks "$D/rag-a-all500.jsonl" --context-store redis)
        # rag 只测过一晚，按 S4 那一晚比最慢那轮快的比例放慢：理解问题、挑证据、核对共用 900 毫秒，回答 1740 毫秒；离散度 0.3
        FAKE_ARGS=(--keyword-ms 900 --answer-ms 1740 --sigma 0.3) ;;
    *) die "场景是 s4 或 rag，不是 $1" ;;
  esac
}

plan() { # plan <场景>：按顺序列出这一场景的格子，一行一格：遍 并发 布局
  local c k; scen_conf "$1"
  for c in $LOW $LEVELS; do for k in "${LAYOUTS[@]}"; do echo "p1 $c $k"; done; done
  for c in $(tr ' ' '\n' <<< "$LEVELS" | sort -rn); do for ((k=${#LAYOUTS[@]}-1; k>=0; k--)); do echo "p2 $c ${LAYOUTS[$k]}"; done; done
}

write_plan() { # 计划写进 sweep-plan.json；结果目录里已有一份而且不一样就停下（换了设定要换结果目录）
  local s args=()
  for s in $SCENS; do scen_conf "$s"; args+=("$s" "${LAYOUTS[*]}" "${FAKE_ARGS[*]}" "${SCEN_ARGS[*]}"); done
  python -I - "$OUT/sweep-plan.json" "$WINDOW_END" "$WARMUP" "$LOW" "$LEVELS" "$CORES" "$PROG_COUNT" "$PROG_ORDER" "${PROG_ARGS[*]}" "${args[@]}" <<'EOF'
import json, sys
from pathlib import Path
f, end, warm, low, levels, cores, pcount, porder, pargs, *rest = sys.argv[1:]
scens = {rest[i]: {"layouts": rest[i + 1].split(), "fake": rest[i + 2], "args": rest[i + 3]} for i in range(0, len(rest), 4)}
plan = {"window_end": int(end), "warmup": int(warm), "low": [int(x) for x in low.split()], "levels": [int(x) for x in levels.split()],
        "cores": cores, "scenarios": scens,
        "prog_check": ({"count": int(pcount), "order": porder.split(), "args": pargs,
                        "desc": f"rag Monolithic、并发 37、原种子前 200 题、{pcount} 题"} if "rag" in scens else None)}
p = Path(f)
if p.exists():
    old = json.loads(p.read_text(encoding="utf-8"))
    if old != plan:
        print("这个结果目录是按另一份计划跑的（sweep-plan.json 对不上）：换设定就换结果目录（SWEEP_OUT）", file=sys.stderr)
        print("原来的：" + json.dumps(old, ensure_ascii=False), file=sys.stderr)
        print("这次的：" + json.dumps(plan, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)
else:
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
EOF
}

# ---------------------------------------------------------------- 环境

take_lock() {
  mkdir -p "$SEC/_tmp"
  if ! mkdir "$LOCK" 2>/dev/null; then
    local pid; pid=$(cat "$LOCK/pid" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then die "另一个 real.sh 或 sweep.sh 正在跑（$(cat "$LOCK/what" 2>/dev/null)，进程 $pid）"; fi
    if [ -z "$pid" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then die "另一个 real.sh 或 sweep.sh 正在启动（运行锁刚建好）"; fi
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || die "拿不到运行锁 $LOCK"
  fi
  echo $$ > "$LOCK/pid"; echo "离线并发扫描，$(date '+%F %T') 开始" > "$LOCK/what"; echo "$OUT" > "$LOCK/out"; echo sweep > "$LOCK/kind"
}
release_lock() { if [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$LOCK"; fi; }
stop_guard() { [ ! -f "$LOCK/stop" ] || die "已经用 sweep.sh down 停下了，不再开始新的格子"; }

ensure_infra() { # Docker、镜像、网络、要用的库（原种子；没有就建）
  docker info >/dev/null 2>&1 || die "Docker 没开：先打开 Docker Desktop"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "镜像 $IMAGE 不在了"
  docker image inspect redis:7-alpine >/dev/null 2>&1 || die "本机没有 redis:7-alpine 镜像"
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET" >/dev/null
  docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null
  local want=() spec file build s
  # 库：文件名 → 建库命令。S4 的建库最后才写 stores.json，有它才算建完（建到一半断了的库不算）
  for s in $SCENS; do
    case "$s" in
      s4) want+=("stores.json|build-store") ;;
      rag) want+=("rag-a-all500.sqlite|build-rag-store a500" "rag-a-first200.sqlite|build-rag-store a") ;;
    esac
  done
  for spec in "${want[@]}"; do
    file=${spec%%|*} build=${spec#*|}
    if ! docker run --rm --network none --mount "type=volume,src=$VOL,dst=/store,readonly" --entrypoint /bin/sh "$IMAGE" -c "test -s /store/$file"; then
      say "建库（$build；原种子；S4 的记忆库约 2 分钟，rag 的内部知识库几秒）"
      # shellcheck disable=SC2086
      bash scripts/run.sh $build > "$OUT/build-${build// /-}.log" 2>&1 || die "建库失败，见 $OUT/build-${build// /-}.log"
    fi
  done
}

wait_log() { # wait_log <容器> <日志里要出现的字>
  local _
  for _ in $(seq 1 60); do
    if docker logs "$1" 2>&1 | grep -q "$2"; then return 0; fi
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || break
    sleep 0.5
  done
  docker logs "$1" >&2 || true; return 1
}

CUR=""   # 现在起着的出口属于哪个场景（收出口时把它的日志存到那里）；空就是本脚本没起出口、假模型、Redis
infra_up() { # infra_up <场景>：假模型（这一场景的延迟）、出口（只接实验网络，上游是假模型，假密钥）；rag 另起 Redis
  local scen=$1 dir=$OUT/$1
  scen_conf "$scen"
  infra_down
  # 同名的旧容器（例如 Docker 重启后留下的、已退出的）会让 docker run --name 失败；开跑前已确认没有本实验的容器在跑，这里直接删
  docker rm -f "$EGRESS" "$FAKE" "$REDIS" >/dev/null 2>&1 || true
  mkdir -p "$dir/egress"
  CUR=$dir
  docker run -d --name "$FAKE" --network "$NET" --network-alias fake --cpuset-cpus "$EGRESS_CORES" --memory 2g \
    --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe --entrypoint /usr/local/bin/node "$IMAGE" \
    --no-warnings src/fake-model.ts --port 8080 "${FAKE_ARGS[@]}" >/dev/null
  wait_log "$FAKE" 'fake-model listening' || die "假模型没起来"
  { echo "==== $(date '+%F %T')"; docker logs "$FAKE" 2>&1; } >> "$dir/fake-model.log"
  printf 'BFCL_API_KEY=%sOFFLINESWEEP0000000\n' "$DEEPSEEK_KEY_PREFIX" > "$dir/fake.keys"
  # 假模型按字数算的“费用”很大，账本上限放到 1 亿元（扫描不花钱）
  docker run -d --name "$EGRESS" --network "$NET" --network-alias egress --cpuset-cpus "$EGRESS_CORES" --memory 2g --memory-swap 2g \
    --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe \
    --mount "type=bind,src=$dir/fake.keys,dst=/run/secrets/deepseek.key,readonly" \
    --mount "type=bind,src=$dir/egress,dst=/ledger" -e DEEPSEEK_KEY_PREFIX \
    --entrypoint /usr/local/bin/node "$IMAGE" --no-warnings src/egress.ts --port 8080 --key-file /run/secrets/deepseek.key \
    --ledger /ledger --limit-cny 100000000 --upstream http://fake:8080/v1 >/dev/null
  wait_log "$EGRESS" '"ready":true' || die "出口没起来"
  if [ "$scen" = rag ]; then
    docker run -d --name "$REDIS" --network "$NET" --network-alias redis --cpuset-cpus "$REDIS_CORES" --memory 4g \
      redis:7-alpine redis-server --save "" --appendonly no >/dev/null
    wait_log "$REDIS" 'Ready to accept connections' || die "Redis 没起来"
  fi
  say "$scen：假模型（${FAKE_ARGS[*]}）、出口$([ "$scen" = rag ] && echo "、Redis" || true)就绪"
}
infra_down() { # 收本脚本起的出口（日志存到它的场景目录）、假模型、Redis；本脚本没起过就不动（不删别人的容器）
  [ -n "$CUR" ] || return 0
  if docker inspect "$EGRESS" >/dev/null 2>&1; then
    { echo "==== $(date '+%F %T') 收出口时的日志"; docker logs "$EGRESS" 2>&1; } >> "$CUR/egress/egress.log" || true
  fi
  docker rm -f "$EGRESS" "$FAKE" "$REDIS" >/dev/null 2>&1 || true
  CUR=""
}
egress_status() { # 出口的 /status（JSON）；取不到（HTTP 不是 2xx、超时）就失败
  docker exec "$EGRESS" node -e "fetch('http://127.0.0.1:8080/status',{signal:AbortSignal.timeout(10000)}).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(j=>console.log(JSON.stringify(j))).catch(e=>{console.error(String(e));process.exit(1)})"
}
json_is() { python -I -c "import json,sys; j=json.loads(sys.argv[1]); sys.exit(0 if j.get(sys.argv[2]) == json.loads(sys.argv[3]) else 1)" "$@"; }
egress_ok() { # 出口明确没停（stopped 是 false）；停了（预算、连续失败等）或查不到就不往下走
  local s; s=$(egress_status) || die "查不到出口状态"
  json_is "$s" stopped false || die "出口停了或状态不对：$s"
}
egress_ready() { # 出口没停、在途请求为 0（上一格超时留下的请求排空）才开下一格；等 2 分钟还没排空就停下
  local s _
  for _ in $(seq 1 60); do
    s=$(egress_status) || die "查不到出口状态"
    json_is "$s" stopped false || die "出口停了或状态不对：$s"
    if json_is "$s" active 0; then return 0; fi
    sleep 2
  done
  die "出口里还有在途请求，2 分钟没排空：$s"
}
cell_containers() { # 名字是 sec1new-memctx-<进程>-<格子名> 的容器（这一格的编排器和被拆出的 Worker）
  docker ps -a --format '{{.ID}} {{.Names}}' | awk -v n="$1" '$2 ~ ("^sec1new-memctx-[a-z]+-" n "$") {print $1}'
}
cleanup_cell() { # 收这一格留下的容器（超时被打断时 run.sh 来不及收）；收不掉就停下，免得残留的容器占着被测的核
  local ids; ids=$(cell_containers "$1")
  if [ -n "$ids" ]; then docker rm -f $ids >/dev/null 2>&1 || true; fi
  ids=$(cell_containers "$1")
  [ -z "$ids" ] || die "$1 的容器收不掉：$ids"
}

# ---------------------------------------------------------------- 格子

failed_cell() { # failed_cell <raw 目录> <名字> <原因>：结果挪进同级的 failed\（加时间前缀），记一笔，接着跑下一格
  local dir=$1 name=$2 why=$3 stamp f; stamp=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$dir/../failed"
  for f in "$dir/$name".*; do if [ -e "$f" ]; then mv "$f" "$dir/../failed/$stamp-$(basename "$f")"; fi; done
  printf '%s\t%s\t%s\n' "$(date '+%F %T')" "$name" "$why" >> "$OUT/failed-cells.txt"
  say "$name 作废（$why），挪进 failed\\；再跑 run 会重跑它"
}
marked_ok() { # marked_ok <结果文件> <验收标记>：标记在，而且标记里记的摘要和结果文件一致
  local h; h=$(sha256sum "$1" 2>/dev/null | cut -c1-64 || true)
  [ ${#h} = 64 ] && [ -f "$2" ] && grep -q "$h" "$2"
}
validate_cell() { # validate_cell <结果文件> <题数> <并发> <场景> <布局> <轮名标签> <要不要附加文件 0|1>：验收和真实运行相同，上游是假模型、一题都不许失败
  python -I src/cell_check.py validate "$1" 0 fake:8080 strict-cost "$2" "$3" "$4" 0 "$5" "$6" "$WARMUP" "$7"
}
mark_ok() { printf '{"validatedAt":"%s","sha256":"%s","window_end":%s,"warmup":%s}\n' "$(date '+%F %T')" "$(sha256sum "$1" | cut -c1-64)" "$WINDOW_END" "$WARMUP" > "$2"; }

run_cell() { # run_cell <raw 目录> <名字> <布局> <并发> <题数> <轮名标签> <场景参数...>：跑一格（不验收）；返回退出码
  local dir=$1 name=$2 layout=$3 c=$4 count=$5 label=$6 rc=0; shift 6
  cleanup_cell "$name"   # 上次同名的一格被打断留下的容器
  egress_ready
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "$dir/$name.docker-ps.txt" 2>&1 || true
  RAW_DIR=$dir timeout --kill-after=30 "$CELL_TIMEOUT" bash scripts/run.sh e2e "$name" "$layout" "$CORES" "$@" \
    --concurrency "$c" --count "$count" --warmup "$WARMUP" --fake http://egress:8080/v1 --seed "$name" --label "$label" \
    > "$dir/$name.log" 2>&1 || rc=$?
  cleanup_cell "$name"
  return $rc
}

cell() { # cell <场景> <遍> <并发> <布局>
  local scen=$1 pass=$2 c=$3 layout=$4 rc=0
  local name="sweep-$scen-k2-c$c-$layout-$pass" dir=$OUT/$scen/raw count=$((WINDOW_END + c)) label="sweep-$scen|c$c-$pass"
  local json=$dir/$name.json
  stop_guard
  mkdir -p "$dir"
  if [ -f "$dir/$name.ok" ]; then
    # 跳过之前重新验收一遍（题数、并发、轮名、附加文件……），和真实运行的续跑相同
    if marked_ok "$json" "$dir/$name.ok" && validate_cell "$json" "$count" "$c" "$scen" "$layout" "$label" 1 >/dev/null; then return 0; fi
    failed_cell "$dir" "$name" "验收标记和结果文件对不上，或重新验收不过"
  fi
  if [ ! -f "$json" ]; then
    say "开始 $name（$layout，并发 $c，$count 题）"
    run_cell "$dir" "$name" "$layout" "$c" "$count" "$label" "${SCEN_ARGS[@]}" || rc=$?
    if [ "$rc" = 124 ]; then failed_cell "$dir" "$name" "超过 $CELL_TIMEOUT 秒被打断"; egress_ok; return 0; fi
    if [ "$rc" = 137 ]; then failed_cell "$dir" "$name" "被强杀（超时后强杀、内存不足或 down）"; egress_ok; return 0; fi
    if [ ! -f "$json" ]; then tail -3 "$dir/$name.log" || true; failed_cell "$dir" "$name" "没写出结果（退出码 $rc）"; egress_ok; return 0; fi
  fi
  if grep -l "OOMKilled=true" "$dir/$name".*.log >/dev/null 2>&1; then failed_cell "$dir" "$name" "被拆出的 Worker 容器被系统杀掉过（OOM）"; egress_ok; return 0; fi
  if validate_cell "$json" "$count" "$c" "$scen" "$layout" "$label" 1; then mark_ok "$json" "$dir/$name.ok"; else failed_cell "$dir" "$name" "验收没过（见上面）"; fi
  egress_ok
}

warm_cell() { # warm_cell <目录> <场景参数...>：一格不计入的热身（Monolithic、并发 37、60 题）；每次开跑的头一格偏慢，先把它跑掉
  local dir=$1 name; shift
  name="warmup-$(date +%H%M%S)"
  mkdir -p "$dir"
  stop_guard
  say "热身一格（不计入）：$dir"
  run_cell "$dir" "$name" mono 37 60 "warmup|$name" "$@" || say "热身那一格没跑完（不计入，不影响后面）：见 $dir/$name.log"
}

prepare_old() { # 解开旧版程序、拷依赖；出错返回非 0（新旧程序对照这次不做，不让整夜的扫描停下）。.ready 里记归档的摘要
  local sum nm nms=()
  sum=$(sha256sum "$OLD_TAR" | cut -c1-64) || return 1
  if [ -f "$OLD_DIR/.ready" ] && [ "$(cat "$OLD_DIR/.ready")" = "$sum" ]; then return 0; fi
  rm -rf "$OLD_DIR" && mkdir -p "$OLD_DIR" || return 1
  # --force-local：路径是 F:/... 这样带冒号的写法，不加的话 tar 会当成“主机:路径”去连远程主机
  tar --force-local -xzf "$OLD_TAR" -C "$OLD_DIR" || return 1
  # 备份里没有依赖包：把现在程序目录里的各个 node_modules 按原位置拷过去（本目录的，和官方文件里 Redis 客户端的）。
  # 两版的依赖版本相同；官方文件、rag-a、relaxed 两版只有说明文件（.md）不同（11-20 核对过）
  mapfile -t nms < <(find . -name node_modules -prune -print)
  for nm in "${nms[@]}"; do mkdir -p "$OLD_DIR/03_memctx_probe/$(dirname "$nm")" && cp -r "$nm" "$OLD_DIR/03_memctx_probe/$nm" || return 1; done
  if [ ! -f "$OLD_DIR/03_memctx_probe/node_modules/@codesoul-co/ditto/package.json" ] \
     || [ ! -d "$OLD_DIR/03_memctx_probe/official/examples/_shared/tools/storage/dependencies/node_modules/redis" ]; then
    echo "旧版程序的依赖没拷全（找到的 node_modules：${nms[*]}）" >&2; return 1
  fi
  echo "$sum" > "$OLD_DIR/.ready"
}

prog_check() { # 新旧程序对照：四格都通过验收就跳过；否则整组重做（先一格热身，再 旧 新 新 旧），有一格不过就停在那里、下次整组重做
  local dir=$OUT/prog-check/raw spec name probe rc all=1 oldwin require
  mkdir -p "$dir"
  for spec in $PROG_ORDER; do
    name="progcheck-rag-k2-c37-mono-$spec"; require=1; [ "${spec%%-*}" = old ] && require=0
    if ! marked_ok "$dir/$name.json" "$dir/$name.ok" || ! validate_cell "$dir/$name.json" "$PROG_COUNT" 37 rag mono "progcheck|$spec" "$require" >/dev/null; then all=0; fi
  done
  if [ "$all" = 1 ]; then say "新旧程序对照四格都已验收，跳过"; return 0; fi
  for spec in $PROG_ORDER; do
    name="progcheck-rag-k2-c37-mono-$spec"
    if ls "$dir/$name".* >/dev/null 2>&1; then failed_cell "$dir" "$name" "新旧程序对照整组重做"; fi
  done
  if ! prepare_old; then say "旧版程序没准备好（见上面），新旧程序对照这次不做；接着跑扫描"; return 0; fi
  oldwin=$(cd "$OLD_DIR/03_memctx_probe" && (pwd -W 2>/dev/null || pwd))
  { echo "旧版程序：docs/31_附录/程序备份/$(basename "$OLD_TAR")（11-20 凌晨 rag 那一轮用的程序）"; sha256sum "$OLD_TAR" | cut -c1-64; } > "$OUT/prog-check/old-program.txt"
  warm_cell "$OUT/prog-check/warmup" "${PROG_ARGS[@]}"
  for spec in $PROG_ORDER; do
    name="progcheck-rag-k2-c37-mono-$spec"; probe=$PROBE; require=1
    if [ "${spec%%-*}" = old ]; then probe=$oldwin; require=0; fi   # 旧版没有附加文件（每次调用的用时、rag 报告）
    stop_guard
    say "开始 $name（$([ "$require" = 0 ] && echo 旧版 || echo 新版)程序，rag Monolithic，并发 37，$PROG_COUNT 题）"
    rc=0
    PROBE_DIR=$probe run_cell "$dir" "$name" mono 37 "$PROG_COUNT" "progcheck|$spec" "${PROG_ARGS[@]}" || rc=$?
    if [ "$rc" = 0 ] || [ "$rc" = 1 ] || [ "$rc" = 2 ]; then
      if [ -f "$dir/$name.json" ] && validate_cell "$dir/$name.json" "$PROG_COUNT" 37 rag mono "progcheck|$spec" "$require"; then
        mark_ok "$dir/$name.json" "$dir/$name.ok"; egress_ok; continue
      fi
    fi
    failed_cell "$dir" "$name" "新旧程序对照这一格没跑成（退出码 $rc）"
    say "新旧程序对照停在 $name，后面几格不跑（下次 run 整组重做）；接着跑扫描"; egress_ok; return 0
  done
}

run_info() { # run_info <场景>：运行环境；续跑时程序、数据、库、镜像和第一次不同就停下（ALLOW_CODE_CHANGE=1 才继续）
  python -I src/run_info.py "$OUT/$1" plan="sweep-$1" levels="$LOW $LEVELS" passes="8 起两遍，2、4 一遍" count="$WINDOW_END+并发" \
    warmup="$WARMUP" cores="$CORES" fake="${FAKE_ARGS[*]}" image="$IMAGE" scenario_args="${SCEN_ARGS[*]}" store="${SCEN_ARGS[3]#/store/}" \
    volume="$VOL" mode=run || die "run_info.py 没通过（见上面；确认程序、数据的变化不影响已跑完的格子以后，加 ALLOW_CODE_CHANGE=1）"
}

sweep_scen() { # sweep_scen <场景>：扫描一个场景（rag 先做新旧程序对照），需要时补测，最后核对各格请求逐字相同
  local scen=$1 specs spec pass c layout extra ls k
  scen_conf "$scen"
  run_info "$scen"
  infra_up "$scen"
  if [ "$scen" = rag ]; then prog_check; scen_conf rag; fi
  warm_cell "$OUT/$scen/warmup" "${SCEN_ARGS[@]}"
  mapfile -t specs < <(plan "$scen")
  scen_conf "$scen"
  for spec in "${specs[@]}"; do read -r pass c layout <<< "$spec"; cell "$scen" "$pass" "$c" "$layout"; done
  # 补测名单：Windows 上的 Python 会输出 \r\n，去掉 \r，免得最后一个布局名带着 \r
  # 名单算不出来（汇总程序出错）只影响要不要补测：记一笔、这次不补测、接着跑；下次 run 会重算
  extra=$(python -I src/sweep_report.py "$OUT" --supplements "$scen" | tr -d '\r') \
    || { say "$scen：补测名单没算出来（见上面），这次不补测，接着往下跑"; extra=""; }
  if [ -n "$extra" ]; then
    say "$scen：$extra 在 36 只比 48 或 64 慢一点（超出 3% 的部分在两遍的差别以内），在 36、48、64 再补两遍"
    read -r -a ls <<< "$extra"
    for c in 36 48 64; do for layout in "${ls[@]}"; do cell "$scen" p3 "$c" "$layout"; done; done
    for c in 64 48 36; do for ((k=${#ls[@]}-1; k>=0; k--)); do cell "$scen" p4 "$c" "${ls[$k]}"; done; done
  fi
  stop_guard
  infra_down
  python -I src/cell_check.py sweep "$OUT/$scen" 2>&1 | tee "$OUT/$scen/sweep-check.txt" \
    || say "$scen：各格请求、结果逐字相同的核对没过（见 $OUT/$scen/sweep-check.txt）；汇总不会据此推荐并发"
}

report() { # 汇总：raw\ 里有没验收的结果文件（中途断了）就不汇总，免得表里混进没验收的格子
  local s f bad=0 sheets=()
  for s in $SCENS; do
    for f in "$OUT/$s/raw"/*.json; do
      [ -e "$f" ] || continue
      if ! marked_ok "$f" "${f%.json}.ok"; then echo "没验收的结果文件：$f"; bad=1; fi
    done
  done
  [ "$bad" = 0 ] || die "raw\\ 里有没验收的结果文件：先再跑 bash scripts/sweep.sh run 把它们验收完（或挪走），再汇总"
  for s in $SCENS; do
    if [ -d "$OUT/$s/raw" ]; then python -I src/export.py "$OUT/$s/raw" "$OUT/$s" > /dev/null; fi
  done
  python -I src/sweep_report.py "$OUT" > "$OUT/扫描结果.md"
  for s in "曲线=$OUT/curves.csv" "rag每格=$OUT/rag/cells.csv" "S4每格=$OUT/s4/cells.csv" "rag拆开对比=$OUT/rag/pairs.csv" \
           "S4拆开对比=$OUT/s4/pairs.csv" "S4对Direct=$OUT/s4/pairs-direct.csv" "rag对Direct=$OUT/rag/pairs-direct.csv" "S4合并对照=$OUT/s4/pairs-memctx.csv"; do
    if [ -f "${s#*=}" ]; then sheets+=("$s"); fi
  done
  if [ ${#sheets[@]} -gt 0 ]; then python -I src/workbook.py "$OUT/扫描结果.xlsx" "${sheets[@]}" >/dev/null; fi
  say "汇总写好了：$OUT/扫描结果.md"
}

# ---------------------------------------------------------------- 入口

case "${1:-}" in
  run)
    mkdir -p "$OUT"
    take_lock
    trap release_lock EXIT
    exec > >(tee -a "$OUT/sweep.log") 2>&1
    say "离线并发扫描开始：结果目录 $OUT"
    # 开跑前的检查都过了，才装“退出时收本脚本起的出口、假模型、Redis”
    [ -z "$(docker ps -q --filter name=sec1new-memctx-)" ] || die "还有 sec1new-memctx- 开头的容器在跑，先停掉（bash scripts/real.sh down 或 bash scripts/sweep.sh down）"
    others=$(docker ps --format '{{.Names}}' || true)
    [ -z "$others" ] || say "提醒：有本实验以外的容器在跑（$(echo $others)），会抢 CPU；每格的 docker-ps.txt 里有记录"
    write_plan || die "计划对不上（见上面）"
    trap 'infra_down; release_lock' EXIT
    ensure_infra
    for s in $SCENS; do sweep_scen "$s"; done
    report
    # 收尾：删本实验的网络、库（库随时能重建）和解开的旧版程序
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    rm -rf "$OLD_DIR"
    if [ -s "$OUT/failed-cells.txt" ]; then say "有作废过的格子（见 failed-cells.txt；没重跑成的，再跑 run 会重跑）"; fi
    say "离线并发扫描全部完成。先看 $OUT/扫描结果.md" ;;
  report)
    [ -f "$OUT/sweep-plan.json" ] || die "$OUT 里没有 sweep-plan.json：先跑 run"
    report ;;
  down)
    # 运行锁是 real.sh 的（真实运行在跑）就不动；是扫描的才放停止标记、删本实验的容器
    if [ -d "$LOCK" ] && [ "$(cat "$LOCK/kind" 2>/dev/null)" != sweep ]; then
      die "运行锁不是扫描的（$(cat "$LOCK/what" 2>/dev/null)）：真实运行在跑，要停用 bash scripts/real.sh down"
    fi
    if [ -d "$LOCK" ]; then touch "$LOCK/stop"; fi
    docker ps -a --filter name=sec1new-memctx- -q | xargs -r docker rm -f >/dev/null
    say "已停：不再开始新的格子；再跑 bash scripts/sweep.sh run 会接着跑（正在跑的那一格作废、重跑）" ;;
  *)
    sed -n '2,24p' "$0"; exit 2 ;;
esac
