// rag 场景：按 Ditto 官方 rag-qa 流程（runRagLoop）跑 LongMemEval_S。docs\24。
// 官方代码用 rag-a\ 下的副本（切法 (a)：一段对话一个来源）：index.ts、adapters.ts 一字未改，
// domain.ts 只放开两个数字（每题来源 20→100、每个来源的块 200→400），见 rag-a\CHANGES.md。切法 (b) 的数据也守得住这两个数字以内，照样能跑。
//
// 官方示例是一题一个 Runtime（rag-qa/cli.ts：每题新建 RagAdapters 和一套 Worker）。实验要几十题同时跑在同一组进程里，
// 所以只加两层外壳，官方代码一行不改（docs\24 第 5 节）：
// 1. RagDispatch：INTERACTION 的 4 个工具和 RETRIEVAL 的检索，按题转给这一题自己的官方 RagAdapters（自己的目录和题库）。
//    官方工具调用的编号就是工具名（index.ts 61 行），认不出是哪一题，所以从参数里认：
//    rag_authorize 看 request.id；rag_ingest 看记忆的 key；rag_check_sources 看块的 uri；rag_publish 看 report.requestId；
//    检索看 namespace（官方写法 <tenant>:<principal>:<requestId>）。这些都带着这一题的 token（题号的哈希）。
// 2. MEMORY 不设 concurrency: 1（官方 storage/workers.ts 16 行设了）：Ditto 在 Worker 满载时直接报错、不排队，多题同时跑会大量失败。
//    数据库操作仍由官方 SQLite 适配器自己的串行队列一次一个。和 S4 的处理相同。
// 其余照官方：CONTEXT 用 Redis、官方政策（ditto.yaml）；MEMORY 用官方 SQLite 适配器（relaxed 副本只改了记录编号的来源，见 relaxed\CHANGES.md）；
// RETRIEVAL、INTERACTION 用官方工厂函数。
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { RuntimeConfig } from "@codesoul-co/ditto/runtime";
import { createRetrievalWorker, type RetrievalProviderRegistry } from "@codesoul-co/ditto-retrieval";
import { createInteractionWorker, type RegisteredTool } from "@codesoul-co/ditto/worker/interaction";
import { createContextWorker, type RedisContextClient } from "@codesoul-co/ditto/worker/context";
import { createMemoryWorker } from "@codesoul-co/ditto/worker/memory";
import type { WorkerDefinition } from "@codesoul-co/ditto/worker";
import { RagAdapters } from "../rag-a/examples/_shared/tools/rag/adapters.ts";
import { object, type Report, type Request } from "../rag-a/examples/_shared/tools/rag/domain.ts";
import type { Input } from "../rag-a/examples/patterns/rag-qa/index.ts";
import { openSqliteMemory } from "../relaxed/examples/_shared/tools/storage/sqlite-memory.ts";
import { PRINCIPAL, TENANT, type RagSource, type RagTask } from "./rag-sources.ts";
import { sha256 } from "./common.ts";

/** 官方 4 个工具的名字和读写性质（adapters.ts 205–252 行）。 */
export const RAG_TOOLS: readonly (readonly [string, readonly ("read" | "write")[]])[] = [
  ["rag_authorize", ["read"]], ["rag_ingest", ["read", "write"]], ["rag_check_sources", ["read"]], ["rag_publish", ["read", "write"]],
];
export const INTERACTION_NODES = ["INTERACTION.ACT.TOOL", "INTERACTION.OBSERVE"] as const;
export const RETRIEVAL_NODES = ["RETRIEVAL.SEARCH"] as const;

/** 一次执行的请求。编号由这次执行的身份算出（两种布局相同、每次执行不同），末尾是题的 token。模型看不到请求编号。 */
export function ragRequest(run: string, task: RagTask): Request {
  return { id: `t${sha256(run).slice(0, 12)}-${task.token}`, tenant: TENANT, principal: PRINCIPAL, question: task.question,
    sourceIds: task.sources.map(s => s.id) };
}
export const ragInput = (run: string, task: RagTask, model: Input["model"]): Input => ({ request: ragRequest(run, task), model });

/**
 * 官方报告 → 判分用的文字。官方回答是带引用的结构化报告（status、claims、limitations），
 * LongMemEval 判分要一段文字：把每条结论句依次连起来；状态不是 answered（证据不足、有冲突、要澄清）时，再接上 limitations，
 * 它说明缺什么——“无法回答”类的题就靠这段判对。引文不放进去（结论句本身就是回答）。
 */
export function ragAnswerText(report: Report): string {
  const lines = report.answer.claims.map(c => c.text);
  if (report.answer.status !== "answered") lines.push(...report.answer.limitations);
  return lines.join("\n");
}

