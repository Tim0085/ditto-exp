// Direct（src\direct.ts，docs\31）的测试：不联网、不碰 Docker。node --test tests/direct.test.ts
// 1. rag 的四段提示词和官方 rag-qa/index.ts 逐字相同（从官方文件里取出来比）。
// 2. S4（假模型那一套提示词）：同样 3 道题同时跑，Monolithic（Ditto 运行时 + Worker）和 Direct 发给模型的每个请求逐字相同，
//    回答、检索词、检索条数也相同；Direct 也记下每次调用的用时（calls.ts），节点名和 Monolithic 相同、带题的序号。
// 3. rag（官方 rag-qa）：同样 2 道题，两边每一步的请求逐字相同，官方报告逐字相同；模型输出过不了官方检查时，失败分类和 Monolithic 一样。
// 4. S4 真实模型那一套提示词（MEMCTX_REAL=1，在子进程 direct-real-child.ts 里跑）：经出口（egress.ts）到假模型，两边请求逐字相同；
//    出口记下完整请求体（按摘要去重）和每次的完整回复。
// 临时文件放 F 盘 sec1-2.0\_tmp\scratch\direct-test\，测完删掉。
import { test, after, before } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:http";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDitto } from "@codesoul-co/ditto/runtime";
import { createContextWorker, createInMemoryContextStore } from "@codesoul-co/ditto/worker/context";
import { createInferWorker } from "@codesoul-co/ditto/worker/infer";
import { createMemory } from "@codesoul-co/ditto/worker/memory";
import { runRagLoop } from "../rag-a/examples/patterns/rag-qa/index.ts";
import { openSqliteMemory } from "../relaxed/examples/_shared/tools/storage/sqlite-memory.ts";
import { LOCAL_CONTEXT_STORE, contextWorker, inferWorker, memoryWorker, runtimeConfig, taskLoop, type Job, type LmeTask } from "../src/app.ts";
import { MODEL, REAL, jsonLines } from "../src/common.ts";
import { CallLog, jobContext } from "../src/calls.ts";
import { RAG_ANSWER_PROMPT, RAG_PLAN_PROMPT, RAG_RULES, RAG_SELECT_PROMPT, RAG_VERIFY_PROMPT, createDirectRag, createDirectS4 } from "../src/direct.ts";
import { RagDispatch, ragFailKind, ragInput, ragInteractionWorkers, ragMemoryWorker, ragSandbox, type GraphMark } from "../src/rag.ts";
import { buildRagTaskA } from "../src/rag-sources.ts";

const ROOT = fileURLToPath(new URL("../../_tmp/scratch/direct-test/", import.meta.url));
rmSync(ROOT, { recursive: true, force: true });
mkdirSync(ROOT, { recursive: true });
after(() => rmSync(ROOT, { recursive: true, force: true }));
const DATA = fileURLToPath(new URL("../../bench_data/longmemeval-s/derived/memctx-lme-first200.jsonl", import.meta.url));

async function firstTasks(n: number): Promise<LmeTask[]> {
  const out: LmeTask[] = [];
  for await (const line of jsonLines(DATA)) { out.push(JSON.parse(line)); if (out.length === n) break; }
  return out;
}

// ---------------------------------------------------------------- 假模型（src\fake-model.ts，子进程）

