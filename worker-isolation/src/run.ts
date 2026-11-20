// 编排器入口：阶段一（单题测量）和阶段三（端到端）共用。docs\16 第 4 节。
//
// 阶段一 --mode probe：只用 Monolithic，一题接一题（并发 1），模型延迟 0。先跑 --reps 遍计时：每次节点调用记墙钟、
//   主线程忙的时间（事件循环利用率）、主线程 CPU。再单独跑一遍量 CONTEXT、MEMORY 调用的入参、返回序列化后的字节数和
//   序列化、解析各要多久（拆开后这些要过 HTTP）。量字节会产生大量垃圾、引起 GC，所以不和计时放在同一遍。
// 阶段三 --mode e2e：按 --layout 把 MEMORY / CONTEXT 放在本进程或远端（官方 HTTP 传输），--concurrency 个任务同时跑，
//   前 --warmup 题热身不计。记每题用时、各节点往返用时、各进程和容器的资源读数、假模型收到的请求体哈希。
//   有任何一题失败，进程以非零退出（结果照样写出，标明失败数）。
//
//   node src/run.ts --mode probe|e2e --scenario s1-qa|s1-write|s2|s3|s3-full|s4|rag --tasks <jsonl> --count 200 --out /out/x.json
//     [--layout mono|direct|split-memory|split-context|split-both|split-memctx|split-interaction|split-interaction-memory]
//     [--concurrency 16] [--warmup 20] [--reps 3] [--distinct N]
//   布局见下面的 LAYOUTS：split-memctx 是 MEMORY 和 CONTEXT 放进同一个独立进程（docs\23）；split-interaction* 只用于 rag（docs\24）；
//   direct 不经过 Ditto 的运行时，同样的步骤写成普通函数在本进程里直接调用（direct.ts，docs\31），只用于 S4 和 rag 的阶段三。
//   阶段三另写两个文件（docs\31：全量实验的记录要全面）：<out>.calls.json.gz 每次调用的原始用时（本进程的和被拆出进程的，calls.ts），
//   rag 时 <out>.reports.jsonl.gz 每题完整的官方报告；结果里另有每 5 秒一次的资源读数（samples）。两个文件的摘要记在 meta.extras。
//   rag：官方 rag-qa 流程（rag-a\ 的副本），--tasks 用 rag-a-first200.jsonl（切法 (a)；切法 (b) 是 rag-lme-first200.jsonl），
//     --db 用对应的内部知识库，必须 --context-store redis。真实模型时把官方报告转成判分用的文字（rag.ts 的 ragAnswerText）。
//     [--db /store/users-100.sqlite] [--context-store memory|redis] [--select default|bm25]
//     [--fake http://fake:8080/v1] [--memory-url http://memory:7001] [--context-url http://context:7002]
//     [--keyword-ms 1000 --answer-ms 2000 --sigma 0.5 --seed s1-r1] [--label ...]
import { createHash } from "node:crypto";
import { writeFileSync, existsSync, readFileSync, renameSync } from "node:fs";
import { basename } from "node:path";
import { performance } from "node:perf_hooks";
import { gzipSync } from "node:zlib";
import { createDitto, createHttpTransport, type DittoRuntime, type InvokeTransport } from "@codesoul-co/ditto/runtime";
import type { WorkerDefinition } from "@codesoul-co/ditto/worker";
import type { NodeType } from "@codesoul-co/ditto/contracts";
import { openRedisContext } from "../official/examples/_shared/tools/storage/redis-context.ts";
import {
  CONTEXT_NODES, MEMORY_NODES, contextWorker, inferWorker, memoryWorker, runtimeConfig, taskLoop, workingStore,
  type ContextStoreKind, type Job, type LbTask, type LmeTask, type Scenario, type SelectKind,
} from "./app.ts";
import { MODEL, REAL, arg, round3 } from "./common.ts";
import { runRagLoop } from "../rag-a/examples/patterns/rag-qa/index.ts";
import {
  INTERACTION_NODES, RETRIEVAL_NODES, RagDispatch, ragAnswerText, ragContextWorker, ragFailKind, ragInput, ragInteractionWorkers, ragMemoryWorker,
  ragSandbox, type GraphMark,
} from "./rag.ts";
import type { RagTask } from "./rag-sources.ts";
import { loopDelay, resetLoopDelay, snapshot, window, type Snapshot } from "./stats.ts";
import { MAX_BODY_BYTES } from "./limits.ts";
import { CallLog, LOCAL, REMOTE, execKey, jobContext } from "./calls.ts";
import { createDirectRag, createDirectS4 } from "./direct.ts";