/**
 * rag 的一题失败时是哪一类（real.sh 据此判格子）：
 * - "model-output"：模型的输出过不了官方检查。照官方，这一题就是答不出，判分时算错。两种情况：
 *   1. 紧跟在一次成功的模型调用（官方 inferGraph，图名 rag-context-and-model）之后报的错：回的不是 JSON（SyntaxError）、
 *      输出不完整，或者过不了 evidence.ts 对这一步输出的检查（plan、selection、answer、verify）。
 *      官方流程里，模型那张图完成以后、下一张图开始以前只做这几件事（index.ts 207–226、256–266、340–356、401–433 行）。
 *   2. 检索池超过 4.5 万字、证据超过 1 万字（index.ts 327、354 行）：取决于模型给的检索词和挑的证据，官方只在这两处报这两句。
 * - "infra"：其余一切（Worker、传输、工具、检查点、出口出错）。多了说明程序或网络有问题，这一格作废。
 * 只看错误文字会误判：例如检查点读出坏数据也报 “Expected object”，被拆出的 Worker 回了坏 JSON 也是 SyntaxError，
 * 所以要看出错时最近的一张图：由 runtime.loop 的 onGraph 记下（run.ts）。
 */
export interface GraphMark { graphId: string; status: string }
const MODEL_GRAPH = "rag-context-and-model";
const MODEL_OUTPUT_ERRORS = [
  "Expected object", "Invalid text", "Invalid list", "Invalid identifier", "Invalid clarification", "Missing query or facets",
  "Invalid selection", "Invalid conflicts", "Conflict needs two selected chunks", "Invalid answer", "Every claim needs citations",
  "Unsupported citation", "Answer status must match selection", "Incomplete or conflicting evidence cannot be answered conclusively",
  "Conflict must be disclosed", "Cite both sides of every conflict", "Explain unresolved requirements", "Grounding check failed",
  "Incomplete model output",
];
const BUDGET_ERRORS = ["Retrieval budget exceeded; narrow the request", "Context budget exceeded; narrow the request"];
export function ragFailKind(error: unknown, last?: GraphMark): "model-output" | "infra" {
  const message = String((error as Error)?.message ?? error);
  if (BUDGET_ERRORS.includes(message)) return "model-output";
  const afterModel = last?.graphId === MODEL_GRAPH && last.status === "completed";
  return afterModel && (error instanceof SyntaxError || MODEL_OUTPUT_ERRORS.some(m => message.startsWith(m))) ? "model-output" : "infra";
}

const TOKEN = /([0-9a-f]{12})$/;
const tokenOfRequest = (id: string) => TOKEN.exec(id)?.[1];
const tokenOfRef = (ref: string) => /:([0-9a-f]{12})-s\d+$/.exec(ref)?.[1];

interface Open { adapters: RagAdapters; tools: Map<string, RegisteredTool>; providers: RetrievalProviderRegistry; dir: string }

export class RagDispatch {
  /** token → 来源清单（来自题目文件；原文在 MEMORY 里） */
  private readonly catalogs = new Map<string, RagSource[]>();
  /** request id → 这一题的官方 adapters */
  private readonly open = new Map<string, Open>();
  private readonly root: string;

  constructor(tasksFile: string, root = join(tmpdir(), "rag-tasks")) {
    for (const line of readFileSync(tasksFile, "utf8").split("\n")) {
      if (!line.trim()) continue;
      const t = JSON.parse(line) as RagTask;
      this.catalogs.set(t.token, t.sources);
    }
    this.root = root;
    rmSync(root, { recursive: true, force: true });
    mkdirSync(root, { recursive: true });
  }

  get active() { return this.open.size; }

