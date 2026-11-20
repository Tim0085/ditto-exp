#!/usr/bin/env bash
# 03_memctx_probe 的全部容器命令（在 Git Bash 里跑）。docs\16 第 4 节。
#
#   bash scripts/run.sh infra-up | infra-down       建/删网络，起/停假模型和 Redis
#   bash scripts/run.sh build-store                 建记忆库（50/100/250/500 个用户）到 Docker 卷
#   bash scripts/run.sh build-rag-store [a|a500|b]  建 rag 场景的内部知识库（docs\24；a 是正式用的切法 (a) 前 200 题，a500 是全量 500 题（docs\31），默认 b）到同一个卷
#   bash scripts/run.sh probe <名字> <核> <run.ts 参数...>          阶段一：一个 Monolithic 容器，结果写 RAW_DIR/<名字>.json
#   bash scripts/run.sh e2e <名字> <布局> <核> <run.ts 参数...>     阶段三：按布局起被拆的 Worker 容器，再起编排器
#   bash scripts/run.sh cleanup                     删本实验的容器、网络、卷（镜像另删）
#
# 只复用已有镜像 3513d314a207（Node 24.14.0），不新建镜像；另用官方 redis:7-alpine。
# 容器都以 sec1new-memctx- 开头；网络是 --internal（不能出外网）。
set -euo pipefail
export MSYS_NO_PATHCONV=1

