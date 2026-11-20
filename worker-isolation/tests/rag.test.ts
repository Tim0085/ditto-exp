// rag 场景（docs\24）的测试：切来源（rag-sources.ts，切法 (a)、(b)）、rag-a 副本、假模型回复（rag-fake.ts）、按题分派（rag.ts）、
// 报告转文字和失败分类，以及官方 rag-qa 流程在一个进程里用假模型完整走通。不联网、不碰 Docker。node --test tests/rag.test.ts
// 临时文件放 F 盘 sec1-2.0\_tmp\scratch\rag-test\，测完删掉。
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDitto } from "@codesoul-co/ditto/runtime";
import { createContextWorker, createInMemoryContextStore } from "@codesoul-co/ditto/worker/context";
import { createInferWorker } from "@codesoul-co/ditto/worker/infer";
import { answer, chunks, plan, request, selection, verify } from "../official/examples/_shared/tools/rag/domain.ts";
import { chunks as chunksA, request as requestA } from "../rag-a/examples/_shared/tools/rag/domain.ts";
import { runRagLoop } from "../rag-a/examples/patterns/rag-qa/index.ts";
import { buildRagTask, buildRagTaskA, countChunks, packSessions, sessionText, splitOversized, wrapLongLines, type LmeInput } from "../src/rag-sources.ts";
import { EVIDENCE_CHARS, ragKind, ragReply, ragTask } from "../src/rag-fake.ts";
import { RagDispatch, ragAnswerText, ragFailKind, ragInput, ragInteractionWorkers, ragMemoryWorker, ragRequest, ragSandbox, type GraphMark } from "../src/rag.ts";
import { runtimeConfig } from "../src/app.ts";
import { MODEL, jsonLines, sha256 } from "../src/common.ts";

const ROOT = fileURLToPath(new URL("../../_tmp/scratch/rag-test/", import.meta.url));
rmSync(ROOT, { recursive: true, force: true });
mkdirSync(ROOT, { recursive: true });
after(() => rmSync(ROOT, { recursive: true, force: true }));
const DATA = fileURLToPath(new URL("../../bench_data/longmemeval-s/derived/memctx-lme-first200.jsonl", import.meta.url));
const probe = { id: "p", tenant: "lme", readers: ["user"], title: "p", kind: "internal" as const, ref: "knowledge:lme:p" };

async function firstTasks(n: number): Promise<LmeInput[]> {
  const out: LmeInput[] = [];
  for await (const line of jsonLines(DATA)) { out.push(JSON.parse(line)); if (out.length === n) break; }
  return out;
}

test("折行：只插入换行，每行 ≤1600；短行不动", () => {
  const long = ("word ".repeat(700) + "x".repeat(2000)).trim();
  const text = `a\n${long}\nb`;
  const w = wrapLongLines(text);
  assert.equal(w.wrapped, 1);
  assert.equal(w.text.replaceAll("\n", ""), text.replaceAll("\n", ""));
  assert.ok(w.text.split("\n").every(l => l.length <= 1600));
  assert.deepEqual(wrapLongLines("short\nlines"), { text: "short\nlines", wrapped: 0 });
});

test("数块和官方 chunks() 一致；超过 200 块的 session 在空行处分段，文字不变", () => {
  const paragraphs = Array.from({ length: 450 }, (_, i) => `paragraph ${i} ` + "z".repeat(i % 7 * 40));
  const text = "Session Date: d\nSession Content:\n\n" + paragraphs.join("\n\n");
  assert.ok(countChunks(text) > 200);
  const parts = splitOversized(text);
  assert.ok(parts.length >= 3);
  assert.equal(parts.join("\n\n"), text);
  for (const p of parts) assert.equal(countChunks(p), chunks(probe, p).length); // 每段都过得了官方切块
  assert.deepEqual(splitOversized("small\n\ntext"), ["small\n\ntext"]);
});

test("装组：不超过 20 个来源，每个 ≤10 万字、≤200 块，保持原顺序", () => {
  const pieces = Array.from({ length: 61 }, (_, i) => ({ date: `2023/05/${String(i % 28 + 1).padStart(2, "0")}`, text: "t".repeat(9000 + i * 100), chunks: 6 }));
  const groups = packSessions(pieces);
  assert.ok(groups.length <= 20);
  assert.deepEqual(groups.flat(), pieces);
  for (const g of groups) assert.ok(g.reduce((n, p) => n + p.text.length, 0) + 2 * (g.length - 1) <= 100_000);
});

