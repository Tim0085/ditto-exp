// 应用：Worker 的创建和四个场景的流程。几种布局（全部合在一起、拆开）用同一份代码。docs\16 第 2、3 节。
//
// 流程全部用 Ditto 的 graph / loop / graphStep 串起来，写法照官方 capabilities/memory 示例（shared.ts）：
// 每一步是一个小 graph，用 graphStep 在一个 loop 的 plan 里顺序执行。
// 每一步交给 Worker 的入参由下面“每一步的入参”一节的函数拼出；Direct（direct.ts，不经过 Ditto 运行时）调用同一批函数，
// 所以几种布局交给 Worker、发给模型的内容逐字相同（docs\31）。
import { chmodSync, copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { parse as parseYaml } from "yaml";
import { graph, graphStep, loop, loadRuntimeConfig, type RuntimeConfig } from "@codesoul-co/ditto/runtime";
import {
  createContextWorker, createInMemoryContextStore, createRagStrategy, isProtectedContextItem,
  type ContextInput, type ContextServices, type RedisContextClient,
} from "@codesoul-co/ditto/worker/context";
import {
  createMemoryWorker, MemoryError, type MemoryInput, type MemoryOutput, type MemoryStore, type MemorySearchProvider, type MemoryDraft,
} from "@codesoul-co/ditto/worker/memory";
import { createInferWorker, type InferInput, type InferOutput } from "@codesoul-co/ditto/worker/infer";
import type { WorkerDefinition } from "@codesoul-co/ditto/worker";
import type { ContextIngress, ContextItem, NodeResult } from "@codesoul-co/ditto/contracts";
// 官方存储适配器。sqlite-memory.ts 是原样副本；它引用的 sql-memory.ts 只改了一处：有 key 时记录编号由 key 算出
// （和官方 qdrant-memory.ts 的 memoryPointId 相同），否则两种布局、两次运行检索到的记录不同。见 relaxed\CHANGES.md。
import { openSqliteMemory } from "../relaxed/examples/_shared/tools/storage/sqlite-memory.ts";
import { scopedMemory } from "../official/examples/_shared/tools/storage/scoped-memory.ts";
import { tokens } from "../relaxed/examples/_shared/tools/evidence.ts";
import type { RagTask } from "./rag-sources.ts";
import {
  ANSWER_INSTRUCTION, KEYWORD_INSTRUCTION, MAX_TOKENS, MODEL, REAL, REAL_ANSWER_GENERATION, REAL_KEYWORD_GENERATION, REAL_KEYWORD_INSTRUCTION, SEARCH_LIMIT,
  lmeAnswerPrompt, realMarker,
} from "./common.ts";

// ---------------------------------------------------------------- 配置

/**
 * 运行时配置：官方 ditto.yaml 原样读入，只把 runtime.timeoutMs 从 120 秒放到 600 秒（并发 37 时排队可能较久，两种布局相同）。
 * 模型指向本机假模型（openai-compatible），只允许访问它。
 */
export function runtimeConfig(fakeModelUrl: string): RuntimeConfig {
  const origin = new URL(fakeModelUrl).origin;
  const settings = parseYaml(readFileSync(new URL("../official/ditto.yaml", import.meta.url), "utf8"));
  settings.runtime.timeoutMs = 600_000;
  return loadRuntimeConfig({
    DITTO_RUNTIME_ENV: "test",
    DITTO_SHARED_PROVIDERS: "local",
    DITTO_SHARED_PROVIDER_LOCAL_KIND: "openai-compatible",
    DITTO_SHARED_PROVIDER_LOCAL_BASE_URL: fakeModelUrl,
    DITTO_SHARED_PROVIDER_LOCAL_MODEL: MODEL.model,
    DITTO_WORKER_INFER_MODEL_PROVIDER: MODEL.provider,
    DITTO_WORKER_INFER_MODEL: MODEL.model,
    DITTO_SHARED_SANDBOX_ALLOW_NETWORK: origin,
  }, settings);
}

/**
 * CONTEXT 的政策。官方默认 maxItems 256、单条 64 KiB；长文档一题最多约 4500 块、
 * 长对话整段放进工作集约 500 条、个别消息或文档行超过 64 KiB，所以只放开这两个上限（docs\16 第 3 节）。
 * 去重、删除规则不变。
 */
export const CONTEXT_POLICY = { maxItems: 100_000, maxInlineBytes: 1_000_000 } as const; // 1_000_000 是政策允许的最大值
/**
 * maxItems 同时是 SELECT 的默认条数上限和 COMPRESS 的默认条数上限（select/node.js、deterministic.js 取 min(请求, 政策)）。
 * 放开政策后，这两处显式传官方默认的 256，挑选和压缩的行为不变。
 */
export const OFFICIAL_MAX_ITEMS = 256;
/** CONTEXT 工作集放进程内存时的设定：和官方 ditto.yaml workers.context.localCache 相同（ttl 1 小时、最多 1000 个会话）。 */
export const LOCAL_CONTEXT_STORE = { ttlMs: 3_600_000, maxEntries: 1000 } as const;
/** S4 的 MEMORY Worker 默认条数（docs\16 第 3 节）。 */
export const MEMORY_DEFAULTS = { queryLimit: 100, searchLimit: 10 } as const;

// ---------------------------------------------------------------- Worker

export type ContextStoreKind = "memory" | "redis";
export type SelectKind = "default" | "bm25";

/**
 * CONTEXT Worker。
 * - 工作集放进程内存：核心自带的 createInMemoryContextStore（ditto.yaml workers.context.localCache 的取值）。
 * - 放 Redis：官方 workers.ts 的写法 createContextWorker({ redis: { client, ...config.context.cache } })。
 * - bm25：核心的 RAG 策略 createRagStrategy，检索一步在 CONTEXT 进程里对当前工作集建 FTS5 索引，
 *   建表、分词和 BM25 查询照抄官方 rag 示例（adapters.ts 34、114、162–171 行）。
 */
export function contextWorker(kind: ContextStoreKind, select: SelectKind, redis?: RedisContextClient): WorkerDefinition {
  const ragStrategy = select === "bm25" ? bm25Strategy() : undefined;
  if (kind === "redis") {
    if (!redis) throw new Error("redis client required");
    return createContextWorker({ policy: CONTEXT_POLICY, redis: { client: redis, ttlMs: 3_600_000, keyPrefix: "ditto:context:" },
      ...(ragStrategy ? { services: { ragStrategy } } : {}) });
  }
  // 每个本地副本一份存储（servicesFactory），和官方 localCache 设定一样：ttl 1 小时、最多 1000 个会话。
  // 不设运行时并发上限（官方 HTTP 示例设 8）：Ditto 在 Worker 满载时直接报错、不排队，并发 16、37 会失败。两种布局相同。
  const services = (): ContextServices => ({ stateStore: createInMemoryContextStore(LOCAL_CONTEXT_STORE),
    ...(ragStrategy ? { ragStrategy } : {}) });
  return createContextWorker({ policy: CONTEXT_POLICY, servicesFactory: services });
}

/**
 * 官方 rag 示例的 BM25：FTS5 建表、按官方 tokens() 分词、OR 连接、bm25 排序、取前 12 条（官方默认）。
 * 只对工作集里的非保护条目建索引（受保护的当前目标由 SELECT 自己保留）。
 * 官方在入库时建一次索引；这里每次 SELECT 时对当前工作集建。每题只 SELECT 一次，两者等价。
 */
export const BM25_LIMIT = 12;
function bm25Strategy() {
  return createRagStrategy({
    retrieve: {
      async retrieve(input) {
        const db = new DatabaseSync(":memory:");
        try {
          db.exec("CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED, terms)");
          const insert = db.prepare("INSERT INTO search(id, terms) VALUES (?, ?)");
          const byId = new Map<string, ContextItem>();
          db.exec("BEGIN");
          for (const item of input.items) {
            if (typeof item.content !== "string" || isProtectedContextItem(item)) continue;
            byId.set(item.id, item);
            insert.run(item.id, tokens(item.content).join(" "));
          }
          db.exec("COMMIT");
          const query = typeof input.query === "string" ? input.query : JSON.stringify(input.query ?? "");
          const terms = tokens(query.slice(0, 120)).slice(0, 32);
          if (!terms.length) return [];
          const rows = db.prepare("SELECT id, bm25(search) AS rank FROM search WHERE search MATCH ? ORDER BY rank, id LIMIT ?")
            .all(terms.map(t => `"${t.replaceAll('"', '""')}"`).join(" OR "), BM25_LIMIT) as { id: string; rank: number }[];
          return rows.map(r => ({ item: byId.get(r.id)!, score: -Number(r.rank) }));
        } finally { db.close(); }
      },
    },
  });
}

/**
 * 多用户共用一个官方 SQLite 记忆库。官方 scopedMemory 创建时绑定一个用户（命名空间）；
 * 这里每次请求按请求里的命名空间套一层 scopedMemory，生成的 SQL 和扫描代价不变（docs\16 第 3 节）。
 */
export function multiUserMemory(base: MemoryStore & MemorySearchProvider): MemoryStore & MemorySearchProvider {
  const ns = (value: unknown) => {
    if (typeof value !== "string") throw new MemoryError("SCOPE_DENIED", "Request must name a memory namespace");
    return scopedMemory(base, value);
  };
  const fromKey = (key: string | undefined) => ns(key?.split(":").slice(0, 2).join(":"));
  return {
    get: (input, options) => fromKey(input.keys?.[0]).get(input, options),
    query: (input, options) => ns(input.filter?.namespace).query(input, options),
    search: (input, options) => ns(input.filter?.namespace).search(input, options),
    write: (input, options) => {
      const names = new Set(input.memories.map(m => m.metadata?.namespace));
      if (names.size !== 1) throw new MemoryError("SCOPE_DENIED", "One namespace per write");
      return ns([...names][0]).write(input, options);
    },
    update: () => { throw new MemoryError("UNSUPPORTED", "not used"); },
    delete: () => { throw new MemoryError("UNSUPPORTED", "not used"); },
  };
}

/**
 * MEMORY Worker：官方 openSqliteMemory（文件库、WAL、同步驱动、自带串行队列）。
 * 官方 storage.ts 设 concurrency: 1；Ditto 运行时在 Worker 满载时直接报错、不排队（runtime.js #track、router.js），
 * 多任务同时跑会失败，所以不设运行时并发上限。数据库操作仍由官方适配器的串行队列一次一个（sqlite-memory.ts serial）。
 */
export function memoryWorker(filename: string) {
  const sqlite = openSqliteMemory(filename);
  return { worker: createMemoryWorker({ store: multiUserMemory(sqlite.store), defaults: MEMORY_DEFAULTS }), close: sqlite.close };
}

export const inferWorker = () => createInferWorker();

/** 把卷里的只读记忆库复制到容器自己的文件系统，改成可写（每次运行从同一份副本开始）。 */
export function workingStore(source: string): string {
  mkdirSync("/tmp/work", { recursive: true });
  const target = "/tmp/work/memory.sqlite";
  copyFileSync(source, target);
  chmodSync(target, 0o644);
  return target;
}

// ---------------------------------------------------------------- 每一步的入参（graph 和 direct.ts 共用）

export type Scope = { sessionId: string };
export const nodeValue = <T>(r: NodeResult<T>): T => {
  if (r.status !== "success" || r.output === undefined) throw new Error(`Worker failed: ${r.error?.code ?? r.status} ${r.error?.message ?? ""}`);
  return r.output;
};

export const loadInput = (scope: Scope, sources: ContextItem[]): ContextInput<"CONTEXT.LOAD"> => ({ scope, sources });
/** 追加条目；检索到的记忆按官方写法走 ingress（memory.zh-CN.md 268 行附近的 memoryToContext）。 */
export const updateInput = (scope: Scope, add?: ContextItem[], ingress?: ContextIngress[]): ContextInput<"CONTEXT.UPDATE"> =>
  ({ scope, ...(add ? { add } : {}), ...(ingress ? { ingress } : {}) });
/** 把窗口压回预算（官方确定性压缩：按组删除，预算内时不删）。 */
export const compressInput = (scope: Scope): ContextInput<"CONTEXT.COMPRESS"> => ({ scope, maxTokens: MAX_TOKENS, maxItems: OFFICIAL_MAX_ITEMS });
/** S4：挑出要长期保存的（purpose: memory），条数是这一段里用户消息的条数。 */
export const memorySelectInput = (scope: Scope, candidates: number): ContextInput<"CONTEXT.SELECT"> => ({ scope, purpose: "memory", limit: candidates });
export const memoryWriteInput = (ns: string, items: readonly ContextItem[]): MemoryInput<"MEMORY.WRITE"> => ({ memories: items.map(item => memoryDraft(ns, item)) });
export const searchInput = (query: string, ns: string): MemoryInput<"MEMORY.SEARCH"> =>
  ({ query, filter: { namespace: ns }, strategy: "keyword", limit: SEARCH_LIMIT });
/** 回答前的挑选，照官方 inferGraph（先取 CONTEXT，再交给 INFER）。 */
export const answerSelectInput = (scope: Scope, query: string, strategy: SelectKind): ContextInput<"CONTEXT.SELECT"> =>
  ({ scope, purpose: "infer", query, maxTokens: MAX_TOKENS, limit: OFFICIAL_MAX_ITEMS, ...(strategy === "bm25" ? { strategy: { kind: "rag" } } : {}) });
/** 检索词一步发给模型的请求。 */
export const keywordInput = (key: string, question: string): InferInput<"INFER.REASONING.SAMPLE"> => REAL
  ? { model: MODEL, generation: REAL_KEYWORD_GENERATION, messages: [
    { role: "system", content: REAL_KEYWORD_INSTRUCTION },
    { role: "user", content: `${realMarker(key, "keyword")}Question: ${question}` },
  ] }
  : { model: MODEL, messages: [
    { role: "system", content: KEYWORD_INSTRUCTION },
    { role: "user", content: `[task:${key}][question:${question}][/question]` },
  ] };
/** 回答一步发给模型的请求。真实模型时，挑出的条目按 LongMemEval 官方格式写进官方回答模板。 */
export const answerInput = (key: string, items: readonly ContextItem[], question?: string, date?: string): InferInput<"INFER.REASONING.SAMPLE"> => REAL
  ? { model: MODEL, generation: REAL_ANSWER_GENERATION, messages: [
    { role: "user", content: realMarker(key, "answer") + lmeAnswerPrompt(renderHistory(items), date ?? "", question ?? "") },
  ] }
  : { model: MODEL, messages: [
    { role: "system", content: ANSWER_INSTRUCTION },
    { role: "user", content: `[task:${key}]` + JSON.stringify(items) },
  ] };

// ---------------------------------------------------------------- 一步一个 graph

const searchGraph = graph<{ query: string; ns: string }>("memctx-recall")
  .node("result", "MEMORY.SEARCH", [], i => searchInput(i.query, i.ns));
const writeGraph = graph<{ memories: MemoryDraft[] }>("memctx-save")
  .node("result", "MEMORY.WRITE", [], i => ({ memories: i.memories }));
const loadGraph = graph<{ scope: Scope; sources: ContextItem[] }>("memctx-load")
  .node("result", "CONTEXT.LOAD", [], i => loadInput(i.scope, i.sources));
const updateGraph = graph<{ scope: Scope; add?: ContextItem[]; ingress?: ContextIngress[] }>("memctx-update")
  .node("result", "CONTEXT.UPDATE", [], i => updateInput(i.scope, i.add, i.ingress));
/** 追加一个 session，再把窗口压回预算。 */
const appendGraph = graph<{ scope: Scope; add: ContextItem[] }>("memctx-append")
  .node("update", "CONTEXT.UPDATE", [], i => updateInput(i.scope, i.add))
  .node("result", "CONTEXT.COMPRESS", ["update"], i => compressInput(i.scope));
/** S4：追加 → 挑出要长期保存的（purpose: memory）→ 写进 MEMORY → 压回预算。context.zh-CN.md 152 行的协作流程。 */
const sessionGraph = graph<{ scope: Scope; add: ContextItem[]; candidates: number; ns: string }>("memctx-session")
  .node("update", "CONTEXT.UPDATE", [], i => updateInput(i.scope, i.add))
  .node("selection", "CONTEXT.SELECT", ["update"], i => memorySelectInput(i.scope, i.candidates))
  .node("write", "MEMORY.WRITE", ["selection"], (i, { selection }) => memoryWriteInput(i.ns, selection.context.items))
  .node("result", "CONTEXT.COMPRESS", ["write"], i => compressInput(i.scope));
const keywordGraph = graph<{ key: string; question: string }>("memctx-keywords")
  .node("result", "INFER.REASONING.SAMPLE", [], i => keywordInput(i.key, i.question));
/** 挑选 + 回答，照官方 inferGraph（先取 CONTEXT，再交给 INFER）。 */
const answerGraph = graph<{ key: string; scope: Scope; query: string; strategy: SelectKind; question?: string; date?: string }>("memctx-answer")
  .node("selection", "CONTEXT.SELECT", [], i => answerSelectInput(i.scope, i.query, i.strategy))
  .node("result", "INFER.REASONING.SAMPLE", ["selection"], (i, { selection }) => answerInput(i.key, selection.context.items, i.question, i.date));

/**
 * 真实模型：把 CONTEXT.SELECT 挑出的条目写成 LongMemEval 官方 history_format=nl 的样子（run_generation.py 224–255 行）：
 * 按日期排序，同一 session 归成一段 “### Session i: / Session Date: / Session Content:”，段内按原来的轮次，每条 “角色: 内容”。
 * - 跳过受保护的当前目标（题目和日期另外填进模板）。
 * - 窗口里的消息（CONTEXT 里的会话，编号 “session:轮次”）和检索到的记忆（MEMORY.SEARCH 经 ingress 进来，metadata 带 session、turn）
 *   一起排。SELECT 给出的顺序是相关度，不是对话顺序，所以一律按（日期、session、轮次）重排。
 * - 同一条原始消息（同一 session、同一轮次）既在窗口里、又被检索到时只留一条；文字相同但轮次不同的照样保留。
 * - 只用条目本身和它的 metadata，两种布局完全相同。
 */
export function renderHistory(items: readonly ContextItem[]): string {
  const turns: { date: string; session: string; turn: number; key: string; text: string; i: number }[] = [];
  const seen = new Set<string>();
  items.forEach((item, i) => {
    if (isProtectedContextItem(item)) return;
    const raw = typeof item.content === "string" ? item.content : JSON.stringify(item.content);
    const session = String(item.metadata?.session ?? "");
    let text = raw, turn = Number.NaN;
    if (item.metadata?.sourceNode === "MEMORY.SEARCH") {
      // 记忆的内容是 {"text": ...}：S4 写入的是窗口里的 “user: 原文”，S1 写入的是原文。轮次由 recallPlan 从记忆的 key 取出。
      try { const parsed = JSON.parse(raw); if (typeof parsed?.text === "string") text = parsed.text; } catch { /* 原样用 */ }
      if (!/^(user|assistant): /.test(text)) text = `user: ${text}`;
      turn = Number(item.metadata?.turn);
    } else if (session && item.id.startsWith(`${session}:`)) {
      turn = Number(item.id.slice(session.length + 1));
    }
    const speaker = /^(user|assistant): /.exec(text)?.[1];
    text = speaker ? `${speaker}: ${text.slice(speaker.length + 2).trim()}` : text.trim();
    const key = Number.isInteger(turn) ? `${session}#${turn}` : `${session}#text:${text}`;
    if (seen.has(key)) return;
    seen.add(key);
    turns.push({ date: String(item.metadata?.date ?? ""), session, turn: Number.isInteger(turn) ? turn : Number.MAX_SAFE_INTEGER, key, text, i });
  });
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
  turns.sort((a, b) => cmp(a.date, b.date) || cmp(a.session, b.session) || a.turn - b.turn || a.i - b.i);
  const groups: { date: string; session: string; lines: string[] }[] = [];
  for (const t of turns) {
    let g = groups.at(-1);
    if (!g || g.date !== t.date || g.session !== t.session) groups.push(g = { date: t.date, session: t.session, lines: [] });
    g.lines.push(t.text);
  }
  return groups.map((g, i) => `\n### Session ${i + 1}:\nSession Date: ${g.date}\nSession Content:\n${g.lines.map(l => `\n\n${l}`).join("")}\n`).join("");
}

export function memoryDraft(ns: string, item: ContextItem): MemoryDraft {
  return { key: `${ns}:${item.id}`, content: { text: item.content },
    metadata: { namespace: ns, kind: "message", session: String(item.metadata?.session ?? ""), date: String(item.metadata?.date ?? "") } };
}

/**
 * 真实模型的检索词：照官方记忆示例去掉 ```json 围栏再解析（capabilities/memory/shared.ts 的 inferPlan）；
 * 前后有多余文字时取第一个 {...}。最多 3 个、每个最多 100 字符（官方 evidence.ts 的 plan() 同样的上限）。解析不了这题算失败。
 */
export function parseQueries(content: string): string[] {
  const text = content.trim().replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "");
  let parsed: unknown;
  try { parsed = JSON.parse(text); }
  catch {
    // 前后有多余的话：取第一个能解析的 {...}（从第一个 “{” 起，逐个试到后面的 “}”），后面再有带花括号的话也不受影响。
    const start = text.indexOf("{");
    for (let end = text.indexOf("}", start); start >= 0 && end >= 0 && parsed === undefined; end = text.indexOf("}", end + 1)) {
      try { parsed = JSON.parse(text.slice(start, end + 1)); } catch { /* 再往后试 */ }
    }
    if (parsed === undefined) throw new Error("Keyword step: model returned no JSON");
  }
  const raw = (parsed as { queries?: unknown })?.queries;
  const queries = Array.isArray(raw) ? raw.filter((q): q is string => typeof q === "string").map(q => q.trim().slice(0, 100)).filter(Boolean) : [];
  if (!queries.length) throw new Error("Keyword step: no queries");
  // 记忆检索不分大小写（sql-memory.ts 的 LOWER(content) LIKE），大小写不同的同一个词只查一次。
  const unique = new Map<string, string>();
  for (const q of queries) if (!unique.has(q.toLowerCase())) unique.set(q.toLowerCase(), q);
  return [...unique.values()].slice(0, 3);
}
/** 检索词一步：模型的回复 → 检索词（graph 和 direct.ts 共用）。 */
export function keywordsFrom(out: InferOutput<"INFER.REASONING.SAMPLE">): string[] {
  if (typeof out.message.content !== "string") throw new Error("Incomplete model response");
  // Ditto 把服务商的 content_filter 映射成 finishReason “error”：算这题运行失败，原因写清楚。
  if (REAL && out.finishReason === "error") throw new Error("Keyword step: provider filtered the output (content_filter)");
  if (REAL) return parseQueries(out.message.content);
  if (out.finishReason !== "stop") throw new Error("Incomplete model response");
  const parsed = JSON.parse(out.message.content) as { queries: string[] };
  if (!Array.isArray(parsed.queries) || !parsed.queries.length) throw new Error("No queries");
  return parsed.queries.slice(0, 3).map(String);
}
/** 回答一步：真实模型时回答就是模型给的文字（官方判分直接拿它去比），finishReason 一并记下（graph 和 direct.ts 共用）。 */
export function answerFrom(out: InferOutput<"INFER.REASONING.SAMPLE">): { answer: string; finishReason?: string } {
  if (typeof out.message.content !== "string") throw new Error("Incomplete model response");
  if (REAL && out.finishReason === "error") throw new Error("Answer step: provider filtered the output (content_filter)");
  if (REAL) return { answer: out.message.content, finishReason: out.finishReason as string };
  if (out.finishReason !== "stop") throw new Error("Incomplete model response");
  return JSON.parse(out.message.content) as { answer: string };
}
function* keywordsPlan(key: string, question: string) {
  return keywordsFrom(nodeValue((yield* graphStep(keywordGraph, { key, question })).result));
}
function* answerPlan(key: string, scope: Scope, query: string, strategy: SelectKind = "default", question?: string, date?: string) {
  return answerFrom(nodeValue((yield* graphStep(answerGraph, { key, scope, query, strategy, question, date })).result));
}
/** 记忆 key 的最后一段是轮次；解析不出整数就不带（metadata 里不能放 NaN）。 */
const turnOf = (key: string | undefined): { turn: number } | Record<string, never> => {
  const turn = Number(String(key ?? "").split(":").at(-1));
  return Number.isInteger(turn) ? { turn } : {};
};
/**
 * 检索到的一条记忆按官方 memoryToContext 的写法映射成 ingress（graph 和 direct.ts 共用）。
 * 真实模型时另带上记忆的 session 和日期（官方示例只带编号和相关度）：时间推理题要用日期，renderHistory 按它排序。
 */
