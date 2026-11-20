// 被拆出的 Worker 进程，用 Ditto 官方 HTTP 传输（createWorkerHttpHandler）接收调用。
// docs\16 阶段三；写法照 docs/worker-communication.zh-CN.md 的“服务端”示例。
//
//   node src/worker-server.ts --types memory|context|interaction[,...] --port 7001 [--name memory]
//        [--scenario s4|rag] [--db /store/users-100.sqlite] [--context-store memory|redis] [--redis-url redis://redis:6379]
//        [--select default|bm25] [--tasks <rag 题目文件>]
// --types 可以列几类，放进同一个进程（docs\23：memory,context）。interaction 是 INTERACTION + RETRIEVAL（官方 rag-qa 里两者共用一份题库，docs\24）。
// --name 是这个进程的名字（编排器登记远端时用的 hostId/processId 前缀），默认是几类名字连起来。
// --scenario rag：MEMORY、CONTEXT 照官方 rag-qa 的写法（见 rag.ts）；其余场景照原来的写法（app.ts）。
// 令牌从环境变量 DITTO_TRANSPORT_HTTP_WORKER_TOKEN 读。/stats 是本实验加的只读计量入口，不经过 Ditto。
// /stats/calls：这个进程里每次调用的执行用时（calls.ts，docs\31），编排器在题都跑完以后取一次。Worker 的执行入口外面套一层计时，
// 和编排器给本进程的 Worker 套的一样（run.ts 的 instrument），不改节点本身。
import { createServer } from "node:http";
import { createDitto, createWorkerHttpHandler } from "@codesoul-co/ditto/runtime";
import { openRedisContext } from "../official/examples/_shared/tools/storage/redis-context.ts";
import { contextWorker, memoryWorker, runtimeConfig, workingStore, type ContextStoreKind, type SelectKind } from "./app.ts";
import { RagDispatch, ragContextWorker, ragInteractionWorkers, ragMemoryWorker, ragSandbox } from "./rag.ts";
import { arg } from "./common.ts";
import { loopDelay, resetLoopDelay, snapshot } from "./stats.ts";
import { MAX_BODY_BYTES } from "./limits.ts";
import { CallLog, timedWorker } from "./calls.ts";

const types = arg("types").split(",");
for (const t of types) if (!["memory", "context", "interaction"].includes(t)) throw new Error(`不认识的 Worker 类型 ${t}`);
const port = Number(arg("port"));
const token = process.env.DITTO_TRANSPORT_HTTP_WORKER_TOKEN;
if (!token) throw new Error("Set DITTO_TRANSPORT_HTTP_WORKER_TOKEN");
const rag = arg("scenario", "s4") === "rag";

const name = arg("name", types.join("-"));
const config = runtimeConfig(arg("fake", "http://fake:8080/v1"));
const runtime = createDitto({ hostId: `${name}-host`, processId: `${name}-proc`, config,
  ...(types.includes("interaction") ? { sandbox: ragSandbox(config) } : {}) });
const closers: (() => Promise<unknown> | unknown)[] = [];
const calls = new CallLog();
if (types.includes("memory")) {
  // 每次运行从同一份库的副本开始（中途写入会改变库，检索结果按编号排序）。
  const memory = rag ? ragMemoryWorker(workingStore(arg("db")), config) : memoryWorker(workingStore(arg("db")));
  runtime.register(timedWorker(memory.worker, calls), "memory-remote");
  closers.push(memory.close);
}
if (types.includes("context")) {
  const kind = arg("context-store", "memory") as ContextStoreKind;
  const redis = kind === "redis" ? await openRedisContext(arg("redis-url", "redis://redis:6379")) : undefined;
  if (rag && !redis) throw new Error("rag 场景照官方用 Redis 放 CONTEXT：加 --context-store redis");
  runtime.register(timedWorker(rag ? ragContextWorker(config, redis!) : contextWorker(kind, arg("select", "default") as SelectKind, redis), calls), "context-remote");
  if (redis) closers.push(() => redis.quit());
}
if (types.includes("interaction")) {
  const dispatch = new RagDispatch(arg("tasks"));
  const w = ragInteractionWorkers(dispatch);
  runtime.register(timedWorker(w.retrieval, calls), "retrieval-remote");
  runtime.register(timedWorker(w.interaction, calls), "interaction-remote");
  closers.push(() => dispatch.close());
}
const handler = createWorkerHttpHandler(runtime, { token, maxBodyBytes: MAX_BODY_BYTES });
const server = createServer((req, res) => {
  if (req.url === "/stats" && req.method === "GET") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ snapshot: snapshot(), loopDelay: loopDelay() })); return;
  }
  if (req.url === "/stats/calls" && req.method === "GET") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ name, ...calls.toJSON() })); return;
  }
  if (req.url === "/stats/reset" && req.method === "POST") {
    resetLoopDelay(); res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); return;
  }
  handler(req, res);
});
server.keepAliveTimeout = 600_000;
server.headersTimeout = 610_000;
server.requestTimeout = 0;
server.listen(port, "0.0.0.0", () => console.log(JSON.stringify({ ready: true, types, name, port })));
const stop = async () => {
  server.close();
  await runtime.close();
  for (const close of closers) await close();
  process.exit(0);
};
process.once("SIGTERM", () => void stop());
process.once("SIGINT", () => void stop());