test("真实题目：官方 request()/sources()/chunks() 全部通过；来源里看不到题号（_abs）", async () => {
  for (const t of await firstTasks(5)) {
    const built = buildRagTask(t);
    assert.ok(built.task.sources.length <= 20);
    const all = built.knowledge.map(k => k.content.text).join("\n\n");
    assert.ok(t.sessions.every(s => all.includes(`Session Date: ${s.date}\nSession Content:`)), "每个 session 都在");
    const visible = JSON.stringify(built.task.sources) + JSON.stringify(built.knowledge.map(k => [k.key, k.content.title]));
    assert.ok(!visible.includes(t.id), "题号不出现在模型能看到的地方");
    assert.ok(built.task.question.startsWith(`Current Date: ${t.question_date}\nQuestion: `));
  }
});

test("假模型的 4 步回复都过得了官方校验", () => {
  const items = (data: unknown) => JSON.stringify([{ id: "question", content: "Current Date: 2023/06/01\nQuestion: Where did I buy the dog bed?" },
    { id: "working-set", content: data }]);
  const sys = (s: string) => `prefix ${s} suffix`;
  assert.equal(ragKind(sys("Understand the question and construct retrieval queries")), "plan");
  assert.equal(ragKind("something else"), undefined);
  const p = plan(JSON.parse(ragReply(sys("Understand the question and construct retrieval queries"), items({ sources: [], question: "q" }))!.content));
  assert.ok(p.queries.length >= 1 && p.clarification === null);
  const pool = Array.from({ length: 12 }, (_, i) => ({ id: `c${i}`, text: `line ${i}\n` + "y".repeat(1700) }));
  const sel = selection(JSON.parse(ragReply(sys("Select relevant chunks from the retrieved pool"), items({ plan: p, pool }))!.content), pool);
  const evidence = sel.selectedIds.map(id => pool.find(c => c.id === id)!);
  assert.ok(evidence.length >= 1 && evidence.reduce((n, c) => n + c.text.length, 0) <= EVIDENCE_CHARS);
  const draft = answer(JSON.parse(ragReply(sys("Generate a useful grounded answer"), items({ plan: p, selection: sel, evidence, expectedStatus: "answered" }))!.content), evidence, sel);
  assert.equal(draft.status, "answered");
  verify(JSON.parse(ragReply(sys("Independently verify the draft"), items({ question: "q", selection: sel, evidence, draft }))!.content), draft.claims.length);
  // 同一题同一步，回复和题号都一样（两种布局下等一样久）
  const a = ragReply(sys("Select relevant chunks from the retrieved pool"), items({ plan: p, pool }))!;
  const b = ragReply(sys("Select relevant chunks from the retrieved pool"), items({ plan: p, pool }))!;
  assert.deepEqual(a, b);
});

test("按题分派：几道题交错调用官方工具和检索，各自的题库互不串", async () => {
  const tasks = (await firstTasks(2)).map(buildRagTask);
  const file = join(ROOT, "tasks-dispatch.jsonl");
  writeFileSync(file, tasks.map(t => JSON.stringify(t.task)).join("\n") + "\n");
  const dispatch = new RagDispatch(file, join(ROOT, "dispatch"));
  const tools = new Map(dispatch.tools.map(t => [t.name, t]));
  const ctx = {} as never;
  const call = async (name: string, args: any) => {
    const r = await tools.get(name)!.execute(JSON.parse(JSON.stringify(args)), ctx);
    assert.equal(r.status, "success", `${name}: ${JSON.stringify(r)}`);
    return r.structuredContent as any;
  };
  const requests = tasks.map((t, k) => ragRequest(`run-${k}`, t.task));
  for (const r of requests) await call("rag_authorize", { request: r });
  const indexed: any[] = [];
  for (const t of tasks) indexed.push((await call("rag_ingest", { memories: Object.fromEntries(t.knowledge.map(k => [k.key, k])) })).chunks);
  for (const chunksOfTask of indexed) await call("rag_check_sources", { evidence: chunksOfTask, verifyIndex: true });
  for (const [k, r] of requests.entries()) {
    const target = { name: "rag-corpus", namespace: `${r.tenant}:${r.principal}:${r.id}` };
    const out = await dispatch.providers.resolve(target, "bm25").search({ target, query: { content: "the" }, limit: 12 });
    const own = new Set(indexed[k].map((c: any) => c.id));
    assert.ok(out.candidates.length > 0 && out.candidates.every(c => own.has(c.id)), "只取到自己这一题的块");
  }
  assert.equal(dispatch.active, 2);
  // 同一题重新开始：上一次收尾，新的一次照常
  await call("rag_authorize", { request: ragRequest("run-0b", tasks[0]!.task) });
  assert.equal(dispatch.active, 2);
  dispatch.close();
  assert.equal(dispatch.active, 0);
});