const mode = arg("mode") as "probe" | "e2e";
const scenario = arg("scenario") as Scenario;
const layout = arg("layout", "mono");
const out = arg("out");
const count = Number(arg("count", "200"));
const warmup = Number(arg("warmup", mode === "probe" ? "2" : "20"));
const concurrency = mode === "probe" ? 1 : Number(arg("concurrency", "16"));
const reps = Number(arg("reps", "1"));
const select = arg("select", "default") as SelectKind;
const contextStore = arg("context-store", "memory") as ContextStoreKind;
const fake = arg("fake", "http://fake:8080/v1");
const tag = basename(out, ".json");
if (existsSync(out)) throw new Error(`输出文件已存在：${out}`);

// 每种布局拆出去哪几个进程；每个进程里放哪几类 Worker。interaction = INTERACTION + RETRIEVAL（官方 rag-qa 里两者共用一份题库，
// 总在同一个进程里，docs\24 第 3 节）；memctx = MEMORY 和 CONTEXT 放进同一个独立进程（docs\23）。
type Kind = "memory" | "context" | "interaction";
const GROUPS: Record<string, { kinds: Kind[]; url: string }> = {
  memory: { kinds: ["memory"], url: "http://memory:7001" },
  context: { kinds: ["context"], url: "http://context:7002" },
  memctx: { kinds: ["memory", "context"], url: "http://memctx:7003" },
  interaction: { kinds: ["interaction"], url: "http://interaction:7004" },
};
const LAYOUTS: Record<string, string[]> = {
  mono: [], direct: [], "split-memory": ["memory"], "split-context": ["context"], "split-both": ["memory", "context"], "split-memctx": ["memctx"],
  "split-interaction": ["interaction"], "split-interaction-memory": ["interaction", "memory"],
};
if (!LAYOUTS[layout]) throw new Error(`没有这个布局 ${layout}`);
const remotes = LAYOUTS[layout]!.map(name => ({ name, kinds: GROUPS[name]!.kinds, url: arg(`${name}-url`, GROUPS[name]!.url) }));
const remoteKinds = new Set(remotes.flatMap(r => r.kinds));
const rag = scenario === "rag";
const direct = layout === "direct";
const uses: Record<Kind, boolean> = { memory: ["s1-qa", "s1-write", "s4", "rag"].includes(scenario), context: scenario !== "s1-write", interaction: rag };
for (const k of remoteKinds) if (!uses[k]) throw new Error(`${scenario} 不用这个布局 ${layout}`);
if (rag && contextStore !== "redis") throw new Error("rag 场景照官方用 Redis 放 CONTEXT：加 --context-store redis");
// rag 只跑 docs\24 定的三种布局和 Direct（拆 CONTEXT 时官方 restoreContextPlan 的错误类型判断跨不过 HTTP）。
if (rag && !["mono", "direct", "split-interaction", "split-interaction-memory"].includes(layout)) throw new Error(`rag 不跑布局 ${layout}`);
// Direct 只写了 S4 和 rag（docs\31）；S4 的 Direct 只用默认挑选（真实模型的各轮都是默认）。阶段一（--mode probe）也可以量 Direct：
// 离线、并发 1 地量每题主线程忙多久，和 Monolithic 比就是 Ditto 运行时本身的代价。
if (direct && (!["s4", "rag"].includes(scenario) || select !== "default")) throw new Error("direct 只用于 S4、rag（默认挑选）");
if (mode === "probe" && !["mono", "direct"].includes(layout)) throw new Error("阶段一只量 Monolithic 和 Direct");
const timeout = () => AbortSignal.timeout(30_000);

// ---------------------------------------------------------------- 题目