export function ingressOf(hit: MemoryOutput<"MEMORY.SEARCH">[number]): ContextIngress {
  return { id: `memory:${hit.memory.id}`, sourceNode: "MEMORY.SEARCH",
    content: typeof hit.memory.content === "string" ? hit.memory.content : JSON.stringify(hit.memory.content),
    metadata: { memoryId: hit.memory.id, ...(hit.score === undefined ? {} : { relevance: hit.score }),
      // 轮次取 key 的最后一段（S4 的 key 是 “命名空间:session:轮次”，轮次和窗口里的编号一致），供 renderHistory 排序、去重。
      ...(REAL ? { session: String(hit.memory.metadata?.session ?? ""), date: String(hit.memory.metadata?.date ?? ""),
        ...turnOf(hit.memory.key) } : {}) } };
}
/** 每个检索词一次 MEMORY.SEARCH；检查 NodeResult 后映射成 ingress。 */
function* recallPlan(queries: string[], ns: string) {
  const hits: ContextIngress[] = [];
  for (const query of queries) {
    for (const hit of nodeValue((yield* graphStep(searchGraph, { query, ns })).result)) hits.push(ingressOf(hit));
  }
  return hits;
}

// ---------------------------------------------------------------- 题目

export interface Message { role: string; content: string }
export interface LmeTask { id: string; qtype: string; question: string; question_date: string; sessions: { id: string; date: string; messages: Message[] }[] }
export interface LbTask { id: string; segment: string; question: string; chunks: { id: string; text: string }[] }