// ---------------------------------------------------------------- 切法 (a)（正式用）

test("rag-a 副本：index.ts、adapters.ts、evidence.ts、files.ts 和官方逐字相同；domain.ts 只改了两行", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
  for (const f of ["examples/patterns/rag-qa/index.ts", "examples/_shared/tools/rag/adapters.ts", "examples/_shared/tools/evidence.ts", "examples/_shared/tools/execution/files.ts"]) {
    assert.equal(read(`../rag-a/${f}`), read(`../official/${f}`), f);
  }
  const a = read("../rag-a/examples/_shared/tools/rag/domain.ts").split("\n"), o = read("../official/examples/_shared/tools/rag/domain.ts").split("\n");
  assert.equal(a.length, o.length);
  assert.deepEqual(o.map((line, i) => [line, a[i]]).filter(([x, y]) => x !== y), [
    ["  const sourceIds = strings(r.sourceIds, 20).map(identifier);", "  const sourceIds = strings(r.sourceIds, 100).map(identifier);"],
    ['  if (result.length > 200) throw new Error("Too many document chunks");', '  if (result.length > 400) throw new Error("Too many document chunks");'],
  ]);
});

test("rag-a 的上限：每题来源 ≤100、每个来源 ≤400 块（官方 20、200）；每行 1600 字等其余检查照旧", () => {
  const paragraphs = (n: number) => Array.from({ length: n }, (_, i) => `paragraph ${i}`).join("\n\n");
  assert.throws(() => chunks(probe, paragraphs(301)), /Too many document chunks/);
  assert.equal(chunksA(probe, paragraphs(301)).length, 301);
  assert.throws(() => chunksA(probe, paragraphs(401)), /Too many document chunks/);
  assert.throws(() => chunksA(probe, "x".repeat(1601)), /Normalize long lines/);
  assert.throws(() => chunksA(probe, "y".repeat(100_001)), /1–100000/);
  const req = (n: number) => ({ id: "r", tenant: "lme", principal: "user", question: "q", sourceIds: Array.from({ length: n }, (_, i) => `s${i}`) });
  assert.throws(() => request(req(21)), /Invalid list/);
  requestA(req(100));
  assert.throws(() => requestA(req(101)), /Invalid list/);
});

test("切法 (a)：一段对话一个来源，按日期排，编号不透明、标题是日期；文字只多了折行；看不到题号和 session 编号", async () => {
  for (const t of await firstTasks(5)) {
    const built = buildRagTaskA(t);
    assert.equal(built.task.sources.length, t.sessions.length);
    const sorted = t.sessions.map((s, i) => ({ s, i })).sort((a, b) => (a.s.date < b.s.date ? -1 : a.s.date > b.s.date ? 1 : a.i - b.i)).map(x => x.s);
    built.task.sources.forEach((src, k) => {
      const s = sorted[k]!;
      assert.equal(src.id, `s${String(k + 1).padStart(2, "0")}`);
      assert.equal(src.title, `Session ${s.date}`);
      const text = built.knowledge[k]!.content.text;
      assert.equal(text.replaceAll("\n", ""), sessionText(s.date, s.messages).replaceAll("\n", ""), "只插入换行");
      assert.ok(text.split("\n").every(l => l.length <= 1600));
      assert.equal(built.knowledge[k]!.key, src.ref);
    });
    const visible = JSON.stringify(built.task.sources) + JSON.stringify(built.knowledge.map(k => [k.key, k.content.title]));
    assert.ok(!visible.includes(t.id), "题号不出现在模型能看到的地方");
    for (const s of t.sessions) assert.ok(!visible.includes((s as unknown as { id: string }).id), "session 编号（含 answer_）不出现");
    assert.ok(built.task.question.startsWith(`Current Date: ${t.question_date}\nQuestion: `));
  }
});