/**
 * 题目（docs\31）：整个文件读成一块 Buffer（在 V8 的堆外），按 "\n" 切成每题一段，派题时才解析这一题，做完就成了垃圾。
 * 以前开头就把全部题解析成对象常驻内存：500 题时编排器的堆约 0.9 GB（200 题时约 0.24 GB），垃圾回收要扫的对象多得多，
 * 编排器的负担随题数变。现在常驻的只有堆外的原始字节，同时在做的几十题才有解析出的对象。
 * 只按 "\n" 分（和 common.ts 的 jsonLines 相同：JSON.stringify 不转义 U+2028）。
 */
function readTaskLines(file: string, limit: number): Buffer[] {
  const buf = readFileSync(file), out: Buffer[] = [];
  const blank = (b: Buffer) => b.every(c => c === 0x20 || c === 0x09 || c === 0x0d);
  for (let start = 0; start < buf.length && out.length < limit;) {
    let end = buf.indexOf(0x0a, start);
    if (end < 0) end = buf.length;
    const line = buf.subarray(start, end);
    if (!blank(line)) out.push(line);
    start = end + 1;
  }
  return out;
}
const loadStart = performance.now();
const lines = readTaskLines(arg("tasks"), Number(arg("distinct", "100000")));
if (!lines.length) throw new Error("没有题目");
// S1 问答检索的是库里已有的用户：题目文件按固定顺序排列，规模 N 的库只含前 N 个用户。
if (scenario === "s1-qa") {
  const users = Number(/users-(\d+)\.sqlite$/.exec(arg("db"))?.[1]);
  if (!(lines.length <= users)) throw new Error(`S1 问答的题目（${lines.length} 个用户）超出记忆库（${users} 个用户），用 --distinct ${users}`);
}
const total = mode === "probe" ? warmup + count : count;
// rag 的 e2e：同一道题同时跑两次时，按题分派会把前一次收尾（rag.ts），所以不许题目循环。阶段一是一题接一题，不受影响。
if (rag && mode === "e2e" && count > lines.length) throw new Error(`rag 的 e2e 题数 ${count} 超过不同的题 ${lines.length}`);
/** 第 i 次任务：题目按文件顺序取，不够时循环。key 带上序号，同一题第二次出现也是新的会话、新的命名空间。 */
function jobOf(i: number): Job {
  const t = JSON.parse(lines[i % lines.length]!.toString("utf8"));
  const key = `${i}:${t.id}`;
  const base = { scenario, key, run: key, tag };
  return scenario === "s2" ? { ...base, lb: t as LbTask, select } : rag ? { ...base, rag: t as RagTask } : { ...base, lme: t as LmeTask, select };
}
const loadMs = performance.now() - loadStart;

// ---------------------------------------------------------------- 计量

interface NodeRow { job: number; rep: number; node: string; wall_ms: number; busy_ms: number; thread_cpu_ms: number;
  in_bytes?: number; in_stringify_ms?: number; in_parse_ms?: number; out_bytes?: number; out_stringify_ms?: number; out_parse_ms?: number }
