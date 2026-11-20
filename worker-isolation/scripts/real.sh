#!/usr/bin/env bash
# 真实模型运行。在 Git Bash 里跑，第二个参数指明哪一轮：
#   full-s4：全量 500 题的 S4（docs\31）。12 格，并发 37，每格 500 题、热身 20：Monolithic、Direct、拆 MEMORY、拆 CONTEXT、
#            两个都拆、MEMORY 和 CONTEXT 合进一个进程（合并对照），然后倒过来再跑一遍。结果 results\full-s4-500，账本单独记，上限 400 元
#   full-rag：全量 500 题的官方 rag-qa（切法 (a)，docs\31）。6 格，并发 37：Monolithic、Direct、拆 INTERACTION+RETRIEVAL，再倒过来一遍。
#            结果 results\full-rag-500，账本单独记，上限 400 元。要 Redis（照官方放 CONTEXT），脚本自己起
#   以下几轮都已跑完、已汇总，默认不让 run、judge（frozen_guard）：
#   mem：拆 MEMORY（docs\19 方案 A，11-19）。4 格：Monolithic、拆 MEMORY、拆 MEMORY、Monolithic。结果 results\memctx-real-s4
#   ctx：拆 CONTEXT 和两个都拆（docs\21，11-19）。6 格：Monolithic、拆 CONTEXT、两个都拆、两个都拆、拆 CONTEXT、Monolithic。结果 results\memctx-real-s4-ctx
#   ctx2：ctx 的第二次（docs\29，11-20），6 格和 ctx 完全相同。结果 results\memctx-real-s4-ctx2
#   rag：官方 rag-qa 切法 (a)（docs\30，11-20）。8 格：并发 16、37 各一组 Monolithic、拆 INTERACTION+RETRIEVAL、拆、Monolithic。结果 results\rag-real-a
#   rag 场景：官方对每一步模型输出都有检查，过不了的题照官方算答不出（记成“模型输出不合格”，判分算错）；这类失败不让格子作废，
#   程序、网络出错的失败仍按每格最多 4 题算（全量 500 题时是 10 题，即 2%）。
#
#   bash scripts/real.sh check [计划]      开跑前检查，不花钱：Docker 和镜像、DeepSeek 密钥文件、GPT-4o 文件、
#                                         容器能否连到 DeepSeek（不带密钥）、记忆库（没有就重建，约 2 分钟）
#   bash scripts/real.sh run full-s4      一次跑完：检查 → 写 run-info.json → GPT-4o 判分冒烟（2 次请求）→ DeepSeek 冒烟（这一轮的各布局各 3 题）
#                                         → 正式各格 → GPT-4o 判分 → 汇总 → 收尾（删本轮容器、网络、记忆库卷）
#   bash scripts/real.sh judge full-s4    只重新判分和汇总（正式各格已经跑完；判过的回答不再判）
#   bash scripts/real.sh rehearse full-s4 离线演练：出口接本机假模型，走一遍冒烟和正式各格（全量计划默认也是每格 500 题，REHEARSE_COUNT 可改）。
#                                         不联网、不读真实密钥、不花钱
#   bash scripts/real.sh down             停掉出口和本轮的容器、网络（中途想停就用它）
#
# DeepSeek 费用上限：全量两轮每轮 400 元，以前几轮每轮 100 元（LIMIT_CNY，只许调低），按高峰价记账，由出口在每次发请求前卡住；
# 账本在这一轮结果目录的 egress\ 里，跨重跑累计。GPT-4o 判分：全量两轮不设请求次数上限，以前几轮 1500–2000 次。
# 注意：python -I 不看 PYTHONIOENCODING，内嵌的 Python 自己把输出设成 UTF-8。
# 中途断了，再跑一次 run：已经跑完的格子跳过；某一格失败超过 2% 会停下，那一格的结果挪进 failed\，再跑 run 会重跑它。
# 换密钥文件：DEEPSEEK_KEY_FILE=<路径> bash scripts/real.sh run；换 GPT-4o 文件：JUDGE_KEY_FILE=<路径>。
# 认 DeepSeek 密钥用的前缀写在本机的 local.env（不进 git，格式见 local.env.example），脚本开头读进来。
# 判分要求请求和中转返回的模型都正好是 gpt-4o-2024-08-06；中转返回的名字不同而你确认可以接受，
# 用 JUDGE_ALLOW_OTHER_MODEL=1 bash scripts/real.sh run（结果就不算 LongMemEval 官方口径，报告里会写出实际返回的模型）。
set -euo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."
# 本机配置（不进 git）：DEEPSEEK_KEY_PREFIX 等，格式见 local.env.example
if [ -f local.env ]; then set -a; . ./local.env; set +a; fi