let fake: ChildProcess | undefined;
const FAKE_PORT = 18993, FAKE = `http://127.0.0.1:${FAKE_PORT}`;
before(async () => {
  fake = spawn(process.execPath, ["--no-warnings", fileURLToPath(new URL("../src/fake-model.ts", import.meta.url)), "--port", String(FAKE_PORT)], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise<void>((resolve, reject) => { fake!.stdout!.once("data", () => resolve()); fake!.once("exit", code => reject(new Error(`假模型退出 ${code}`))); });
});
after(() => { fake?.kill(); });
/** 假模型复位，返回一个取“这次收到的请求”的函数：(题, 步骤) → 请求体的 SHA-256。 */
async function fakeSession(seed: string) {
  assert.equal((await fetch(`${FAKE}/reset?seed=${seed}`, { method: "POST" })).status, 200);
  return async () => {
    const s = await (await fetch(`${FAKE}/stats`)).json() as { rows: { task: string; kind: string; sha256: string }[]; errors: number };
    assert.equal(s.errors, 0, "假模型没有报错");
    const m = new Map<string, string>();
    for (const r of s.rows) { assert.ok(!m.has(`${r.task}|${r.kind}`), `重复的请求键 ${r.task}|${r.kind}`); m.set(`${r.task}|${r.kind}`, r.sha256); }
    return m;
  };
}

// ---------------------------------------------------------------- 1. 提示词

test("rag 的四段提示词和官方 rag-qa/index.ts 逐字相同", () => {
  const src = readFileSync(new URL("../rag-a/examples/patterns/rag-qa/index.ts", import.meta.url), "utf8");
  const rules = /const rules =\s*\n?\s*"((?:[^"\\]|\\.)*)";/.exec(src)?.[1];
  assert.ok(rules, "官方文件里找到 rules");
  assert.equal(RAG_RULES, JSON.parse(`"${rules}"`));
  for (const [name, mine] of [["planPrompt", RAG_PLAN_PROMPT], ["selectPrompt", RAG_SELECT_PROMPT], ["answerPrompt", RAG_ANSWER_PROMPT], ["verifyPrompt", RAG_VERIFY_PROMPT]] as const) {
    const body = new RegExp(`const ${name} =\\s*rules \\+\\s*\`([^\`]*)\`;`).exec(src)?.[1];
    assert.ok(body !== undefined, `官方文件里找到 ${name}`);
    assert.ok(!body.includes("${") && !body.includes("\\"), `${name} 里没有插值和转义，原文就是字面量`);
    assert.equal(mine, RAG_RULES + body, name);
  }
});

// ---------------------------------------------------------------- 2. S4

test("S4（假模型提示词）：Monolithic 和 Direct 同时跑 3 题，发给模型的每个请求逐字相同，结果相同；Direct 记下每次调用", { timeout: 300_000 }, async () => {
  assert.equal(REAL, false, "这个测试在不设 MEMCTX_REAL 时跑");
  const tasks = await firstTasks(3);
  const jobs: Job[] = tasks.map((t, i) => ({ scenario: "s4", key: `${i}:${t.id}`, run: `${i}:${t.id}`, tag: "direct-test", lme: t }));
  const config = runtimeConfig(`${FAKE}/v1`);

  const monoRows = await fakeSession("s4-mono");
  const mem = memoryWorker(join(ROOT, "s4-mono.sqlite"));
  const runtime = createDitto({ config, workers: [inferWorker(), mem.worker, contextWorker("memory", "default")] });
  let mono: any[];
  try { mono = await Promise.all(jobs.map(job => runtime.loop(taskLoop, job))); }
  finally { await runtime.close(); await mem.close(); }
  const a = await monoRows();

  const directRows = await fakeSession("s4-direct");
  const calls = new CallLog();
  const d = createDirectS4({ config, db: join(ROOT, "s4-direct.sqlite"), contextStore: "memory", calls });
  let direct: any[];
  try { direct = await Promise.all(jobs.map((job, i) => jobContext.run(i, () => d.run(job)))); }
  finally { await d.close(); }
  const b = await directRows();

  assert.equal(a.size, 6, "每题两次模型请求（检索词、回答）");
  assert.deepEqual(b, a, "Direct 和 Monolithic 发给模型的每个请求逐字相同");
  assert.deepEqual(direct, mono, "回答、检索词、检索条数相同");
  assert.ok(mono.every(r => r.hits > 0), "检索到了记忆（写进 MEMORY 的确实被查到）");
  const log = calls.toJSON();
  assert.deepEqual(new Set(log.nodes), new Set(["CONTEXT.LOAD", "CONTEXT.UPDATE", "CONTEXT.COMPRESS", "CONTEXT.SELECT", "MEMORY.WRITE", "MEMORY.SEARCH", "INFER.REASONING.SAMPLE"]));
  assert.deepEqual(new Set(log.rows.map(r => r[3])), new Set([0, 1, 2]), "每次调用记上了题的序号");
  assert.ok(log.rows.every(r => r[4] === 0 && Number(r[2]) >= 0 && Number(r[1]) > 1.7e12 && r.length === 5), "本进程执行、用时非负、开始时刻是 Unix 毫秒、本进程的调用不带执行标识");
});

// ---------------------------------------------------------------- 3. rag