test("官方报告转成判分用的文字：结论句依次连起来；状态不是 answered 时接上 limitations", () => {
  const report = (status: string, claims: string[], limitations: string[]) =>
    ({ answer: { status, claims: claims.map(text => ({ text, citations: [] })), limitations } }) as never;
  assert.equal(ragAnswerText(report("answered", ["You bought six pairs.", "That was in May."], [])), "You bought six pairs.\nThat was in May.");
  assert.equal(ragAnswerText(report("insufficient-evidence", ["You mentioned a dog."], ["The dog's name was not found."])),
    "You mentioned a dog.\nThe dog's name was not found.");
  assert.equal(ragAnswerText(report("insufficient-evidence", [], ["No sufficient evidence was found in the authorized sources."])),
    "No sufficient evidence was found in the authorized sources.");
  assert.equal(ragAnswerText(report("needs-clarification", [], ["Which trip do you mean?"])), "Which trip do you mean?");
});

test("出口认题：从 user 消息的条目里取问题文字的哈希；认不出返回 undefined", () => {
  const q = "Current Date: 2023/05/30 (Tue) 22:09\nQuestion: What health issue did I initially think was just a cold?";
  assert.equal(ragTask(JSON.stringify([{ id: "question", content: q }, { id: "working-set", content: {} }])), `rag:${sha256(q).slice(0, 16)}`);
  assert.equal(ragReply("Understand the question and construct retrieval queries", JSON.stringify([{ id: "question", content: q }]))!.task, `rag:${sha256(q).slice(0, 16)}`);
  for (const bad of ["not json", "{}", JSON.stringify([{ id: "working-set", content: q }]), JSON.stringify([{ id: "question", content: "  " }])]) assert.equal(ragTask(bad), undefined);
});

type BuiltA = ReturnType<typeof buildRagTaskA>;
/** 一个进程里的官方 rag-qa（MEMORY、CONTEXT、INFER、RETRIEVAL、INTERACTION 都在本进程），模型接 modelUrl。 */
async function withRag<T>(name: string, modelUrl: string, built: BuiltA[],
  fn: (run: (k: number, onGraph?: (e: { graphId: string; status: string }) => void) => Promise<any>, dispatch: RagDispatch) => Promise<T>) {
  const file = join(ROOT, `tasks-${name}.jsonl`);
  writeFileSync(file, built.map(t => JSON.stringify(t.task)).join("\n") + "\n");
  const config = runtimeConfig(modelUrl);
  const memory = ragMemoryWorker(join(ROOT, `memory-${name}.sqlite`), config);
  const dispatch = new RagDispatch(file, join(ROOT, name));
  const w = ragInteractionWorkers(dispatch);
  // 测试里没有 Redis：CONTEXT 用核心自带的进程内存储，政策照官方（正式运行照官方用 Redis）。
  const context = createContextWorker({ ...(config.context.policy ? { policy: config.context.policy } : {}),
    servicesFactory: () => ({ stateStore: createInMemoryContextStore({ ttlMs: 3_600_000, maxEntries: 1000 }) }) });
  const runtime = createDitto({ config, sandbox: ragSandbox(config), workers: [memory.worker, context, createInferWorker(), w.retrieval, w.interaction] });
  try {
    for (const t of built) assert.equal((await runtime.invoke("MEMORY.WRITE", { memories: t.knowledge })).status, "success");
    return await fn((k, onGraph) => runtime.loop(runRagLoop, [ragInput(`${name}-${k}`, built[k]!.task, MODEL), {}], onGraph ? { onGraph } : {}), dispatch);
  } finally { await runtime.close(); await memory.close(); dispatch.close(); }
}