IMAGE=3513d314a207
# 目录从本脚本的位置推出来：本目录（03_memctx_probe）的上一级是实验根目录。Git Bash 的 pwd -W 给出 Docker 认得的 F:/... 写法。
PROBE=$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))
SEC=${PROBE%/*}
DATA=$SEC/bench_data
RAW_DIR=${RAW_DIR:-$SEC/_tmp/scratch/memctx/smoke}
NET=sec1new-memctx-net
VOL=sec1new-memctx-store
FAKE_CORES=${FAKE_CORES:-28-31}
REDIS_CORES=${REDIS_CORES:-26}

base=(--network "$NET" --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe
      --mount "type=bind,src=$DATA,dst=/data,readonly" --mount "type=volume,src=$VOL,dst=/store,readonly")
# 真实模型模式（docs\19）：MEMCTX_REAL=1 时传进编排器和被拆 Worker 的容器（scripts\real.sh 会设）。
if [ "${MEMCTX_REAL:-}" = 1 ]; then base+=(-e MEMCTX_REAL=1); fi

token() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

wait_ready() { # wait_ready <容器名>
  for _ in $(seq 1 240); do
    if docker logs "$1" 2>&1 | grep -q '"ready":true'; then return 0; fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" != "true" ]; then docker logs "$1" >&2 || true; return 1; fi
    sleep 0.5
  done
  echo "等待 $1 超时" >&2; return 1
}

case "${1:-}" in
  infra-up)
    docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET" >/dev/null
    docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null
    docker rm -f sec1new-memctx-fake sec1new-memctx-redis >/dev/null 2>&1 || true
    docker run -d --name sec1new-memctx-fake --network "$NET" --network-alias fake --cpuset-cpus "$FAKE_CORES" --memory 2g \
      --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe --entrypoint /usr/local/bin/node "$IMAGE" src/fake-model.ts --port 8080 >/dev/null
    docker run -d --name sec1new-memctx-redis --network "$NET" --network-alias redis --cpuset-cpus "$REDIS_CORES" --memory 4g \
      redis:7-alpine redis-server --save "" --appendonly no >/dev/null
    sleep 2; docker ps --filter name=sec1new-memctx- --format '{{.Names}} {{.Status}}' ;;
  infra-down)
    docker rm -f sec1new-memctx-fake sec1new-memctx-redis >/dev/null 2>&1 || true ;;
  build-store)
    # 写卷要 root（镜像默认用户 1000）。
    docker run --rm --network none --user 0 --cpuset-cpus 2 --memory 6g \
      --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe --mount "type=bind,src=$DATA,dst=/data,readonly" \
      --mount "type=volume,src=$VOL,dst=/store" --entrypoint /bin/sh "$IMAGE" -c \
      'node --max-old-space-size=4096 src/build-store.ts --tasks /data/longmemeval-s/derived/memctx-lme.jsonl --order /data/longmemeval-s/derived/memctx-lme-order.json --scales 50,100,250,500 --out /store && chmod 0444 /store/*.sqlite && ls -la /store' ;;
  build-rag-store)
    # rag 场景的内部知识库（docs\24）。第二个参数是切法（先跑 node src/prepare-rag.ts --cut <切法> 生成数据）：
    #   a（正式用）：rag-a-first200.knowledge.jsonl → /store/rag-a-first200.sqlite
    #   a500（全量 500 题，docs\31；先跑 node src/prepare-rag.ts --cut a ... --prefix rag-a-all500）：rag-a-all500.knowledge.jsonl → /store/rag-a-all500.sqlite
    #   b（默认，11-19 的离线测量）：rag-lme-first200.knowledge.jsonl → /store/rag-first200.sqlite
    case "${2:-b}" in
      a) data=rag-a-first200 store=rag-a-first200 ;;
      a500) data=rag-a-all500 store=rag-a-all500 ;;
      b) data=rag-lme-first200 store=rag-first200 ;;
      *) echo "切法是 a、a500 或 b，不是 $2" >&2; exit 2 ;;
    esac
    docker run --rm --network none --user 0 --cpuset-cpus 2 --memory 6g \
      --mount "type=bind,src=$PROBE,dst=/probe,readonly" --workdir /probe --mount "type=bind,src=$DATA,dst=/data,readonly" \
      --mount "type=volume,src=$VOL,dst=/store" --entrypoint /bin/sh "$IMAGE" -c \
      "node --max-old-space-size=4096 src/build-rag-store.ts --knowledge /data/longmemeval-s/derived/$data.knowledge.jsonl --out /store/$store.sqlite && chmod 0444 /store/$store.sqlite && ls -la /store/$store.sqlite" ;;
  probe)
    name=${2:?名字}; cores=${3:?核}; shift 3
    mkdir -p "$RAW_DIR"
    case " $* " in *" --context-store redis "*) docker exec sec1new-memctx-redis redis-cli FLUSHALL >/dev/null ;; esac
    docker run --rm --name "sec1new-memctx-probe-$name" "${base[@]}" --cpuset-cpus "$cores" --memory 6g --memory-swap 6g \
      --mount "type=bind,src=$RAW_DIR,dst=/out" --entrypoint /usr/local/bin/node "$IMAGE" --max-old-space-size=4096 \
      src/run.ts --mode probe --out "/out/$name.json" "$@" ;;
  e2e)
    name=${2:?名字}; layout=${3:?布局}; cores=${4:?核}; shift 4
    mkdir -p "$RAW_DIR"
    tok=$(token); started=()
    extra=(); for a in "$@"; do extra+=("$a"); done
    get() { local key=$1 def=$2 i; for ((i=0; i<${#extra[@]}; i++)); do [ "${extra[$i]}" = "--$key" ] && { echo "${extra[$((i+1))]}"; return; }; done; echo "$def"; }
    db=$(get db ""); store=$(get context-store memory); sel=$(get select default); scen=$(get scenario s4); tasks=$(get tasks "")
    # 记下被拆容器的日志和是否被系统杀掉（OOM），再删除。先设好再起容器，起到一半失败也不会残留。
    cleanup_workers() { for c in "${started[@]}"; do
      echo "OOMKilled=$(docker inspect -f '{{.State.OOMKilled}}' "$c" 2>/dev/null)" > "$RAW_DIR/$name.$c.log"
      docker logs "$c" >> "$RAW_DIR/$name.$c.log" 2>&1 || true; docker rm -f "$c" >/dev/null 2>&1 || true; done; }
    trap cleanup_workers EXIT
    [ "$store" = redis ] && docker exec sec1new-memctx-redis redis-cli FLUSHALL >/dev/null
    # 起一个被拆出的进程：worker <进程名=网络别名> <端口> <内存> <Worker 类型,...>。进程名、端口和 run.ts 的 GROUPS 一一对应。
    worker() {
      local group=$1 port=$2 mem=$3 types=$4 args=(--types "$4" --port "$2" --name "$1" --scenario "$scen")
      [ -n "$db" ] && args+=(--db "$db"); [ -n "$tasks" ] && args+=(--tasks "$tasks")
      args+=(--context-store "$store" --select "$sel")
      # 只放 MEMORY 的进程照以前不加堆上限（和 docs\18、20、25 的运行相同）；其余照以前加 4 GiB。
      local heap=(--max-old-space-size=4096); [ "$types" = memory ] && heap=()
      docker run -d --name "sec1new-memctx-$group-$name" "${base[@]}" --network-alias "$group" --cpuset-cpus "$cores" --memory "$mem" --memory-swap "$mem" \
        -e DITTO_TRANSPORT_HTTP_WORKER_TOKEN="$tok" --entrypoint /usr/local/bin/node "$IMAGE" "${heap[@]}" src/worker-server.ts "${args[@]}" >/dev/null
      started+=("sec1new-memctx-$group-$name")
    }
    # 各布局拆出去哪几个进程（和 run.ts 的 LAYOUTS 相同）。memctx：MEMORY 和 CONTEXT 放进同一个进程（docs\23）；
    # interaction：INTERACTION + RETRIEVAL（docs\24，只用于 rag）。direct：不经过 Ditto 的运行时，全部在编排器一个进程里（docs\31）。
    case "$layout" in
      mono|direct) ;;
      split-memory) worker memory 7001 4g memory ;;
      split-context) worker context 7002 6g context ;;
      split-both) worker memory 7001 4g memory; worker context 7002 6g context ;;
      split-memctx) worker memctx 7003 6g memory,context ;;
      split-interaction) worker interaction 7004 6g interaction ;;
      split-interaction-memory) worker interaction 7004 6g interaction; worker memory 7001 4g memory ;;
      *) echo "没有这个布局 $layout" >&2; exit 2 ;;
    esac
    for c in "${started[@]}"; do wait_ready "$c"; done
    docker run --rm --name "sec1new-memctx-orch-$name" "${base[@]}" --cpuset-cpus "$cores" --memory 6g --memory-swap 6g \
      -e DITTO_TRANSPORT_HTTP_WORKER_TOKEN="$tok" --mount "type=bind,src=$RAW_DIR,dst=/out" --entrypoint /usr/local/bin/node "$IMAGE" \
      --max-old-space-size=4096 src/run.ts --mode e2e --layout "$layout" --out "/out/$name.json" "$@" || { code=$?; echo "编排器退出码 $code（137 表示被系统杀掉）" >&2; exit $code; } ;;
  cleanup)
    docker ps -a --filter name=sec1new-memctx- -q | xargs -r docker rm -f >/dev/null
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true ;;
  *)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