  /** rag_authorize 时为这一题建目录、写来源清单（官方示例里由“可信控制器”事先准备好），再建官方 RagAdapters。 */
  private create(input: unknown): Open {
    const r = object(input) as unknown as Request;
    const found = this.open.get(String(r.id));
    if (found) return found;
    const token = tokenOfRequest(String(r.id));
    const catalog = token ? this.catalogs.get(token) : undefined;
    if (!catalog) throw new Error(`没有这道题的来源清单：${String(r.id)}`);
    // 同一道题开始了新的一次执行（阶段一的下一遍，或者上一次中途失败、没走到 rag_publish）：上一次已经结束，收尾，
    // 免得后面按 token 找题时撞到两份。阶段三每格每题只出现一次，不会和正在做的冲突。
    for (const o of [...this.open.values()]) if (tokenOfRequest(o.adapters.request.id) === token) this.finish(o);
    const dir = join(this.root, String(r.id));
    rmSync(dir, { recursive: true, force: true });
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "sources.json"), JSON.stringify(catalog.map(s =>
      ({ id: s.id, tenant: TENANT, readers: [PRINCIPAL], title: s.title, kind: "internal", ref: s.ref }))));
    const adapters = new RagAdapters(dir, r);
    const opened: Open = { adapters, tools: new Map(adapters.tools.map(t => [t.name, t])), providers: adapters.providers, dir };
    this.open.set(adapters.request.id, opened);
    return opened;
  }

  /** 按 token 找正在做的那一题；同一 token 有两次执行同时在做就报错（每格每题只出现一次，不会发生）。 */
  private byToken(token: string | undefined): Open {
    const hits = [...this.open.values()].filter(o => tokenOfRequest(o.adapters.request.id) === token);
    if (hits.length !== 1) throw new Error(`按 token ${token} 找到 ${hits.length} 道正在做的题`);
    return hits[0]!;
  }

  private route(name: string, args: Record<string, unknown>): Open {
    switch (name) {
      case "rag_authorize": return this.create(args.request);
      case "rag_ingest": return this.byToken(tokenOfRef(Object.keys(object(args.memories))[0] ?? ""));
      case "rag_check_sources": {
        const first = Array.isArray(args.evidence) ? object(args.evidence[0]) : {};
        return this.byToken(tokenOfRef(String(first.uri ?? "")));
      }
      case "rag_publish": {
        const o = this.open.get(String(object(args.report).requestId));
        if (!o) throw new Error("rag_publish：这道题没有在做");
        return o;
      }
      default: throw new Error(`没有工具 ${name}`);
    }
  }

  /** 4 个工具：名字、读写性质、参数检查和官方相同，执行时转给这一题的官方工具。发布以后这一题收尾（关库、删目录）。 */
  get tools(): RegisteredTool[] {
    return RAG_TOOLS.map(([name, effects]) => ({
      name, effects: [...effects], inputSchema: { type: "object" }, validate: object,
      execute: async (args, ctx) => {
        const o = this.route(name, args);
        const outcome = await o.tools.get(name)!.execute(args, ctx);
        if (name === "rag_publish" && outcome.status === "success") this.finish(o);
        return outcome;
      },
    }) satisfies RegisteredTool);
  }

  /** 检索：按 namespace 转给这一题的官方检索（adapters.providers，BM25）。 */
  get providers(): RetrievalProviderRegistry {
    return {
      resolve: (target, strategy) => {
        const id = String(target.namespace ?? "").split(":")[2] ?? "";
        const o = this.open.get(id);
        if (!o) throw new Error(`检索：请求 ${id} 没有在做`);
        return o.providers.resolve(target, strategy);
      },
    };
  }

  private finish(o: Open) {
    this.open.delete(o.adapters.request.id);
    try { o.adapters.close(); } catch { /* 已经关了 */ }
    void rm(o.dir, { recursive: true, force: true }).catch(() => {});
  }

  close() { for (const o of [...this.open.values()]) this.finish(o); }
}

/** INTERACTION + RETRIEVAL：官方 rag-qa 里两者共用一份题库（cli.ts 23、36–37 行），所以总在同一个进程里。 */
export function ragInteractionWorkers(dispatch: RagDispatch): { retrieval: WorkerDefinition; interaction: WorkerDefinition } {
  return { retrieval: createRetrievalWorker({ providers: dispatch.providers }), interaction: createInteractionWorker({ tools: dispatch.tools }) };
}
/** INTERACTION 所在进程的工具许可：官方 cli.ts 的写法 { ...config.sandbox, tools: 工具名 }。 */
export const ragSandbox = (config: RuntimeConfig) => ({ ...config.sandbox, tools: RAG_TOOLS.map(([name]) => name) });

/** MEMORY：官方 storage/workers.ts 的写法，只去掉 concurrency: 1（见文件开头）。filename 是内部知识库的工作副本。 */
export function ragMemoryWorker(filename: string, config: RuntimeConfig) {
  const memory = openSqliteMemory(filename);
  return { worker: createMemoryWorker({ store: memory.store, defaults: config.memory }), close: memory.close };
}
/** CONTEXT：官方 storage/workers.ts 的写法（Redis、ditto.yaml 里的政策和缓存设置），一个上限都不放开。 */
export const ragContextWorker = (config: RuntimeConfig, redis: RedisContextClient): WorkerDefinition =>
  createContextWorker({ ...(config.context.policy ? { policy: config.context.policy } : {}), redis: { client: redis, ...config.context.cache } });