test("官方 rag-qa 流程整条走通（切法 (a)，一个进程，假模型）：两道题都发布出报告；证据块带日期；回答文字是结论句", { timeout: 120_000 }, async () => {
  const port = 18991;
  const fake = spawn(process.execPath, ["--no-warnings", fileURLToPath(new URL("../src/fake-model.ts", import.meta.url)), "--port", String(port)], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise<void>((resolve, reject) => { fake.stdout!.once("data", () => resolve()); fake.once("exit", code => reject(new Error(`假模型退出 ${code}`))); });
  try {
    const built = (await firstTasks(2)).map(buildRagTaskA);
    await withRag("loop", `http://127.0.0.1:${port}/v1`, built, async (run, dispatch) => {
      for (const k of [0, 1]) {
        const report: any = await run(k);
        assert.equal(report.answer.status, "answered", JSON.stringify(report.answer));
        assert.ok(report.evidence.length >= 1 && report.answer.claims.length >= 1);
        assert.ok(report.evidence.every((c: any) => /^Session \d{4}\/\d{2}\/\d{2} \(\w{3}\) \d{2}:\d{2}$/.test(c.title)), "证据块带着这段对话的日期");
        assert.equal(ragAnswerText(report), report.answer.claims.map((c: any) => c.text).join("\n"));
      }
      assert.equal(dispatch.active, 0, "发布以后这一题收尾");
    });
  } finally { fake.kill(); }
});

/** 本进程里按步骤回固定内容的模型（按官方提示词认步骤）；没给的步骤回 HTTP 500。 */
async function scriptedModel(replies: Partial<Record<string, string>>) {
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

test("失败分类：紧跟在成功的模型调用之后的格式、官方检查错误认作 model-output；其余（检查点、传输、工具、请求本身）认作 infra", { timeout: 120_000 }, async t => {
  const built = (await firstTasks(1)).map(buildRagTaskA);
  const cases = [["not-json", "this is not json"], ["four-queries", JSON.stringify({ intent: "x", queries: ["cold", "health", "issue", "doctor"], facets: ["f"], clarification: null })]];
  for (const [name, reply] of cases) {
    const model = await scriptedModel({ plan: reply! });
    try {
      let last: GraphMark | undefined;
      const error: any = await withRag(`bad-${name}`, model.url, built, run => run(0, e => { last = { graphId: e.graphId, status: e.status }; }).then(() => undefined, e => e));
      assert.ok(error, `${name}：应当失败`);
      t.diagnostic(`${name}：${error?.constructor?.name}：${String(error?.message ?? error).slice(0, 200)}；最近一张图 ${JSON.stringify(last)}`);
      assert.deepEqual(last, { graphId: "rag-context-and-model", status: "completed" }, "出错时最近一张图是模型那张、已完成");
      assert.equal(ragFailKind(error, last), "model-output", `${name}：${String(error?.message ?? error)}`);
    } finally { await model.close(); }
  }
  const afterModel = { graphId: "rag-context-and-model", status: "completed" };
  // 反例：文字和模型输出不合格一样，但不是模型的输出出的错
  assert.equal(ragFailKind(new Error("Expected object"), { graphId: "rag-checkpoint-read", status: "completed" }), "infra", "检查点读出坏数据");
  assert.equal(ragFailKind(new SyntaxError("Unexpected token"), { graphId: "rag-application-tool", status: "started" }), "infra", "被拆出的 Worker 回了坏 JSON");
  assert.equal(ragFailKind(new Error("Invalid list"), undefined), "infra", "第一张图之前：请求本身不合格");
  assert.equal(ragFailKind(new Error("Worker failed: INFER_PROVIDER_ERROR"), afterModel), "infra");
  assert.equal(ragFailKind(new Error("Tool failed: TOOL_EXECUTION_FAILED"), { graphId: "rag-application-tool", status: "completed" }), "infra");
  assert.equal(ragFailKind(new Error("Unsupported citation"), afterModel), "model-output");
  assert.equal(ragFailKind(new Error("Unsupported citation"), { graphId: "rag-context-and-model", status: "started" }), "infra", "模型那张图没完成");
  assert.equal(ragFailKind(new Error("Retrieval budget exceeded; narrow the request"), { graphId: "rag-document-search", status: "completed" }), "model-output");
  assert.equal(ragFailKind(new Error("Context budget exceeded; narrow the request"), afterModel), "model-output");
});