type BuiltA = ReturnType<typeof buildRagTaskA>;
/** 写好题目文件和内部知识库（官方 SQLite 记忆），返回题目文件路径。 */
async function ragFixture(name: string, built: BuiltA[]) {
  const file = join(ROOT, `rag-tasks-${name}.jsonl`);
  writeFileSync(file, built.map(t => JSON.stringify(t.task)).join("\n") + "\n");
  const db = join(ROOT, `rag-${name}.sqlite`);
  const sqlite = openSqliteMemory(db);
  const memory = createMemory({ store: sqlite.store });
  for (const t of built) assert.equal((await memory.write({ memories: t.knowledge })).status, "success");
  await sqlite.close();
  return { file, db };
}
/** Monolithic：一个进程里的官方 rag-qa（同 rag.test.ts 的 withRag；没有 Redis，CONTEXT 放进程内存）。 */
async function ragMono(name: string, modelUrl: string, built: BuiltA[], onGraph?: (k: number, e: GraphMark) => void) {
  const { file, db } = await ragFixture(`mono-${name}`, built);
  const config = runtimeConfig(modelUrl);
  const memory = ragMemoryWorker(db, config);
  const dispatch = new RagDispatch(file, join(ROOT, `dispatch-mono-${name}`));
  const w = ragInteractionWorkers(dispatch);
  const context = createContextWorker({ ...(config.context.policy ? { policy: config.context.policy } : {}),
    servicesFactory: () => ({ stateStore: createInMemoryContextStore(LOCAL_CONTEXT_STORE) }) });
  const runtime = createDitto({ config, sandbox: ragSandbox(config), workers: [memory.worker, context, createInferWorker(), w.retrieval, w.interaction] });
  try {
    return await Promise.all(built.map((t, k) => runtime.loop(runRagLoop, [ragInput(`${name}-${k}`, t.task, MODEL), {}],
      onGraph ? { onGraph: e => onGraph(k, { graphId: e.graphId, status: e.status }) } : {}).then(r => r, e => ({ error: e }))));
  } finally { await runtime.close(); await memory.close(); dispatch.close(); }
}
async function ragDirect(name: string, modelUrl: string, built: BuiltA[], onMark?: (k: number, m: GraphMark) => void) {
  const { file, db } = await ragFixture(`direct-${name}`, built);
  const calls = new CallLog();
  const d = createDirectRag({ config: runtimeConfig(modelUrl), db, tasksFile: file, calls, root: join(ROOT, `dispatch-direct-${name}`) });
  try {
    const out = await Promise.all(built.map((t, k) => d.run(ragInput(`${name}-${k}`, t.task, MODEL), m => onMark?.(k, m)).then(r => r, e => ({ error: e }))));
    // 发布以后这一题收尾；失败的题没有发布，和 Monolithic 一样等关掉时（close）才收尾
    assert.equal(d.dispatch.active, out.filter(r => "error" in r).length, "发布过的题都收尾了");
    return { out, calls: calls.toJSON() };
  } finally { await d.close(); }
}

test("rag（官方 rag-qa，假模型）：Monolithic 和 Direct 两题每一步的请求逐字相同，官方报告逐字相同", { timeout: 300_000 }, async () => {
  const built = (await firstTasks(2)).map(buildRagTaskA);
  const monoRows = await fakeSession("rag-mono");
  const mono = await ragMono("same", `${FAKE}/v1`, built);
  const a = await monoRows();
  const directRows = await fakeSession("rag-direct");
  const { out: direct, calls } = await ragDirect("same", `${FAKE}/v1`, built);
  const b = await directRows();
  assert.ok(mono.every((r: any) => r.answer?.status === "answered"), "两题都发布出报告");
  assert.equal(a.size, 8, "每题 4 步（理解问题、挑证据、回答、核对）");
  assert.deepEqual(b, a, "每一步的请求逐字相同");
  assert.equal(JSON.stringify(direct), JSON.stringify(mono), "官方报告逐字相同");
  assert.deepEqual(new Set(calls.nodes), new Set(["MEMORY.GET", "MEMORY.WRITE", "INTERACTION.ACT.TOOL", "CONTEXT.LOAD", "CONTEXT.UPDATE", "INFER.REASONING.SAMPLE", "RETRIEVAL.SEARCH"]));
});