/** 记忆库里一条记忆 = 一条用户消息（LongMemEval 官方检索默认的粒度，docs\16 S1）。 */
export function userMemories(ns: string, session: LmeTask["sessions"][number]): MemoryDraft[] {
  const out: MemoryDraft[] = [];
  session.messages.forEach((m, i) => {
    if (m.role !== "user" || !m.content.trim()) return;
    out.push({ key: `${ns}:${session.id}:${i}`, content: { text: m.content }, metadata: { namespace: ns, kind: "message", session: session.id, date: session.date } });
  });
  return out;
}
export const sessionItems = (session: LmeTask["sessions"][number], candidates: boolean): ContextItem[] =>
  session.messages.filter(m => m.content.trim()).map((m, i) => ({
    id: `${session.id}:${i}`, content: `${m.role}: ${m.content}`,
    metadata: { session: session.id, date: session.date, speaker: m.role, ...(candidates && m.role === "user" ? { memoryCandidate: true } : {}) },
  }));
export const goalItem = (question: string, date?: string): ContextItem =>
  ({ id: "goal", content: date ? `Question date: ${date}\nQuestion: ${question}` : question, metadata: { currentGoal: true } });

// ---------------------------------------------------------------- 四个场景

/** rag（docs\24）走官方 rag-qa 流程，由 run.ts 直接调用 runRagLoop，不经过下面的 taskLoop。 */
export type Scenario = "s1-qa" | "s1-write" | "s2" | "s3" | "s3-full" | "s4" | "rag";
/**
 * 一次任务。
 * - key：题号 + 序号，写进发给模型的请求（[task:...]），假模型按它抽延迟。两种布局相同。
 * - run：这次执行的身份，用于新建的记忆命名空间（S1 写入、S4）。阶段一的重复遍数编进去，每遍都是新的；
 *   阶段三等于 key（每格从同一份新库副本开始），两种布局相同，检索到的记录编号也相同。
 * - tag：只用于 CONTEXT 的会话作用域，阶段三是格子名。Redis 跨格子保留数据，换 tag 才不会读到上一格的工作集。
 */