const nodeRows: NodeRow[] = [];
// 阶段三：每个节点每次调用的用时（本进程的 Worker 是执行时间，远端的是从发出到收到，Direct 是直接调用的用时），
// 原样记进 calls（calls.ts），汇总时只取稳态窗口内的调用。
let currentJob = -1, currentRep = -1, transferPass = false, measureOverheadMs = 0;
function transfer(value: unknown) {
  const t0 = performance.now(); const text = JSON.stringify(value); const t1 = performance.now(); JSON.parse(text); const t2 = performance.now();
  return { bytes: Buffer.byteLength(text), stringify: t1 - t0, parse: t2 - t1 };
}
const calls = new CallLog();
/** 给 Worker 的执行入口套一层计时；不改节点本身。 */
function instrument(def: WorkerDefinition, crossesProcess: boolean): WorkerDefinition {
  return {
    type: def.type, capabilities: def.capabilities, ...(def.nodeTypes ? { nodeTypes: def.nodeTypes } : {}),
    ...(def.concurrency === undefined ? {} : { concurrency: def.concurrency }),
    instantiate() {
      const inner = def.instantiate();
      return {
        async execute(node, input, services) {
          if (mode === "e2e") return calls.time(node, () => inner.execute(node, input, services), LOCAL);
          const e0 = performance.eventLoopUtilization(), c0 = process.threadCpuUsage(), t0 = performance.now();
          let output: unknown;
          try { output = await inner.execute(node, input, services); return output; }
          finally {
            const wall = performance.now() - t0, elu = performance.eventLoopUtilization(e0), c = process.threadCpuUsage(c0);
            const row: NodeRow = { job: currentJob, rep: currentRep, node, wall_ms: wall, busy_ms: elu.active, thread_cpu_ms: (c.user + c.system) / 1000 };
            if (crossesProcess && transferPass) {
              const m0 = performance.now();
              const a = transfer(input), b = output === undefined ? { bytes: 0, stringify: 0, parse: 0 } : transfer(output);
              Object.assign(row, { in_bytes: a.bytes, in_stringify_ms: a.stringify, in_parse_ms: a.parse, out_bytes: b.bytes, out_stringify_ms: b.stringify, out_parse_ms: b.parse });
              measureOverheadMs += performance.now() - m0;
            }
            nodeRows.push(row);
          }
        },
        ...(inner.dispose ? { dispose: () => inner.dispose!() } : {}),
      };
    },
  };
}
/** 阶段三：远端调用的往返用时（官方 HTTP 传输外面套一层计时，不改传输）。记下执行标识，和被拆出进程那边的执行对得上（calls.ts）。 */
function timed(transport: InvokeTransport): InvokeTransport {
  return { id: transport.id, invoke: (envelope, options) => calls.time(envelope.node, () => transport.invoke(envelope, options), REMOTE, execKey(envelope.execution)) };
}

// ---------------------------------------------------------------- 运行时

const config = runtimeConfig(fake);
const token = process.env.DITTO_TRANSPORT_HTTP_WORKER_TOKEN ?? "";
const closers: (() => Promise<unknown>)[] = [];
let runtime: DittoRuntime | undefined;
let dispatch: RagDispatch | undefined;
let directS4: ReturnType<typeof createDirectS4> | undefined, directRag: ReturnType<typeof createDirectRag> | undefined;
if (direct) {
  // Direct：不建 Ditto 运行时和 Worker，本进程直接调用（direct.ts）。记忆库副本、Redis 和 Monolithic 相同。
  const redis = contextStore === "redis" ? await openRedisContext(arg("redis-url", "redis://redis:6379")) : undefined;
  if (rag) { directRag = createDirectRag({ config, db: workingStore(arg("db")), redis: redis!, tasksFile: arg("tasks"), calls }); closers.push(directRag.close); }
  else { directS4 = createDirectS4({ config, db: workingStore(arg("db")), contextStore, ...(redis ? { redis } : {}), calls }); closers.push(directS4.close); }
  if (redis) closers.push(() => redis.quit());
} else {
  const workers: WorkerDefinition[] = [instrument(inferWorker(), false)];
  if (uses.memory && !remoteKinds.has("memory")) {
    const memory = rag ? ragMemoryWorker(workingStore(arg("db")), config) : memoryWorker(workingStore(arg("db")));
    workers.push(instrument(memory.worker, true)); closers.push(memory.close);
  }
  if (uses.context && !remoteKinds.has("context")) {
    const redis = contextStore === "redis" ? await openRedisContext(arg("redis-url", "redis://redis:6379")) : undefined;
    workers.push(instrument(rag ? ragContextWorker(config, redis!) : contextWorker(contextStore, select, redis), true));
    if (redis) closers.push(() => redis.quit());
  }
  if (uses.interaction && !remoteKinds.has("interaction")) {
    dispatch = new RagDispatch(arg("tasks"));
    const w = ragInteractionWorkers(dispatch);
    workers.push(instrument(w.retrieval, true), instrument(w.interaction, true));
  }
  const transports: InvokeTransport[] = remotes.map(r =>
    timed(createHttpTransport({ id: `http-${r.name}`, url: `${r.url}/ditto/invoke`, token, timeoutMs: 600_000, maxBodyBytes: MAX_BODY_BYTES })));
  // rag：工具许可照官方 cli.ts（{ ...config.sandbox, tools }）；许可在 INTERACTION 执行的那个进程里检查，各布局都给。
  runtime = createDitto({ config, hostId: "orchestrator-host", processId: "orchestrator", workers, transports,
    ...(rag ? { sandbox: ragSandbox(config) } : {}) });
  const NODES: Record<Kind, [string, readonly NodeType[]][]> = {
    memory: [["MEMORY", MEMORY_NODES]], context: [["CONTEXT", CONTEXT_NODES]],
    interaction: [["INTERACTION", INTERACTION_NODES], ["RETRIEVAL", RETRIEVAL_NODES]],
  };
  for (const r of remotes) {
    for (const kind of r.kinds) {
      for (const [type, nodes] of NODES[kind]) {
        runtime.registerRemote({
          address: { workerId: `${type.toLowerCase()}-remote`, workerType: type, hostId: `${r.name}-host`, processId: `${r.name}-proc` },
          capabilities: [...nodes], transportId: `http-${r.name}`,
        });
      }
    }
  }
}