/** 本进程里按步骤回固定内容的模型（同 rag.test.ts）；没给的步骤回 HTTP 500。 */
async function scriptedModel(replies: Partial<Record<string, string>>) {
  const { ragKind } = await import("../src/rag-fake.ts");
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const c of req) raw += c;
    const body = JSON.parse(raw);
    const content = replies[ragKind(String(body.messages?.[0]?.content ?? "")) ?? ""];
    if (content === undefined) { res.writeHead(500, { "content-type": "application/json" }); res.end("{}"); return; }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ id: "x", object: "chat.completion", created: 0, model: body.model,
      choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
  });
  await new Promise<void>(r => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`,
    close: () => new Promise<void>(r => { server.close(() => r()); server.closeAllConnections(); }) };
}

test("rag 失败分类：模型输出过不了官方检查、模型服务出错时，Direct 和 Monolithic 的错误、最近一张图、分类都一样", { timeout: 300_000 }, async () => {
  const built = (await firstTasks(1)).map(buildRagTaskA);
  const cases: [string, Partial<Record<string, string>>][] = [
    ["not-json", { plan: "this is not json" }],
    ["four-queries", { plan: JSON.stringify({ intent: "x", queries: ["cold", "health", "issue", "doctor"], facets: ["f"], clarification: null }) }],
    ["provider-500", {}],
  ];
  for (const [name, replies] of cases) {
    const model = await scriptedModel(replies);
    try {
      let lastMono: GraphMark | undefined, lastDirect: GraphMark | undefined;
      const [m] = await ragMono(`bad-${name}`, model.url, built, (_, e) => { lastMono = e; }) as any[];
      const { out: [d] } = await ragDirect(`bad-${name}`, model.url, built, (_, e) => { lastDirect = e; }) as any;
      assert.ok(m.error && d.error, `${name}：两边都失败`);
      assert.equal(String(d.error.message), String(m.error.message), `${name}：错误文字相同`);
      assert.equal(d.error.constructor.name, m.error.constructor.name, `${name}：错误类型相同`);
      assert.deepEqual(lastDirect, lastMono, `${name}：出错时最近一张图相同`);
      assert.equal(ragFailKind(d.error, lastDirect), ragFailKind(m.error, lastMono), `${name}：分类相同`);
      assert.equal(ragFailKind(d.error, lastDirect), name === "provider-500" ? "infra" : "model-output", `${name}：分类`);
    } finally { await model.close(); }
  }
});

// ---------------------------------------------------------------- 4. S4 真实模型那一套提示词，经出口

test("S4（真实模型提示词，经出口）：两边请求逐字相同；出口记下完整请求体（去重）和每次的完整回复", { timeout: 300_000 }, async () => {
  const ledger = join(ROOT, "egress-ledger");
  const child = spawn(process.execPath, ["--no-warnings", fileURLToPath(new URL("./direct-real-child.ts", import.meta.url)), `${FAKE}/v1`, ledger, "3"],
    { env: { ...process.env, MEMCTX_REAL: "1" }, stdio: ["ignore", "pipe", "inherit"] });
  let text = ""; child.stdout!.on("data", c => { text += c; });
  const code = await new Promise<number | null>(r => child.once("exit", r));
  assert.equal(code, 0, `子进程退出码 ${code}：${text.slice(-500)}`);
  const out = JSON.parse(text.trim().split("\n").at(-1)!) as { mono: Record<string, string>; direct: Record<string, string>; answersSame: boolean; real: boolean };
  assert.equal(out.real, true, "子进程用的是真实模型那一套提示词");
  assert.equal(Object.keys(out.mono).length, 6);
  assert.deepEqual(out.direct, out.mono, "Direct 和 Monolithic 交给出口的请求逐字相同（出口加防缓存编号之前）");
  assert.ok(out.answersSame, "回答、检索词、检索条数相同");
  // 出口的完整记录：请求体按摘要去重（两边相同，所以只有 6 条）；每次尝试一条完整回复（12 次）
  const lines = (f: string) => readFileSync(join(ledger, f), "utf8").split("\n").filter(Boolean);
  const bodies = lines("bodies.jsonl").map(l => JSON.parse(l) as { sha256: string; body: string });
  assert.equal(bodies.length, 6);
  assert.deepEqual(new Set(bodies.map(b => b.sha256)), new Set(Object.values(out.mono)));
  const { createHash } = await import("node:crypto");
  for (const b of bodies) assert.equal(createHash("sha256").update(b.body).digest("hex"), b.sha256, "请求体全文和摘要对得上");
  assert.deepEqual(lines("bodies.index"), bodies.map(b => b.sha256));
  const responses = lines("responses.jsonl").map(l => JSON.parse(l));
  const requests = lines("requests.jsonl").map(l => JSON.parse(l));
  assert.equal(responses.length, 12);
  assert.ok(responses.every(r => r.status === 200 && JSON.parse(r.body).choices[0].message.content), "完整回复");
  assert.deepEqual(new Set(responses.map(r => r.id)), new Set(requests.map(r => `${r.id}#1`)), "回复和请求按编号对得上");
  assert.ok(requests.every(r => /^[0-9a-f]{16}$/.test(r.rid)), "记下了防缓存编号");
});