export interface Job { scenario: Scenario; key: string; run: string; tag: string; lme?: LmeTask; lb?: LbTask; rag?: RagTask; select?: SelectKind }
export const scopeOf = (prefix: string, job: Job): Scope => ({ sessionId: `${prefix}:${job.tag}:${job.run}` });
export const namespaceOf = (prefix: string, job: Job) => `${prefix}:${job.run.replace(/[^a-zA-Z0-9_-]/g, "_")}`;

/** S1 问答：关键词 → 每个关键词检索记忆 → 载入问题 → 检索结果经 ingress 进 CONTEXT → 挑选 → 回答。 */
function* s1QaPlan(job: Job) {
  const t = job.lme!, ns = `lme:${t.id}`, scope = scopeOf("s1", job);
  const queries = yield* keywordsPlan(job.key, t.question);
  const hits = yield* recallPlan(queries, ns);
  yield* graphStep(loadGraph, { scope, sources: [goalItem(t.question, t.question_date)] });
  yield* graphStep(updateGraph, { scope, ingress: hits });
  return { ...(yield* answerPlan(job.key, scope, queries.join(" "), "default", t.question, t.question_date)), queries, hits: hits.length };
}
/** S1 写入：一个用户的全部历史，每个 session 一次 MEMORY.WRITE。命名空间按这次任务新建，库里原有的用户不动。 */
function* s1WritePlan(job: Job) {
  const t = job.lme!, ns = namespaceOf("wr", job);
  let written = 0;
  for (const session of t.sessions) {
    const memories = userMemories(ns, session);
    if (!memories.length) continue;
    written += nodeValue((yield* graphStep(writeGraph, { memories })).result).length;
  }
  return { answer: `written ${written}` };
}
/** S2 长文档：整份文档按官方切块载入工作集 → 关键词 → 挑选（默认或 BM25）→ 回答。 */
function* s2Plan(job: Job) {
  const t = job.lb!, scope = scopeOf("s2", job);
  yield* graphStep(loadGraph, { scope, sources: [goalItem(t.question), ...t.chunks.map(c => ({ id: `chunk:${c.id}`, content: c.text }))] });
  const queries = yield* keywordsPlan(job.key, t.question);
  return yield* answerPlan(job.key, scope, queries.join(" "), job.select ?? "default");
}
/** S3 长对话交给 CONTEXT：逐个 session 追加；s3 每次追加后压回 32K，s3-full 不压缩。最后提问。 */
function* s3Plan(job: Job, compress: boolean) {
  const t = job.lme!, scope = scopeOf("s3", job);
  yield* graphStep(loadGraph, { scope, sources: [] });
  for (const session of t.sessions) {
    const add = sessionItems(session, false);
    if (!add.length) continue;
    yield* graphStep(compress ? appendGraph : updateGraph, { scope, add });
  }
  yield* graphStep(updateGraph, { scope, add: [goalItem(t.question, t.question_date)] });
  const queries = yield* keywordsPlan(job.key, t.question);
  return yield* answerPlan(job.key, scope, queries.join(" "), job.select ?? "default");
}
/**
 * S4 CONTEXT 与 MEMORY 协作：每个 session 追加 → 挑出用户消息写进记忆 → 压回预算；提问时检索记忆 → 映射进 CONTEXT → 挑选 → 回答。
 * direct.ts 的 directS4 是同样的步骤写成普通函数（不经过 Ditto 运行时），改这里要一起改那里（测试 direct.test.ts 核对两边发给模型的请求逐字相同）。
 */