# 目录都从本脚本的位置推出来：本目录（03_memctx_probe）的上一级是实验根目录（sec1-2.0）。Git Bash 的 pwd -W 给出
# F:/... 这样 Docker 认得的写法。私密文件的位置（密钥文件、GPT-4o 文件）写在 local.env（不进 git），代码里不写。
PROBE=$(pwd -W 2>/dev/null || pwd)
SEC=${PROBE%/*}
JUDGE_KEY_FILE=${JUDGE_KEY_FILE:-}
LME_RAW=$SEC/bench_data/longmemeval-s/longmemeval_s_cleaned.json
LIMIT_CNY_ASKED=${LIMIT_CNY:-}   # 不设就用这一轮的上限 LIMIT_MAX（见下面各轮）
JUDGE_OPTS=()
if [ "${JUDGE_ALLOW_OTHER_MODEL:-}" = 1 ]; then JUDGE_OPTS=(--allow-other-model); fi
IMAGE=3513d314a207
NET=sec1new-memctx-net        # --internal：编排器、被拆的 Worker、出口都在这里，不能出外网
OUTNET=sec1new-memctx-out     # 只放出口一个容器的外网网络（不用 Docker 默认 bridge，别的容器连不到出口）
VOL=sec1new-memctx-store
EGRESS=sec1new-memctx-egress
FAKE=sec1new-memctx-fake
EGRESS_CORES=28-31          # 出口和假模型在另外的核上，不占被测的 2 个核
CORES=4,6                   # 主测 2 核，和 docs\18 相同
L=/data/longmemeval-s/derived/memctx-lme-first200.jsonl
L500=/data/longmemeval-s/derived/memctx-lme-all500.jsonl   # 全量 500 题（src\sample500.ts；前 200 题和 L 逐字节相同）
REDIS=sec1new-memctx-redis REDIS_CORES=26
# 哪一轮：结果目录、格子名前缀、冒烟的布局、正式各格（布局:轮[:并发]，按顺序跑；不写并发就是 37）。
# 场景参数、库文件和建库命令默认是 S4；rag 那一轮另设。冒烟、正式每格最多允许几题“模型输出不合格”（只有 rag 有这类失败）。
PLAN=${2:-}
SCEN=s4 SCEN_ARGS=(--scenario s4 --db /store/users-100.sqlite --tasks "$L") STORE_FILE=users-100.sqlite STORE_BUILD=(build-store)
# 正式每格“模型输出不合格”最多占题数的百分之几；开格前账本至少剩多少元（一格的费用加上在途预扣，见 cell）；
# 每格题数、热身题数；这一轮 DeepSeek 费用上限（元）；判分请求次数上限（0 是不设上限）。
# REQUIRE_EXTRAS：每格必须有两个附加文件（每次调用的用时、rag 每题的报告；以前几轮没有）。
SMOKE_MODEL_FAIL=0 FORMAL_MODEL_FAIL_PCT=0 JUDGE_MAX_CALLS=1500 MIN_CELL_CNY=30 COUNT=200 WARMUP=20 LIMIT_MAX=100 REQUIRE_EXTRAS=0
case "$PLAN" in
  # 全量 500 题（docs\31）。顺序对称：前一半和后一半倒过来，时间上的漂移对各布局一样。
  # S4 一格 500 题约 28 元（高峰价，11-19/20 每题约 0.055 元），12 格约 330 元；开格前剩 40 元足够（一格加上最多 37 个在途预扣）。
  full-s4) RESULTS=$SEC/results/full-s4-500 GROUP=full-s4-u100
       SCEN_ARGS=(--scenario s4 --db /store/users-100.sqlite --tasks "$L500")
       SMOKE_LAYOUTS=(mono direct split-memory split-context split-both split-memctx)
       FORMAL=(mono:r1 direct:r1 split-memory:r1 split-context:r1 split-both:r1 split-memctx:r1
               split-memctx:r2 split-both:r2 split-context:r2 split-memory:r2 direct:r2 mono:r2)
       COUNT=500 JUDGE_MAX_CALLS=0 MIN_CELL_CNY=40 LIMIT_MAX=400 REQUIRE_EXTRAS=1 ;;
  # rag 一格 500 题约 10 元（11-20 每格 200 题约 3.8 元），6 格约 60 元；冒烟每布局 3 题里最多 1 题、正式每格最多一半过不了官方输出检查（同 rag）。
  full-rag) RESULTS=$SEC/results/full-rag-500 GROUP=full-raga-u500
       SCEN=rag STORE_FILE=rag-a-all500.sqlite STORE_BUILD=(build-rag-store a500)
       SCEN_ARGS=(--scenario rag --db /store/rag-a-all500.sqlite --tasks /data/longmemeval-s/derived/rag-a-all500.jsonl --context-store redis)
       SMOKE_LAYOUTS=(mono direct split-interaction)
       FORMAL=(mono:r1 direct:r1 split-interaction:r1 split-interaction:r2 direct:r2 mono:r2)
       SMOKE_MODEL_FAIL=1 FORMAL_MODEL_FAIL_PCT=50 COUNT=500 JUDGE_MAX_CALLS=0 MIN_CELL_CNY=20 LIMIT_MAX=400 REQUIRE_EXTRAS=1 ;;
  mem) RESULTS=$SEC/results/memctx-real-s4 GROUP=real-s4-u100
       SMOKE_LAYOUTS=(mono split-memory)
       FORMAL=(mono:r1 split-memory:r1 split-memory:r2 mono:r2) ;;
  ctx) RESULTS=$SEC/results/memctx-real-s4-ctx GROUP=real-s4ctx-u100
       SMOKE_LAYOUTS=(split-context split-both)
       FORMAL=(mono:r1 split-context:r1 split-both:r1 split-both:r2 split-context:r2 mono:r2) ;;
  ctx2) RESULTS=$SEC/results/memctx-real-s4-ctx2 GROUP=real-s4ctx2-u100
       SMOKE_LAYOUTS=(split-context split-both)
       FORMAL=(mono:r1 split-context:r1 split-both:r1 split-both:r2 split-context:r2 mono:r2) ;;
  rag) RESULTS=$SEC/results/rag-real-a GROUP=real-raga-u200
       SCEN=rag STORE_FILE=rag-a-first200.sqlite STORE_BUILD=(build-rag-store a)
       SCEN_ARGS=(--scenario rag --db /store/rag-a-first200.sqlite --tasks /data/longmemeval-s/derived/rag-a-first200.jsonl --context-store redis)
       SMOKE_LAYOUTS=(mono split-interaction)
       FORMAL=(mono:r1:16 split-interaction:r1:16 split-interaction:r2:16 mono:r2:16 mono:r1:37 split-interaction:r1:37 split-interaction:r2:37 mono:r2:37)
       # 冒烟 3 题里最多 1 题、正式每格最多一半的题过不了官方的输出检查；再多说明模型和官方流程合不上，停下来看（大问题才停）。
       # 8 格 1600 段回答，相同的只判一次；判分请求上限放到 2000 次（GPT-4o 约 3 美元以内）。
       # 一格约 5 元（高峰价）加上最多 37 个在途预扣（每个不到 0.2 元），开格前剩 20 元足够（S4 一格约 11 元，留的是 30 元）。
       SMOKE_MODEL_FAIL=1 FORMAL_MODEL_FAIL_PCT=50 JUDGE_MAX_CALLS=2000 MIN_CELL_CNY=20 ;;
  "") RESULTS="" ;;
  *) echo "第二个参数是 full-s4、full-rag（以及已跑完的 mem、ctx、ctx2、rag），不是 $PLAN" >&2; exit 2 ;;
esac
LIMIT_CNY=${LIMIT_CNY_ASKED:-$LIMIT_MAX}
# rag 的详细分析要题目文件（宿主机上的路径）
RAG_TASKS_HOST=$SEC/bench_data/longmemeval-s/derived/rag-a-first200.jsonl
[ "$PLAN" = full-rag ] && RAG_TASKS_HOST=$SEC/bench_data/longmemeval-s/derived/rag-a-all500.jsonl
need_plan() { [ -n "$RESULTS" ] || { echo "要指明哪一轮：bash scripts/real.sh $1 full-s4 或 full-rag（全量 500 题）；mem、ctx、ctx2、rag 已跑完" >&2; exit 2; }; }
# mem、ctx 两轮 11-19 已跑完，ctx2、rag 11-20 已跑完，都已汇总。run、judge 会重写那里的出口日志和汇总（run 还会先发 GPT-4o 冒烟），
# 所以默认不让跑。演练（rehearse）只写 _tmp，不受限。
frozen_guard() {
  case "$PLAN" in mem|ctx|ctx2|rag)
    if [ "${MEM_FORCE:-}" != 1 ]; then
      echo "$PLAN 这一轮已跑完、已汇总；为了不改动它的结果目录，不执行 $1 $PLAN（确实要的话加 MEM_FORCE=1）" >&2; exit 2
    fi ;;
  esac
}
# 格子名带并发；判分和汇总按“轮”配对（同一轮的 Monolithic 和拆开的比），所以计划里写了并发时，轮名也带上并发（c16-r1），
# 免得不同并发的格子配到一起。S4 那几轮没写并发，格子名、轮名和以前一样。
cell_name() { local layout round conc; IFS=: read -r layout round conc <<< "$1"; echo "$GROUP-k2-c${conc:-37}-$layout-$round"; }
round_label() { local layout round conc; IFS=: read -r layout round conc <<< "$1"; if [ -n "$conc" ]; then echo "c$conc-$round"; else echo "$round"; fi; }
export MEMCTX_REAL=1        # 编排器和被拆的 Worker 用真实模型那一套提示词（src\common.ts）

say() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "[$(date +%H:%M:%S)] 停下：$*" >&2; exit 1; }

# ---------------------------------------------------------------- 检查

find_key() { # 找含 DEEPSEEK_KEY_PREFIX 开头密钥的文件；只打印文件名和变量名，不打印密钥
  local f
  if [ -n "${DEEPSEEK_KEY_FILE:-}" ]; then
    node --no-warnings src/egress.ts --check-key "$DEEPSEEK_KEY_FILE" >&2 || return 1; echo "$DEEPSEEK_KEY_FILE"; return 0
  fi
  # 候选文件写在 local.env 的 DEEPSEEK_KEY_CANDIDATES（空格分开；路径里不能有空格）
  for f in ${DEEPSEEK_KEY_CANDIDATES:-}; do
    [ -f "$f" ] || continue
    if node --no-warnings src/egress.ts --check-key "$f" >&2; then echo "$f"; return 0; fi
  done
  return 1
}

no_other_run() { # 演练和正式共用容器名、网络别名，同时跑会串线
  [ -z "$(docker ps -q --filter name=sec1new-memctx-)" ] || die "还有 sec1new-memctx- 开头的容器在跑（$(docker ps --filter name=sec1new-memctx- --format '{{.Names}}' | tr '\n' ' ')），先 bash scripts/real.sh down"
}

ensure_infra() { # Docker、镜像、网络、记忆库卷
  docker info >/dev/null 2>&1 || die "Docker 没开：先打开 Docker Desktop"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "镜像 $IMAGE 不在了"
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET" >/dev/null
  docker network inspect "$OUTNET" >/dev/null 2>&1 || docker network create "$OUTNET" >/dev/null
  docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null
  if ! docker run --rm --network none --mount "type=volume,src=$VOL,dst=/store,readonly" --entrypoint /bin/sh "$IMAGE" -c "test -s /store/$STORE_FILE"; then
    say "建库 $STORE_FILE（bash scripts/run.sh ${STORE_BUILD[*]}；S4 的记忆库约 2 分钟，rag 的内部知识库几秒）"
    mkdir -p "$OUT"
    bash scripts/run.sh "${STORE_BUILD[@]}" > "$OUT/build-store.log" 2>&1 || die "建库失败，见 $OUT/build-store.log"
  fi
  if [ "$SCEN" = rag ]; then docker image inspect redis:7-alpine >/dev/null 2>&1 || die "本机没有 redis:7-alpine 镜像（实验网络不能出外网，拉不了）"; fi
}

redis_up() { # rag 照官方用 Redis 放 CONTEXT（docs\24）。设置和 run.sh infra-up 相同：只在 --internal 网络里，不占被测的核
  docker rm -f "$REDIS" >/dev/null 2>&1 || true
  docker run -d --name "$REDIS" --network "$NET" --network-alias redis --cpuset-cpus "$REDIS_CORES" --memory 4g \
    redis:7-alpine redis-server --save "" --appendonly no >/dev/null
  for _ in $(seq 1 40); do
    if docker exec "$REDIS" redis-cli ping 2>/dev/null | grep -q PONG; then say "Redis 就绪"; return 0; fi
    sleep 0.5
  done
  die "Redis 没起来"
}
redis_down() { docker rm -f "$REDIS" >/dev/null 2>&1 || true; }

check_all() {
  say "检查 Docker、镜像、记忆库"
  ensure_infra
  [ -n "${DEEPSEEK_KEY_PREFIX:-}" ] || die "没有设 DEEPSEEK_KEY_PREFIX：在 03_memctx_probe\\local.env 里写一行 DEEPSEEK_KEY_PREFIX=<密钥开头几位>（格式见 local.env.example）"
  say "找 DeepSeek 密钥文件（local.env 里 DEEPSEEK_KEY_PREFIX 开头的密钥）"
  KEY=$(find_key) || die "local.env 的 DEEPSEEK_KEY_CANDIDATES 列的文件里都没找到 DEEPSEEK_KEY_PREFIX 开头的密钥；用 DEEPSEEK_KEY_FILE=<路径> 指定"
  say "用 $KEY"
  # 密钥文件里写的模型地址的主机必须是 api.deepseek.com：验收只认它，用别的主机会先花钱、再被验收拦下。
  # 只比主机名（地址带不带 /v1 这类路径都行）。
  local up; up=$(node --no-warnings src/egress.ts --check-key "$KEY" 2>/dev/null \
    | python -I -c "import json,sys,urllib.parse; print(urllib.parse.urlsplit(json.loads(sys.stdin.read())['upstream']).hostname)") \
    || die "读不出密钥文件里的模型地址"
  [ "$up" = api.deepseek.com ] || die "密钥文件里的模型地址的主机是 $up，不是 api.deepseek.com"
  say "检查 GPT-4o 文件"
  [ -n "$JUDGE_KEY_FILE" ] || die "没有设 JUDGE_KEY_FILE：在 local.env 里写 GPT-4o 文件的路径（格式见 local.env.example）"
  python -I src/judge.py --check "$JUDGE_KEY_FILE" || die "GPT-4o 文件还没填好：$JUDGE_KEY_FILE"
  say "检查出口那样接两个网络的容器能否连到 DeepSeek（不带密钥，预期 HTTP 401）"
  local probe=sec1new-memctx-netcheck rc=0
  docker rm -f "$probe" >/dev/null 2>&1 || true
  docker run -d --name "$probe" --network "$OUTNET" --entrypoint /bin/sh "$IMAGE" -c 'sleep 60' >/dev/null
  docker network connect "$NET" "$probe"
  docker exec "$probe" node -e \
    "fetch('https://api.deepseek.com/models',{signal:AbortSignal.timeout(15000)}).then(r=>console.log('能连上 api.deepseek.com（HTTP '+r.status+'）')).catch(e=>{console.error('连不上 api.deepseek.com：'+(e.cause?.code||e.message));process.exit(1)})" || rc=$?
  docker rm -f "$probe" >/dev/null 2>&1 || true
  [ "$rc" = 0 ] || die "出口那样的容器连不上 DeepSeek"
}

# ---------------------------------------------------------------- 出口

egress_up() { # egress_up <密钥文件> <网络 outside|internal> [出口的其他参数...]
  local key=$1 network=$2; shift 2
  docker rm -f "$EGRESS" >/dev/null 2>&1 || true
  mkdir -p "$OUT/egress"
  # 只有这个容器挂了密钥文件（只读）、能上外网（先接专用的外网网络，再接进本轮的 --internal 网络）；其余容器只能找它。
  # 离线演练时只接 --internal 网络，连不了外网。
  local net=(--network "$OUTNET")
  [ "$network" = outside ] || net=(--network "$NET" --network-alias egress)
  # DEEPSEEK_KEY_PREFIX 用 -e 传进去（只给变量名，值从本脚本的环境取，不出现在命令行里）。
  docker run -d --name "$EGRESS" "${net[@]}" --cpuset-cpus "$EGRESS_CORES" --memory 2g --memory-swap 2g \
    --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe \
    --mount "type=bind,src=$key,dst=/run/secrets/deepseek.key,readonly" \
    --mount "type=bind,src=$OUT/egress,dst=/ledger" -e DEEPSEEK_KEY_PREFIX \
    --entrypoint /usr/local/bin/node "$IMAGE" --no-warnings src/egress.ts --port 8080 --key-file /run/secrets/deepseek.key \
    --ledger /ledger --limit-cny "$LIMIT_CNY" "$@" >/dev/null
  if [ "$network" = outside ]; then docker network connect --alias egress "$NET" "$EGRESS"; fi
  for _ in $(seq 1 60); do
    if docker logs "$EGRESS" 2>&1 | grep -q '"ready":true'; then say "出口就绪：$(docker logs "$EGRESS" 2>&1 | grep '"ready":true')"; return 0; fi
    [ "$(docker inspect -f '{{.State.Running}}' "$EGRESS" 2>/dev/null)" = true ] || break
    sleep 0.5
  done
  docker logs "$EGRESS" >&2 || true; die "出口没起来"
}
egress_status() { docker exec "$EGRESS" node -e "fetch('http://127.0.0.1:8080/status').then(r=>r.text()).then(t=>console.log(t))"; }
egress_down() {
  if docker inspect "$EGRESS" >/dev/null 2>&1; then
    egress_status > "$OUT/egress/final-status.json" 2>/dev/null || true
    { echo "==== $(date '+%F %T') 收出口时的日志"; docker logs "$EGRESS" 2>&1; } >> "$OUT/egress/egress.log" || true
    docker rm -f "$EGRESS" >/dev/null 2>&1 || true
  fi
}

# ---------------------------------------------------------------- 格子

cell() { # cell <目录> <名字> <布局> <并发> <题数> <热身> <组> <轮> <最多失败题数> <开格前至少剩多少元> [最多“模型输出不合格”题数]
  local dir=$1 name=$2 layout=$3 c=$4 count=$5 warm=$6 group=$7 round=$8 maxfail=$9 mincny=${10} maxmodel=${11:-0} rc=0 status
  mkdir -p "$dir"
  # 只认验收标记：结果文件在、标记也在，而且标记里记的哈希和结果文件一致，才算跑完。
  # 结果文件读不到时哈希是空串，空串能匹配任何标记，所以先确认拿到了 64 位哈希；跳过之前再验收一遍，
  # 混进来的别的结果（例如演练的同名文件）会被上游、题数等检查拦下。
  if [ -f "$dir/$name.ok" ]; then
    local hash; hash=$(sha256sum "$dir/$name.json" 2>/dev/null | cut -c1-64 || true)
    { [ ${#hash} = 64 ] && grep -q "$hash" "$dir/$name.ok"; } \
      || die "$name 的验收标记和结果文件对不上（结果文件丢了或被换过？），先人工核对 $dir"
    validate "$dir/$name.json" "$maxfail" "$count" "$c" "$maxmodel" "$layout" "$group|$round" "$warm" >/dev/null \
      || die "$name 有验收标记，但重新验收不过（是不是混进了别的结果？），先人工核对 $dir"
    say "已有 $name（验收通过），跳过"; return 0
  fi
  if [ -f "$dir/$name.json" ]; then
    say "$name 有结果、没有验收标记（上次多半在验收前中断）：只重新验收，不重跑"
  else
    stopped_guard
    # 本实验以外的容器会抢 CPU、影响测量：正式运行时停下，先停掉它们再 run（已跑完的格子会跳过）
    local others; others=$(docker ps --format '{{.Names}}' | grep -v '^sec1new-memctx-' || true)
    if [ -n "$others" ] && [ "$UPSTREAM_HOST" = api.deepseek.com ] && [ "${ALLOW_OTHER_CONTAINERS:-}" != 1 ]; then
      die "有本实验以外的容器在跑（$(echo $others)），会抢 CPU、影响测量：先停掉再跑 run（已跑完的格子会跳过）；确实要一起跑就加 ALLOW_OTHER_CONTAINERS=1"
    fi
    docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "$dir/$name.docker-ps.txt" 2>&1 || true
    status=$(egress_status) || die "查不到出口状态"
    if grep -q '"stopped":true' <<<"$status"; then die "出口已经停了：$status"; fi
    # 剩余额度不够一整格（一格约 13–16 元，加上 37 个在途预扣约 11 元）就不开，免得跑到一半撞上限、整格作废。
    python -I -c "import json,sys; r=json.loads(sys.argv[1])['ledger']['remainingCny']; sys.exit(0 if r >= float(sys.argv[2]) else 1)" "$status" "$mincny" \
      || die "剩余额度不够开 $name（要至少 $mincny 元）：$status"
    say "开始 $name（$layout，并发 $c，$count 题）"
    RAW_DIR=$dir bash scripts/run.sh e2e "$name" "$layout" "$CORES" "${SCEN_ARGS[@]}" --concurrency "$c" --count "$count" --warmup "$warm" \
      --fake http://egress:8080/v1 --seed "$name" --label "$group|$round" > "$dir/$name.log" 2>&1 || rc=$?
    [ -f "$dir/$name.json" ] || { tail -5 "$dir/$name.log" >&2; die "$name 没写出结果（退出码 $rc），见 $dir/$name.log"; }
  fi
  if grep -l "OOMKilled=true" "$dir/$name".*.log >/dev/null 2>&1; then
    echo "    被拆出的 Worker 容器被系统杀掉过（OOM）" >&2; mark_failed "$dir" "$name" "Worker 被系统杀掉"
  fi
  if ! validate "$dir/$name.json" "$maxfail" "$count" "$c" "$maxmodel" "$layout" "$group|$round" "$warm"; then
    mark_failed "$dir" "$name" "见上面验收打印的原因（程序或网络出错超过 $maxfail 题、模型输出不合格超过 $maxmodel 题、窗口无效、设定或上游不对、开着思考、费用算不出、附加文件不完整等）"
  fi
  printf '{"validatedAt":"%s","sha256":"%s"}\n' "$(date '+%F %T')" "$(sha256sum "$dir/$name.json" | cut -c1-64)" > "$dir/$name.ok"
  # 出口停了（预算、鉴权、连续失败、费用超出预扣）就不往下走：这一格已经验收通过、保留，后面的格子和判分等你看过再说。
  if docker inspect "$EGRESS" >/dev/null 2>&1; then
    status=$(egress_status) || die "查不到出口状态"
    if grep -q '"stopped":true' <<<"$status"; then die "$name 已验收通过，但出口停了：$status"; fi
  fi
}

validate() { # validate <结果文件> <最多失败题数> <题数> <并发> [最多“模型输出不合格”题数] [布局] [轮名标签] [热身]：打印这一格的小结，合格返回 0
  # 检查都在 src/cell_check.py（docs\31 第 4 节“一格作废”）；冒烟（最多失败 0 题）另要求每个成功请求都算得出费用。
  python -I src/cell_check.py validate "$1" "$2" "$UPSTREAM_HOST" "$([ "$2" = 0 ] && echo strict-cost || echo -)" "$3" "$4" "$SCEN" "${5:-0}" \
    "${6:-}" "${7:-}" "${8:-}" "$REQUIRE_EXTRAS"
}

mark_failed() { # mark_failed <目录> <名字> <原因>：结果挪进 failed\（加时间前缀，不覆盖以前的），然后停下
  local dir=$1 name=$2 why=$3 stamp f; stamp=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$dir/failed"
  for f in "$dir/$name".*; do [ -e "$f" ] && mv "$f" "$dir/failed/$stamp-$(basename "$f")"; done
  die "$name 不合格（$why），结果挪进 $dir/failed/（前缀 $stamp）。要不要重跑这一格由你定；再跑 run 会重跑它"
}

show_smoke() {
  python -I - "$OUT/smoke" <<'EOF'
import glob, json, sys
sys.stdout.reconfigure(encoding="utf-8")
for p in sorted(glob.glob(sys.argv[1] + "/*.json")):
    d = json.load(open(p, encoding="utf-8"))
    print(p.replace("\\", "/").split("/")[-1])
    for t in d["tasks"]:
        rag = f"官方状态 {t.get('ragStatus')}，" if t.get("ragStatus") else (f"失败（{t.get('failKind')}），" if t.get("failKind") else "")
        print(f"    {t['id']}：{rag}检索词 {t.get('queries')}，检索到 {t.get('hits')} 条；回答：{(t.get('answer') or t.get('error') or '').strip()[:200]!r}")
EOF
}

smoke() {
  local layout
  say "DeepSeek 冒烟：${SMOKE_LAYOUTS[*]} 各 3 题（并发 1）"
  for layout in "${SMOKE_LAYOUTS[@]}"; do cell "$OUT/smoke" "smoke-$layout" "$layout" 1 3 1 smoke r1 0 2 "$SMOKE_MODEL_FAIL"; done
  show_smoke
}

formal() { # formal [题数] [热身]：按 FORMAL 的顺序（A B B A、A B C C B A 等对称顺序）；每格开之前账本至少剩 MIN_CELL_CNY 元
  local count=${1:-$COUNT} warm=${2:-$WARMUP} spec layout round conc
  for spec in "${FORMAL[@]}"; do
    IFS=: read -r layout round conc <<< "$spec"
    # 程序或网络出错最多 2%（200 题 4 题、500 题 10 题）；“模型输出不合格”的上限按这一格的题数算
    # （演练 100 题时是 50，正式 200 题时是 100）
    cell "$OUT/raw" "$(cell_name "$spec")" "$layout" "${conc:-37}" "$count" "$warm" "$GROUP" "$(round_label "$spec")" \
      "$(( count * 2 / 100 ))" "$MIN_CELL_CNY" "$(( count * FORMAL_MODEL_FAIL_PCT / 100 ))"
  done
}

JUDGE_INCOMPLETE=0
check_cells() { # 计划里的每一格都在、都有验收标记，raw\ 里也没有别的结果（缺格时判分会少几组比较却显示完成）
  local spec name n hash
  for spec in "${FORMAL[@]}"; do
    name=$(cell_name "$spec")
    [ -f "$OUT/raw/$name.json" ] && [ -f "$OUT/raw/$name.ok" ] || die "缺 $name（或它的验收标记），不判分、不汇总；先跑完 bash scripts/real.sh run $PLAN"
    # 单独跑 judge 时也核对验收标记里的哈希：结果文件被换过就不判
    hash=$(sha256sum "$OUT/raw/$name.json" | cut -c1-64)
    grep -q "$hash" "$OUT/raw/$name.ok" || die "$name 的结果文件和验收标记对不上（被换过？），先人工核对 $OUT/raw"
    python -I src/cell_check.py extras "$OUT/raw/$name.json" "$REQUIRE_EXTRAS" || die "$name 的附加文件不完整（被删或被换过？），先人工核对 $OUT/raw"
  done
  n=$(find "$OUT/raw" -maxdepth 1 -name '*.json' | wc -l)
  [ "$n" = "${#FORMAL[@]}" ] || die "$OUT/raw 里有 $n 个结果文件，计划是 ${#FORMAL[@]} 格：先人工核对，多出来的挪走"
}

judge_and_report() { # judge_and_report [skip-judge]
  check_cells
  if [ "${1:-}" != skip-judge ]; then
    say "GPT-4o 判分"
    # 判分没做完（中转拒绝、判分失败、用 down 停下）时 judge.py 退出码非 0：照样汇总（汇总里会标明“判分没做完”），
    # 但最后不报“全部完成”，退出码也不是 0。
    python -I src/judge.py --key "$JUDGE_KEY_FILE" --cells "$OUT/raw" --lme "$LME_RAW" --out "$OUT/judge" --require-ok \
      --max-calls "$JUDGE_MAX_CALLS" --stop-file "$LOCK/stop" "${JUDGE_OPTS[@]}" | tee "$OUT/judge.log" \
      || { JUDGE_INCOMPLETE=1; say "判分没做完，见上面的原因；处理好以后再跑 bash scripts/real.sh judge $PLAN"; }
  fi
  say "汇总"
  python -I src/export.py "$OUT/raw" "$OUT" >/dev/null
  python -I src/real_report.py "$OUT" > "$OUT/report.md"
  # 详细分析（各步用时、各进程资源、模型请求、分题型、分歧来源等）。要判分结果的部分在演练时没有，生成不了不影响上面的汇总。
  if [ "$SCEN" = rag ]; then python -I src/rag_detail.py "$OUT" "$RAG_TASKS_HOST" > "$OUT/详细分析.md" 2> "$OUT/详细分析.err" || say "详细分析没生成完整（见 详细分析.err；不影响汇总）"
  else python -I src/real_detail.py "$OUT" > "$OUT/详细分析.md" 2> "$OUT/详细分析.err" || say "详细分析没生成完整（见 详细分析.err；不影响汇总）"; fi
  [ -s "$OUT/详细分析.err" ] || rm -f "$OUT/详细分析.err"
  local sheets=() s
  for s in "每格=$OUT/cells.csv" "拆开对比=$OUT/pairs.csv" "对Direct=$OUT/pairs-direct.csv" "合并对照=$OUT/pairs-memctx.csv" "模型和费用=$OUT/model.csv" "正确率=$OUT/judge/accuracy.csv" \
           "正确率对比=$OUT/judge/accuracy-pairs.csv" "每题判分=$OUT/judge/judged.csv"; do
    [ -f "${s#*=}" ] && sheets+=("$s")
  done
  python -I src/workbook.py "$OUT/结果汇总.xlsx" "${sheets[@]}" >/dev/null
  cat "$OUT/report.md"
}

down() {
  egress_down
  docker ps -a --filter name=sec1new-memctx- -q | xargs -r docker rm -f >/dev/null
  docker network rm "$NET" "$OUTNET" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------- 入口

# 运行锁：run、rehearse、judge 同一时间只能有一个（它们共用出口的容器名、网络别名和结果目录）。
# mkdir 是原子的，两个终端同时启动也只有一个拿得到。上一个进程已经不在了（崩溃）就收回。
LOCK=$SEC/_tmp/real-sh.lock
take_lock() {
  mkdir -p "$SEC/_tmp"
  if ! mkdir "$LOCK" 2>/dev/null; then
    local pid; pid=$(cat "$LOCK/pid" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then die "另一个 real.sh 正在跑（$(cat "$LOCK/what" 2>/dev/null)，进程 $pid）：等它结束，或在它的窗口按 Ctrl+C"; fi
    # 锁刚建好、进程号还没写进去（另一个终端正在启动）：1 分钟内的锁都当作有人在用，不收回。
    if [ -z "$pid" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then die "另一个 real.sh 正在启动（运行锁刚建好）：等它开始以后再看"; fi
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || die "拿不到运行锁 $LOCK"
  fi
  echo $$ > "$LOCK/pid"; echo "$1，$(date '+%F %T') 开始" > "$LOCK/what"; echo "${OUT:-}" > "$LOCK/out"
}
release_lock() { if [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$LOCK"; fi; }
# real.sh down 在运行锁里放的停止标记：放了以后不再开始新的付费步骤（GPT-4o 冒烟、出口、新的一格）
stopped_guard() { [ ! -f "$LOCK/stop" ] || die "已经用 real.sh down 停下了，不再开始新的步骤"; }

case "${1:-}" in
  check)
    OUT=${RESULTS:-$SEC/_tmp/real-check}
    check_all; say "检查都通过了，可以跑：bash scripts/real.sh run ${PLAN:-full-s4}" ;;
  run)
    need_plan run
    frozen_guard run
    OUT=$RESULTS UPSTREAM_HOST=api.deepseek.com
    say "这一轮：$PLAN；结果目录 $OUT；每格 $COUNT 题；正式各格 ${FORMAL[*]}；DeepSeek 上限 $LIMIT_CNY 元"
    # 定好的上限（全量两轮每轮 400 元，以前几轮 100 元）：只许调低，不许调高。
    awk -v x="$LIMIT_CNY" -v m="$LIMIT_MAX" 'BEGIN { exit !(x + 0 > 0 && x + 0 <= m + 0) }' || die "LIMIT_CNY=$LIMIT_CNY 不在 0 到 $LIMIT_MAX 元之间"
    take_lock run
    trap release_lock EXIT
    mkdir -p "$OUT"
    no_other_run
    check_all
    # 运行环境和设定（docs\31）。续跑时程序文件和第一次不一样就停下（ALLOW_CODE_CHANGE=1 才继续）。
    python -I src/run_info.py "$OUT" plan="$PLAN" formal="${FORMAL[*]}" smoke="${SMOKE_LAYOUTS[*]}" count="$COUNT" warmup="$WARMUP" \
      cores="$CORES" limit_cny="$LIMIT_CNY" min_cell_cny="$MIN_CELL_CNY" judge_max_calls="$JUDGE_MAX_CALLS" image="$IMAGE" \
      scenario_args="${SCEN_ARGS[*]}" store="$STORE_FILE" volume="$VOL" judge_ref="$LME_RAW" mode=run \
      || die "run_info.py 没通过（见上面；程序、数据或镜像在两次 run 之间变过的话，确认无误后加 ALLOW_CODE_CHANGE=1）"
    stopped_guard
    say "GPT-4o 判分冒烟（2 次请求）"
    python -I src/judge.py --smoke --key "$JUDGE_KEY_FILE" "${JUDGE_OPTS[@]}" || die "GPT-4o 判分冒烟没过"
    trap 'egress_down; redis_down; release_lock' EXIT
    stopped_guard
    if [ "$SCEN" = rag ]; then redis_up; fi
    egress_up "$KEY" outside
    smoke
    formal
    egress_down; redis_down; trap release_lock EXIT
    judge_and_report
    down; docker volume rm "$VOL" >/dev/null 2>&1 || true
    if [ "$JUDGE_INCOMPLETE" = 1 ]; then die "模型部分跑完了，但判分没做完（见上面的原因和 report.md 里的标注）；处理好以后跑 bash scripts/real.sh judge $PLAN"; fi
    say "全部完成。结果在 $OUT（先看 report.md）" ;;
  judge)
    need_plan judge
    frozen_guard judge
    OUT=$RESULTS
    take_lock judge
    trap release_lock EXIT
    judge_and_report
    [ "$JUDGE_INCOMPLETE" = 0 ] || die "判分没做完，见上面的原因" ;;
  rehearse)
    need_plan rehearse
    OUT=$SEC/_tmp/scratch/memctx/real-rehearsal-$PLAN UPSTREAM_HOST=fake:8080
    take_lock rehearse
    trap release_lock EXIT
    no_other_run
    rm -rf "$OUT"; mkdir -p "$OUT"
    ensure_infra
    say "离线演练：假模型当上游，假密钥，不联网"
    # 演练一律用演练专用的假前缀和假密钥（不用 local.env 里真的前缀，免得它进演练目录的文件）。
    # 假模型按字数算的“费用”很大，演练账本上限放到 10 万元。
    export DEEPSEEK_KEY_PREFIX=sk-rehearse
    LIMIT_CNY=${REHEARSE_LIMIT_CNY:-100000}
    # 每格题数、热身：全量两轮默认 500 题、热身 20（全部题在每种布局下走一遍）；以前几轮默认 100 题、热身 10。
    # REHEARSE_FORMAL 可以只演练计划里的几格（例如 "mono:r1 direct:r1"）。
    if [ "$COUNT" = 500 ]; then R_COUNT=${REHEARSE_COUNT:-500} R_WARM=$WARMUP; else R_COUNT=${REHEARSE_COUNT:-100} R_WARM=10; fi
    if [ -n "${REHEARSE_FORMAL:-}" ]; then read -r -a FORMAL <<< "$REHEARSE_FORMAL"; fi
    python -I src/run_info.py "$OUT" plan="$PLAN" formal="${FORMAL[*]}" smoke="${SMOKE_LAYOUTS[*]}" count="$R_COUNT" warmup="$R_WARM" \
      cores="$CORES" limit_cny="$LIMIT_CNY" min_cell_cny="$MIN_CELL_CNY" judge_max_calls="$JUDGE_MAX_CALLS" image="$IMAGE" \
      scenario_args="${SCEN_ARGS[*]}" store="$STORE_FILE" volume="$VOL" judge_ref="$LME_RAW" mode=rehearse || die "run_info.py 没通过"
    docker rm -f "$FAKE" >/dev/null 2>&1 || true
    docker run -d --name "$FAKE" --network "$NET" --network-alias fake --cpuset-cpus "$EGRESS_CORES" --memory 2g \
      --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe --entrypoint /usr/local/bin/node "$IMAGE" --no-warnings src/fake-model.ts --port 8080 >/dev/null
    printf 'BFCL_API_KEY=%sOFFLINEREHEARSAL000\n' "$DEEPSEEK_KEY_PREFIX" > "$OUT/fake.keys"
    trap 'egress_down; redis_down; docker rm -f "$FAKE" >/dev/null 2>&1 || true; release_lock' EXIT
    if [ "$SCEN" = rag ]; then redis_up; fi
    egress_up "$OUT/fake.keys" internal --upstream http://fake:8080/v1
    smoke
    formal "$R_COUNT" "$R_WARM"
    egress_down; redis_down
    judge_and_report skip-judge
    # 严格核对：假模型的回复固定，各格每题每一步的请求、每题结果、rag 报告都应当逐字相同
    python -I src/cell_check.py rehearsal "$OUT" | tee "$OUT/rehearsal-check.txt" \
      || die "离线演练跑完了，但各格不一致（见上面和 $OUT/rehearsal-check.txt）"
    say "离线演练完成，结果在 $OUT" ;;
  down)
    # 正在跑的那一轮把结果目录记在运行锁里；出口的最后状态写到那里。
    OUT=$(cat "$LOCK/out" 2>/dev/null || true); OUT=${OUT:-$SEC/_tmp/real-down}
    # 判分在宿主机上跑，删容器停不了它：在运行锁里放一个停止标记，判分每发一个请求前都看一眼。
    if [ -d "$LOCK" ]; then touch "$LOCK/stop"; fi
    down; say "已停（正在判分的话，手上这几段判完就停，不再发新的请求）" ;;
  *)
    sed -n '2,20p' "$0"; exit 2 ;;
esac