// ---------------------------------------------------------------- 假模型、远端读数

const fakeBase = new URL(fake).origin;
async function resetFake() {
  const q = new URLSearchParams({ seed: arg("seed", "r0"), "keyword-ms": arg("keyword-ms", mode === "probe" ? "0" : "1000"),
    "answer-ms": arg("answer-ms", mode === "probe" ? "0" : "2000"), sigma: arg("sigma", "0.5") });
  const res = await fetch(`${fakeBase}/reset?${q}`, { method: "POST", signal: timeout() }); if (!res.ok) throw new Error("fake reset failed");
}
const message = (error: unknown) => String((error as Error)?.message ?? error).slice(0, 300);
/** 取一个 JSON：检查 HTTP 状态；tries 次，每次之间等 2 秒。 */
async function fetchJson(url: string, method = "GET", tries = 1): Promise<any> {
  let last: unknown;
  for (let k = 0; k < tries; k++) {
    try {
      const res = await fetch(url, { method, signal: timeout() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (error) { last = error; if (k + 1 < tries) await new Promise(r => setTimeout(r, 2000)); }
  }
  throw last;
}
/**
 * 远端读数：逐个进程取，检查 HTTP 状态；/stats 还要有 snapshot。有一个进程取不到就整体记成 { error }（窗口无效），
 * 不让未处理的拒绝把编排器带崩。
 */
const remoteStats = (path = "/stats", method = "GET", tries = 1): Promise<Record<string, any>> => Promise.all(remotes.map(async r => {
  const j = await fetchJson(r.url + path, method, tries);
  if (path === "/stats" && !j?.snapshot) throw new Error(`${r.name} 的读数不完整`);
  return [r.name, j] as const;
})).then(Object.fromEntries, error => ({ error: message(error) }));

/** 每 5 秒一次的资源读数（docs\31）：本进程的 snapshot，加上被拆出各进程的 /stats。远端读不到时记下错误，不影响跑题。 */
const samples: { at_ms: number; local: Snapshot; remote: Record<string, any> }[] = [];
let sampling: ReturnType<typeof setInterval> | undefined;
const pendingSamples = new Set<Promise<void>>();
function startSampling() {
  const take = () => {
    const local = snapshot();
    const p = (remotes.length ? remoteStats() : Promise.resolve({})).then(remote => { samples.push({ at_ms: local.at_ms, local, remote }); });
    pendingSamples.add(p); void p.finally(() => pendingSamples.delete(p));
  };
  take();
  sampling = setInterval(take, 5000);
}
async function stopSampling() {
  if (sampling) clearInterval(sampling);
  await Promise.all(pendingSamples);
  samples.sort((a, b) => a.at_ms - b.at_ms);
}

// ---------------------------------------------------------------- 跑

interface TaskRow { i: number; id: string; start_ms: number; end_ms: number; ok: boolean; error?: string;
  busy_ms?: number; thread_cpu_ms?: number; measure_ms?: number; rep?: number; heap_mb?: number;
  /** 真实模型（MEMCTX_REAL=1）时才记：模型的回答（判分用）、结束原因、检索词、检索到的记忆条数。 */
  answer?: string; finishReason?: string; queries?: string[]; hits?: number;
  /** rag 场景：官方报告里的回答状态、证据块数、结论句数；失败时是哪一类（rag.ts 的 ragFailKind）。 */
  ragStatus?: string; evidence?: number; claims?: number; failKind?: "model-output" | "infra" }
const tasks: TaskRow[] = [];
/** rag：每题完整的官方报告（一行一题，写进 <out>.reports.jsonl.gz）。跑题时只留对象，题都跑完以后才转成文字（不占窗口里的主线程）。 */
const reports: { i: number; id: string; report: unknown }[] = [];
await resetFake();
const started = new Date().toISOString();

async function runOne(i: number, rep?: number, suffix?: string): Promise<TaskRow> {
  // 先解析这一题（不算进这一题的用时）；阶段一每遍用新的执行身份（suffix），会话和命名空间都是新的。
  const base = jobOf(i), job = suffix === undefined ? base : { ...base, run: `${base.key}${suffix}` };
  const row: TaskRow = { i, id: job.lme?.id ?? job.lb?.id ?? job.rag!.id, start_ms: performance.now(), end_ms: 0, ok: false, ...(rep === undefined ? {} : { rep }) };
  const e0 = performance.eventLoopUtilization(), c0 = process.threadCpuUsage(), m0 = measureOverheadMs;
  // rag：最近一张图（runtime.loop 公开的 onGraph 选项；Direct 由 direct.ts 按官方的图名报），失败时据此分类（rag.ts 的 ragFailKind）。
  // 各布局相同，都在编排器里记。
  let last: GraphMark | undefined;
  try {
    // rag：官方 rag-qa 的完整流程（runRagLoop，和官方 runRag 一样调用 runtime.loop），直到 rag_publish。
    // jobContext：让这一题里的每次调用记上题的序号（calls.ts）。
    const result: any = await jobContext.run(i, () => rag
      ? directRag
        ? directRag.run(ragInput(job.run, job.rag!, MODEL), m => { last = m; })
        : runtime!.loop(runRagLoop, [ragInput(job.run, job.rag!, MODEL), {}], { onGraph: e => { last = { graphId: e.graphId, status: e.status }; } })
      : directS4 ? directS4.run(job) : runtime!.loop(taskLoop, job));
    row.ok = true;
    if (rag && result) {
      Object.assign(row, { ragStatus: result.answer?.status, evidence: result.evidence?.length, claims: result.answer?.claims?.length });
      if (mode === "e2e") reports.push({ i, id: row.id, report: result });
    }
    // 真实模型：rag 的官方报告转成判分用的文字（rag.ts 的 ragAnswerText）；检索词取官方计划里的 queries，“检索到的条数”记证据块数。
    if (REAL && result) Object.assign(row, rag
      ? { answer: ragAnswerText(result), queries: result.plan?.queries, hits: result.evidence?.length }
      : { answer: result.answer, finishReason: result.finishReason, queries: result.queries, hits: result.hits });
  } catch (error) {
    row.error = String((error as Error)?.message ?? error).slice(0, 400);
    if (rag) row.failKind = ragFailKind(error, last);
  }
  row.end_ms = performance.now();
  if (mode === "probe") {
    const c = process.threadCpuUsage(c0);
    row.busy_ms = performance.eventLoopUtilization(e0).active; row.thread_cpu_ms = (c.user + c.system) / 1000; row.measure_ms = measureOverheadMs - m0;
    row.heap_mb = process.memoryUsage().heapUsed / 2 ** 20;
  }
  return row;
}

type Mark = { local: Snapshot; loop?: unknown; remote: Promise<Record<string, any>> };
let t1: Mark | undefined, t2: Mark | undefined;
if (mode === "probe") {
  for (let i = 0; i < warmup; i++) { currentJob = -1 - i; await runOne(i, undefined, "#warm"); }
  nodeRows.length = 0;
  // 计时遍（rep 0..reps-1），再加一遍只量跨进程字节（rep = reps）。每遍用新的会话和命名空间。
  for (let rep = 0; rep <= reps; rep++) {
    transferPass = rep === reps; currentRep = rep;
    for (let i = warmup; i < total; i++) { currentJob = i; tasks.push(await runOne(i, rep, `#r${rep}`)); }
  }
} else {
  startSampling();
  let next = 0, done = 0;
  const lane = async () => {
    while (next < count) {
      const i = next++;
      // 最后一题派发的时刻：本进程读数立即取，远端读数异步取，不耽误派发。
      if (next === count) t2 = { local: snapshot(), loop: loopDelay(), remote: remoteStats("/stats", "GET", 3) };
      const row = await runOne(i);
      tasks.push(row);
      if (++done === warmup) {
        resetLoopDelay();
        // 远端先复位事件循环延迟的直方图（复位失败记下来：那样远端的延迟读数含热身），再取读数
        t1 = { local: snapshot(), remote: remotes.length
          ? remoteStats("/stats/reset", "POST").then(reset => remoteStats("/stats", "GET", 3).then(s => (reset.error ? { ...s, resetError: reset.error } : s)))
          : remoteStats() };
      }
    }
  };
  await Promise.all(Array.from({ length: concurrency }, lane));
  await stopSampling();
}
const finished = new Date().toISOString();
// 收尾：题都跑完以后，取读数、关闭各项都不能让异常把已经跑完的题丢掉——取不到的记下错误，结果文件照样写出，
// 由 real.sh 验收决定这一格算不算。
const fakeStats = await fetchJson(`${fakeBase}/stats`, "GET", 3)
  .catch(error => ({ error: message(error), settings: null, rows: [], peakActive: null, errors: null, realModelCalls: null }));
const r1 = t1 ? await t1.remote : undefined, r2 = t2 ? await t2.remote : undefined;
// 被拆出进程里每次调用的用时（worker-server.ts 的 /stats/calls）：题都跑完以后逐个进程取（各自重试，取不到的单独记错误，不连累别的进程）。
const remoteCalls: Record<string, unknown> = mode === "e2e" ? Object.fromEntries(await Promise.all(remotes.map(async r => {
  try {
    const j = await fetchJson(`${r.url}/stats/calls`, "GET", 3);
    if (!Array.isArray(j?.nodes) || !Array.isArray(j?.rows)) throw new Error("内容不对");
    return [r.name, j] as const;
  } catch (error) { return [r.name, { error: message(error) }] as const; }
}))) : {};
const closeErrors: string[] = [];
const safely = async (what: string, f: () => unknown) => { try { await f(); } catch (error) { closeErrors.push(`${what}：${message(error)}`); } };
await safely("运行时", () => runtime?.close());
for (const [k, close] of closers.entries()) await safely(`关闭第 ${k + 1} 项`, close);
await safely("RagDispatch", () => dispatch?.close());

// ---------------------------------------------------------------- 汇总

const pct = (xs: number[], p: number) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor((s.length - 1) * p))]! : null; };
const failed = tasks.filter(t => !t.ok).length;
const result: any = {
  meta: { mode, scenario, layout, concurrency, count, warmup, reps, select, contextStore, label: arg("label", ""), tag, real: REAL,
    tasksFile: arg("tasks"), db: uses.memory ? arg("db", "") : null, distinctTasks: lines.length, loadMs: round3(loadMs),
    started, finished, node: process.version, pid: process.pid, fakeSettings: fakeStats.settings, failedTasks: failed,
    argv: process.argv.slice(2),
    // 时间基准：tasks 的 start_ms、end_ms 和 window 的 from_ms、to_ms 都是相对它的毫秒；calls、samples 是 Unix 毫秒
    timeOrigin: performance.timeOrigin,
    ...(fakeStats.error ? { fakeStatsError: fakeStats.error } : {}), ...(closeErrors.length ? { closeErrors } : {}) },
  tasks,
  fake: { rows: fakeStats.rows, peakActive: fakeStats.peakActive, errors: fakeStats.errors, realModelCalls: fakeStats.realModelCalls },
};
if (mode === "probe") result.nodes = nodeRows;
else {
  result.samples = samples;
  const remoteOk = !r1?.error && !r2?.error;
  if (t1 && t2 && t1.local.at_ms < t2.local.at_ms && remoteOk) {
    const summarize = (kind: number) => Object.fromEntries([...calls.summary(t1!.local.at_ms, t2!.local.at_ms, kind)].map(([node, xs]) =>
      [node, { calls: xs.length, mean_ms: xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null, p50_ms: pct(xs, 0.5), p95_ms: pct(xs, 0.95), max_ms: xs.length ? Math.max(...xs) : null }]));
    // 一种布局里一个节点要么在本进程、要么在远端，按节点名合起来（和以前的 nodeWalls 相同）。
    result.nodeWalls = { ...summarize(LOCAL), ...summarize(REMOTE) };
    const from = t1.local.at_ms - performance.timeOrigin, to = t2.local.at_ms - performance.timeOrigin;
    const inWindow = tasks.filter(t => t.end_ms >= from && t.end_ms <= to);
    // 吞吐只计成功的题；窗口里失败的单列。
    const ok = inWindow.filter(t => t.ok);
    result.window = {
      from_ms: from, to_ms: to, from_unix_ms: t1.local.at_ms, to_unix_ms: t2.local.at_ms, completed: ok.length, failed: inWindow.length - ok.length,
      throughput_per_s: ok.length / ((t2.local.at_ms - t1.local.at_ms) / 1000),
      orchestrator: { ...window(t1.local, t2.local), loopDelay: t2.loop },
      remote: Object.fromEntries(remotes.map(r => [r.name, { ...window(r1![r.name].snapshot, r2![r.name].snapshot), loopDelay: r2![r.name].loopDelay }])),
      ...(r1?.resetError ? { remoteResetError: r1.resetError } : {}),
    };
  } else {
    // 窗口无效（顺序不对或远端读数失败）当错误处理。
    result.window = null; result.windowError = { t1: !!t1, t2: !!t2, ordered: !!(t1 && t2 && t1.local.at_ms < t2.local.at_ms), remote1: r1?.error ?? null, remote2: r2?.error ?? null };
    process.exitCode = 2;
  }
}
// 另外两个文件（docs\31）：先写好，摘要记进 meta.extras，最后才写结果文件——结果文件在，另外两个就一定完整。
// 被拆出进程的调用记录取不到时照样写（主结果不受影响），错误记在 remote 里这个进程名下，real.sh 验收时提示。
const extras: Record<string, { file: string; sha256: string; bytes: number }> = {};
const writeExtra = (suffix: string, text: string) => {
  const file = `${out.replace(/\.json$/, "")}.${suffix}`, gz = gzipSync(text);
  writeFileSync(`${file}.partial`, gz);
  renameSync(`${file}.partial`, file);
  extras[suffix] = { file: basename(file), sha256: createHash("sha256").update(gz).digest("hex"), bytes: gz.length };
};
if (mode === "e2e") {
  writeExtra("calls.json.gz", JSON.stringify({ note: "每行 [节点编号, 开始时刻（Unix 毫秒）, 用时（毫秒）, 题的序号（-1 是认不出）, 类别（0 本进程执行，1 经 HTTP 调远端的往返）, 跨进程的调用另有执行标识]",
    orchestrator: calls.toJSON(), remote: remoteCalls }));
  if (rag) writeExtra("reports.jsonl.gz", reports.map(r => JSON.stringify(r) + "\n").join(""));
}
result.meta.extras = extras;
// 先写临时文件再改名：中途被打断时不会留下半截的结果文件，被 real.sh 当成“已跑完”跳过。
writeFileSync(`${out}.partial`, JSON.stringify(result));
renameSync(`${out}.partial`, out);
console.log(JSON.stringify({ out, tasks: tasks.length, ok: tasks.length - failed, failed, firstError: tasks.find(t => !t.ok)?.error ?? null,
  throughput: result.window?.throughput_per_s ?? null }));
if (failed) process.exitCode ||= 1;