function* s4Plan(job: Job) {
  const t = job.lme!, scope = scopeOf("s4", job), ns = namespaceOf("s4", job);
  yield* graphStep(loadGraph, { scope, sources: [] });
  for (const session of t.sessions) {
    const add = sessionItems(session, true);
    const candidates = add.filter(item => item.metadata?.memoryCandidate === true).length;
    if (!add.length) continue;
    if (!candidates) { yield* graphStep(appendGraph, { scope, add }); continue; }
    const out = yield* graphStep(sessionGraph, { scope, add, candidates, ns });
    nodeValue(out.write);
  }
  const queries = yield* keywordsPlan(job.key, t.question);
  const hits = yield* recallPlan(queries, ns);
  yield* graphStep(updateGraph, { scope, add: [goalItem(t.question, t.question_date)], ingress: hits });
  return { ...(yield* answerPlan(job.key, scope, queries.join(" "), "default", t.question, t.question_date)), queries, hits: hits.length };
}

export const taskLoop = loop({
  id: "memctx-task",
  maxIterations: 4096,
  plan: (job: Job) => {
    switch (job.scenario) {
      case "s1-qa": return s1QaPlan(job);
      case "s1-write": return s1WritePlan(job);
      case "s2": return s2Plan(job);
      case "s3": return s3Plan(job, true);
      case "s3-full": return s3Plan(job, false);
      case "s4": return s4Plan(job);
      case "rag": throw new Error("rag 由 run.ts 直接调用官方 runRagLoop");
    }
  },
});

/** 各场景用到的节点（拆开时远端登记用）。 */
export const MEMORY_NODES = ["MEMORY.SEARCH", "MEMORY.WRITE", "MEMORY.GET"] as const;
export const CONTEXT_NODES = ["CONTEXT.LOAD", "CONTEXT.SELECT", "CONTEXT.UPDATE", "CONTEXT.COMPRESS"] as const;
