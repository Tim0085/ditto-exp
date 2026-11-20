// 出口（src\egress.ts）的测试：用本机的假上游，不联网、不花钱。node --test tests/egress.test.ts
// 临时文件放 F 盘 sec1-2.0\_tmp\scratch\egress-test\，测完删掉。
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Ledger, PEAK_RATES, costMicro, endsWithNewline, keyPrefix, readDeepSeekKey, redact, startEgress } from "../src/egress.ts";
import { sha256 } from "../src/common.ts";

const ROOT = fileURLToPath(new URL("../../_tmp/scratch/egress-test/", import.meta.url));
mkdirSync(ROOT, { recursive: true });
after(() => rmSync(ROOT, { recursive: true, force: true }));
// 测试用的前缀和密钥都是假的（真的前缀只在不进 git 的 local.env 里）。
process.env.DEEPSEEK_KEY_PREFIX = "sk-tst";
const KEY = "sk-tstTEST000000000000";

/** trickleMs：先给响应头，每隔这么久发一个空行，delayMs 以后才给回答（DeepSeek 排队时就是这样，11-19 那一轮等了约 10 分钟）。 */
type Behavior = { status: number; body: unknown; delayMs?: number; trickleMs?: number } | "destroy";
/** 默认用量贴近真实（输入 token 远少于字节数）：10×0.04 + 20×2 + 5×8 = 80.4，向上取整 81 微元。 */
const OK_COST = 81;
const ok = (usage: object = { prompt_tokens: 30, completion_tokens: 5, prompt_cache_hit_tokens: 10, prompt_cache_miss_tokens: 20 }): Behavior =>
  ({ status: 200, body: { id: "x", model: "deepseek-flash", choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }], usage } });

async function upstream(behaviors: Behavior[]) {
  const received: { auth: string; body: any }[] = [];
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const c of req) raw += c;
    received.push({ auth: String(req.headers.authorization), body: JSON.parse(raw) });
    const b = behaviors.shift() ?? ok();
    if (b === "destroy") { req.socket.destroy(); return; }
    if (b.trickleMs) {
      res.writeHead(b.status, { "content-type": "application/json" }); res.write("\n");
      const tick = setInterval(() => { if (!res.destroyed) res.write("\n"); }, b.trickleMs);
      await new Promise(r => setTimeout(r, b.delayMs ?? 0)); clearInterval(tick);
      if (!res.destroyed) res.end(JSON.stringify(b.body));
      return;
    }
    if (b.delayMs) await new Promise(r => setTimeout(r, b.delayMs));
    if (res.destroyed) return;
    res.writeHead(b.status, { "content-type": "application/json" }); res.end(JSON.stringify(b.body));
  });
  await new Promise<void>(r => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as any).port}`, received, close: () => new Promise<void>(r => { server.close(() => r()); server.closeAllConnections(); }) };
}

async function setup(behaviors: Behavior[], options: { limitMicroCny?: number; maxConsecutiveFailures?: number; attemptTimeoutMs?: number; deadlineMs?: number } = {}) {
  const up = await upstream(behaviors);
  const ledgerDir = mkdtempSync(join(ROOT, "ledger-"));
  const egress = await startEgress({ port: 0, host: "127.0.0.1", apiKey: KEY, upstream: up.url, model: "deepseek-flash", ledgerDir,
    limitMicroCny: options.limitMicroCny ?? 10_000_000, attemptTimeoutMs: options.attemptTimeoutMs ?? 2_000, backoffMs: [1, 1],
    ...(options.deadlineMs ? { deadlineMs: options.deadlineMs } : {}),
    ...(options.maxConsecutiveFailures ? { maxConsecutiveFailures: options.maxConsecutiveFailures } : {}) });
  await fetch(`${egress.url}/reset?seed=cellA`, { method: "POST" });
  return { up, egress, ledgerDir, close: async () => { await egress.close(); await up.close(); } };
}

const dittoBody = (step = "answer", extra: object = {}) => ({ model: "deepseek-flash", n: 1, stream: false, max_tokens: 100, temperature: 0,
  messages: [{ role: "system", content: "SYS" }, { role: "user", content: `[task:7:abc][step:${step}]Hello` }], ...extra });
async function post(url: string, body: unknown) {
  const text = JSON.stringify(body);
  const r = await fetch(`${url}/v1/chat/completions`, { method: "POST", headers: { "content-type": "application/json" }, body: text });
  return { status: r.status, json: await r.json() as any, text };
}
/** 和出口同样的预扣：编号是 16 位十六进制（长度固定，内容随机）；每条消息加 64 token 封装余量。 */
const reserveFor = (body: any) => {
  const out = body.messages.map((m: any, i: number) => ({ role: m.role,
    content: i === 0 ? `Request ID: ${"0".repeat(16)}\n\n${m.content}` : m.content.replace(/^\[task:[^\]]*\]\[step:[a-z]+\]/, "") }));
  return Math.ceil((Buffer.byteLength(JSON.stringify(out)) + 64 * out.length) * PEAK_RATES.uncachedInput + body.max_tokens * PEAK_RATES.output);
};

test("转发：去掉内部标记、加 Request ID、关闭思考、只转发该转的字段，按 usage 结算", async () => {
  const s = await setup([ok()]);
  try {
    const body = dittoBody();
    const r = await post(s.egress.url, body);
    assert.equal(r.status, 200);
    assert.equal(r.json.choices[0].message.content, "hi");
    const sent = s.up.received[0]!;
    assert.equal(sent.auth, `Bearer ${KEY}`);
    assert.deepEqual(sent.body.thinking, { type: "disabled" });
    assert.equal(sent.body.model, "deepseek-flash");
    assert.equal(sent.body.stream, false);
    assert.equal(sent.body.max_tokens, 100);
    assert.equal(sent.body.temperature, 0);
    assert.equal(sent.body.n, undefined);
    assert.match(sent.body.messages[0].content, /^Request ID: [0-9a-f]{16}\n\nSYS$/);
    assert.equal(sent.body.messages[1].content, "Hello");
    assert.equal(s.egress.ledger.spent, OK_COST);
    assert.equal(s.egress.state.stopped, false);
    assert.equal(s.egress.ledger.held, 0);
    assert.equal(s.egress.ledger.reserved, 0);
    const stats = await (await fetch(`${s.egress.url}/stats`)).json() as any;
    assert.equal(stats.rows.length, 1);
    assert.equal(stats.rows[0].sha256, sha256(r.text), "记的是 Ditto 发来的原始请求体的哈希");
    assert.equal(stats.rows[0].task, "7:abc");
    assert.equal(stats.rows[0].kind, "answer");
    assert.equal(stats.realModelCalls, 1);
  } finally { await s.close(); }
});

test("max_completion_tokens 也认；关闭思考覆盖 Ditto 可能带来的设置", async () => {
  const s = await setup([ok()]);
  try {
    const body: any = dittoBody("keyword", { max_tokens: undefined, max_completion_tokens: 64, thinking: { type: "enabled" }, reasoning_effort: "high" });
    assert.equal((await post(s.egress.url, body)).status, 200);
    const sent = s.up.received[0]!.body;
    assert.equal(sent.max_tokens, 64);
    assert.deepEqual(sent.thinking, { type: "disabled" });
    assert.equal(sent.reasoning_effort, undefined);
    assert.equal(sent.max_completion_tokens, undefined);
  } finally { await s.close(); }
});

test("没有内部标记、模型不对、流式：拒绝，不发到上游", async () => {
  const s = await setup([]);
  try {
    assert.equal((await post(s.egress.url, dittoBody("answer", { model: "other" }))).status, 400);
    assert.equal((await post(s.egress.url, dittoBody("answer", { stream: true }))).status, 400);
    assert.equal((await post(s.egress.url, { ...dittoBody(), messages: [{ role: "user", content: "no marker" }] })).status, 400);
    assert.equal(s.up.received.length, 0);
    assert.equal(s.egress.ledger.used, 0);
  } finally { await s.close(); }
});

test("预算不够：不发、停下，之后的请求和 /reset 都拒绝", async () => {
  const s = await setup([ok()], { limitMicroCny: 100 });
  try {
    const r = await post(s.egress.url, dittoBody());
    assert.equal(r.status, 503);
    assert.equal(s.up.received.length, 0);
    assert.equal(s.egress.state.stopped, true);
    assert.equal((await fetch(`${s.egress.url}/reset?seed=next`, { method: "POST" })).status, 503);
    assert.equal((await post(s.egress.url, dittoBody())).status, 503);
    assert.equal(s.up.received.length, 0);
  } finally { await s.close(); }
});

test("预扣按最坏情况：在途的预扣也占额度", async () => {
  const body = dittoBody();
  const need = reserveFor(body);
  const s = await setup([ok()], { limitMicroCny: need - 1 });
  try {
    assert.equal((await post(s.egress.url, body)).status, 503, "差 1 微元也不发");
  } finally { await s.close(); }
  const s2 = await setup([ok()], { limitMicroCny: need });
  try { assert.equal((await post(s2.egress.url, body)).status, 200, "刚好够就发"); } finally { await s2.close(); }
});

test("503 后重试成功：失败那次不收钱", async () => {
  const s = await setup([{ status: 503, body: { error: "busy" } }, ok()]);
  try {
    assert.equal((await post(s.egress.url, dittoBody())).status, 200);
    assert.equal(s.up.received.length, 2);
    assert.equal(s.egress.ledger.held, 0);
    assert.equal(s.egress.ledger.spent, OK_COST);
  } finally { await s.close(); }
});

test("发出去以后断线：保留预扣（计入已花），重试用完返回 502", async () => {
  const body = dittoBody();
  const s = await setup(["destroy", "destroy", "destroy"]);
  try {
    assert.equal((await post(s.egress.url, body)).status, 502);
    assert.equal(s.up.received.length, 3);
    assert.equal(s.egress.ledger.held, 3 * reserveFor(body));
    assert.equal(s.egress.ledger.reserved, 0);
  } finally { await s.close(); }
});

test("回复里没有 usage：照常返回，但预扣全额保留", async () => {
  const body = dittoBody();
  const s = await setup([{ status: 200, body: { choices: [{ message: { role: "assistant", content: "x" }, finish_reason: "stop" }] } }]);
  try {
    assert.equal((await post(s.egress.url, body)).status, 200);
    assert.equal(s.egress.ledger.held, reserveFor(body));
  } finally { await s.close(); }
});

test("401 立刻停；400 不重试", async () => {
  const s = await setup([{ status: 400, body: { error: "bad" } }, { status: 401, body: { error: "auth" } }]);
  try {
    assert.equal((await post(s.egress.url, dittoBody())).status, 502);
    assert.equal(s.up.received.length, 1, "400 不重试");
    assert.equal(s.egress.state.stopped, false);
    assert.equal((await post(s.egress.url, dittoBody())).status, 503);
    assert.equal(s.egress.state.stopped, true);
    assert.equal(s.egress.ledger.used, 0, "有状态码的错误不收钱");
  } finally { await s.close(); }
});

test("连续失败到上限就停", async () => {
  const s = await setup([{ status: 400, body: {} }, { status: 400, body: {} }], { maxConsecutiveFailures: 2 });
  try {
    await post(s.egress.url, dittoBody());
    assert.equal(s.egress.state.stopped, false);
    await post(s.egress.url, dittoBody());
    assert.equal(s.egress.state.stopped, true);
  } finally { await s.close(); }
});

test("密钥不进账本、日志和 /stats；上游错误里的密钥打码", async () => {
  const s = await setup([{ status: 400, body: { error: `bad key ${KEY}` } }, ok()]);
  try {
    await post(s.egress.url, dittoBody());
    await post(s.egress.url, dittoBody());
    const stats = await (await fetch(`${s.egress.url}/stats`)).text();
    assert.ok(!stats.includes(KEY));
    for (const f of readdirSync(s.ledgerDir)) assert.ok(!readFileSync(join(s.ledgerDir, f), "utf8").includes(KEY), f);
    assert.match(readFileSync(join(s.ledgerDir, "requests.jsonl"), "utf8"), /sk-\*\*\*/);
  } finally { await s.close(); }
});

test("账本重放：上次没有结果的预扣按全额保留，额度跨重启累计", () => {
  const dir = mkdtempSync(join(ROOT, "replay-"));
  const file = join(dir, "ledger.jsonl");
  writeFileSync(file, [
    { t: "reserve", id: "a", micro: 300 }, { t: "settle", id: "a", micro: 120 },
    { t: "reserve", id: "b", micro: 500 },
    { t: "reserve", id: "c", micro: 700 }, { t: "release", id: "c", reason: "http 503" },
  ].map(e => JSON.stringify(e)).join("\n") + "\n");
  const ledger = new Ledger(file, 10_000);
  assert.equal(ledger.spent, 120);
  assert.equal(ledger.held, 500);
  assert.equal(ledger.reserved, 0);
  const again = new Ledger(file, 10_000);
  assert.equal(again.held, 500, "重放两次不重复计");
  assert.equal(again.reserve("d", 10_000 - 620), true);
  assert.equal(again.reserve("e", 1), false);
});

test("费用：没有缓存字段时全按没命中算", () => {
  assert.equal(costMicro({ prompt_tokens: 1000, completion_tokens: 10 }), 1000 * 2 + 10 * 8);
  assert.equal(costMicro({ prompt_tokens: 1000, completion_tokens: 0, prompt_cache_hit_tokens: 1000, prompt_cache_miss_tokens: 0 }), 40);
});

test("Request ID 不透明、等长：题号里的 _abs、格子名都不出现在发给模型的内容里", async () => {
  const s = await setup([ok(), ok()]);
  try {
    const body = { ...dittoBody(), messages: [{ role: "user", content: "[task:12:gpt4_x_abs][step:answer]Q" }] };
    await post(s.egress.url, body);
    await fetch(`${s.egress.url}/reset?seed=real-s4-u100-k2-c37-split-memory-r1`, { method: "POST" });
    await post(s.egress.url, body);
    const [a, b] = s.up.received.map(r => String(r.body.messages[0].content));
    for (const text of [a!, b!]) {
      assert.match(text, /^Request ID: [0-9a-f]{16}\n\nQ$/);
      assert.ok(!text.includes("_abs") && !text.includes("split") && !text.includes("cellA"));
    }
    assert.notEqual(a, b, "不同格子的编号不同（防跨格缓存）");
    assert.equal(a!.length, b!.length, "等长");
  } finally { await s.close(); }
});

test("官方 rag-qa 的请求（没有内部标记）：认出题和步骤，只在 system 开头加 Request ID；同一格同一题同一步第二次记成 #2", async () => {
  const s = await setup([ok(), ok(), ok()]);
  try {
    const question = "Current Date: 2023/05/30 (Tue) 22:09\nQuestion: What health issue did I initially think was just a cold?";
    const system = "Return only JSON. Answer in the language of the user's question. Understand the question and construct retrieval queries. Return {}";
    const user = JSON.stringify([{ id: "question", content: question, metadata: { protected: true } }, { id: "working-set", content: { sources: [], question } }]);
    const body = { model: "deepseek-flash", stream: false, max_tokens: 8192, temperature: 0, messages: [{ role: "system", content: system }, { role: "user", content: user }] };
    assert.equal((await post(s.egress.url, body)).status, 200);
    assert.equal((await post(s.egress.url, body)).status, 200);
    const [a, b] = s.up.received.map(r => r.body.messages);
    assert.match(a[0].content, /^Request ID: [0-9a-f]{16}\n\n/);
    assert.equal(a[0].content.replace(/^Request ID: [0-9a-f]{16}\n\n/, ""), system, "官方提示词原样");
    assert.equal(a[1].content, user, "user 消息原样");
    assert.notEqual(a[0].content, b[0].content, "第二次换编号");
    const rows = ((await (await fetch(`${s.egress.url}/stats`)).json()) as any).rows;
    const task = `rag:${sha256(question).slice(0, 16)}`;
    assert.deepEqual(rows.map((r: any) => [r.task, r.kind]), [[task, "plan"], [task, "plan#2"]]);
    // 认不出的不发：不是官方提示词、user 不是条目 JSON、多一条消息
    for (const messages of [[{ role: "system", content: "Something else" }, { role: "user", content: user }],
      [{ role: "system", content: system }, { role: "user", content: "plain text" }], [...body.messages, { role: "user", content: user }]]) {
      assert.equal((await post(s.egress.url, { ...body, messages })).status, 400);
    }
    assert.equal(s.up.received.length, 2);
    // 换格以后重新计数
    await fetch(`${s.egress.url}/reset?seed=cellB`, { method: "POST" });
    await post(s.egress.url, body);
    assert.equal(((await (await fetch(`${s.egress.url}/stats`)).json()) as any).rows[0].kind, "plan");
  } finally { await s.close(); }
});

test("同一格重跑（再次 /reset）换编号：不会命中失败那次留下的缓存", async () => {
  const s = await setup([ok(), ok()]);
  try {
    await post(s.egress.url, dittoBody());
    await fetch(`${s.egress.url}/reset?seed=cellA`, { method: "POST" });
    await post(s.egress.url, dittoBody());
    const [a, b] = s.up.received.map(r => String(r.body.messages[0].content));
    assert.notEqual(a, b);
  } finally { await s.close(); }
});

test("实际费用超过预扣：照实记账，立刻停", async () => {
  const s = await setup([ok({ prompt_tokens: 1_000_000, completion_tokens: 0, prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 1_000_000 }), ok()]);
  try {
    assert.equal((await post(s.egress.url, dittoBody())).status, 200, "这一条已经花了，回答照常给");
    assert.equal(s.egress.ledger.spent, 2_000_000);
    assert.equal(s.egress.state.stopped, true);
    assert.equal((await post(s.egress.url, dittoBody())).status, 503);
    assert.equal(s.up.received.length, 1);
  } finally { await s.close(); }
});

test("超时（响应头迟迟不来）：那一次的预扣全额保留，截止前还够就重试", async () => {
  const body = dittoBody();
  const slow = ok() as { status: number; body: unknown };
  const s = await setup([{ ...slow, delayMs: 600 }, ok()], { attemptTimeoutMs: 150 });
  try {
    const r = await post(s.egress.url, body);
    assert.equal(r.status, 200);
    assert.equal(s.up.received.length, 2, "超时那次放弃，再发一次");
    assert.equal(s.egress.ledger.held, reserveFor(body), "放弃的那次按费用不明保留");
    assert.equal(s.egress.ledger.spent, OK_COST);
  } finally { await s.close(); }
});

test("DeepSeek 排队（响应头立刻来、之后只有空行）：到点放弃、再发一次，回答前面的空行不影响解析", async () => {
  const body = dittoBody();
  const queued = { ...(ok() as { status: number; body: unknown }), trickleMs: 30, delayMs: 3_000 };
  const s = await setup([queued, { ...(ok() as { status: number; body: unknown }), trickleMs: 30, delayMs: 100 }], { attemptTimeoutMs: 300 });
  try {
    const t0 = performance.now();
    const r = await post(s.egress.url, body);
    assert.equal(r.status, 200);
    assert.ok(performance.now() - t0 < 1_500, `应在一次超时加一次正常回答以内答复，实际 ${performance.now() - t0} ms`);
    assert.equal(s.up.received.length, 2);
    assert.equal(s.egress.ledger.held, reserveFor(body));
    assert.equal(s.egress.ledger.spent, OK_COST);
  } finally { await s.close(); }
});

test("一直排队：出口在截止前答复（502），每次放弃的都保留预扣，不会等到编排器那边超时", async () => {
  const body = dittoBody();
  const queued = () => ({ ...(ok() as { status: number; body: unknown }), trickleMs: 30, delayMs: 3_000 });
  const s = await setup([queued(), queued(), queued()], { attemptTimeoutMs: 300, deadlineMs: 1_200 });
  try {
    const t0 = performance.now();
    assert.equal((await post(s.egress.url, body)).status, 502);
    const ms = performance.now() - t0;
    assert.ok(ms < 1_200 + 300, `截止 1200 ms，实际 ${ms} ms`);
    const attempts = s.up.received.length;
    assert.ok(attempts >= 2, "截止前还够就重试");
    assert.equal(s.egress.ledger.held, attempts * reserveFor(body));
  } finally { await s.close(); }
});

test("推理资源不足（insufficient_system_resource）：照算钱，再重试", async () => {
  const usage = { prompt_tokens: 40, completion_tokens: 5, prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 40 };
  const busy: Behavior = { status: 200, body: { choices: [{ message: { role: "assistant", content: "par" }, finish_reason: "insufficient_system_resource" }], usage } };
  const s = await setup([busy, ok()]);
  try {
    const r = await post(s.egress.url, dittoBody());
    assert.equal(r.status, 200);
    assert.equal(r.json.choices[0].finish_reason, "stop");
    assert.equal(s.up.received.length, 2);
    assert.equal(s.egress.ledger.spent, 40 * 2 + 5 * 8 + OK_COST, "两次都结清");
    const row = (await (await fetch(`${s.egress.url}/stats`)).json() as any).rows[0];
    assert.deepEqual(row.tries.map((t: any) => t.outcome), ["settled", "settled"], "每次尝试都记下");
  } finally { await s.close(); }
});

test("usage 里的数不对：不让 NaN 进账本，按费用不明保留预扣", async () => {
  assert.equal(costMicro({ prompt_tokens: 10 }), undefined, "缺输出数");
  assert.equal(costMicro({ prompt_tokens: "10", completion_tokens: 1 }), undefined, "不是数");
  assert.equal(costMicro({ prompt_tokens: 10, completion_tokens: 1.5 }), undefined, "不是整数");
  assert.equal(costMicro({ prompt_tokens: 10, completion_tokens: 1, prompt_cache_hit_tokens: 3, prompt_cache_miss_tokens: 3 }), undefined, "命中加没命中对不上");
  const body = dittoBody();
  const s = await setup([{ status: 200, body: { choices: [{ message: { role: "assistant", content: "x" }, finish_reason: "stop" }], usage: { prompt_tokens: "abc" } } }]);
  try {
    assert.equal((await post(s.egress.url, body)).status, 200);
    assert.equal(s.egress.ledger.held, reserveFor(body));
    assert.ok(Number.isFinite(s.egress.ledger.used));
  } finally { await s.close(); }
});

test("总截止：截止前不够再试一次就不试", async () => {
  // 单次超时 2 秒，所以再试一次至少要留 2 秒；截止 1.5 秒：第一次（马上回 503）能发，第二次不够。
  const s = await setup([{ status: 503, body: {} }, ok()], { deadlineMs: 1_500 });
  try {
    assert.equal((await post(s.egress.url, dittoBody())).status, 502);
    assert.equal(s.up.received.length, 1);
  } finally { await s.close(); }
});

test("账本最后一行写到一半：跳过它照常启动；中间的坏行仍然报错", () => {
  const dir = mkdtempSync(join(ROOT, "partial-"));
  const file = join(dir, "ledger.jsonl");
  writeFileSync(file, JSON.stringify({ t: "reserve", id: "a", micro: 300 }) + "\n" + JSON.stringify({ t: "settle", id: "a", micro: 100 }) + '\n{"t":"reser');
  const first = new Ledger(file, 1000);
  assert.equal(first.spent, 100);
  first.reserve("b", 50); // 写在残片后面
  const second = new Ledger(file, 1000);
  assert.equal(second.spent, 100, "第二次重启仍然认得残片");
  assert.equal(second.held, 50, "残片后面那笔没有丢，按在途保留");
  assert.equal(new Ledger(file, 1000).held, 50, "第三次也一样");
  const bad = join(dir, "bad.jsonl");
  writeFileSync(bad, "{oops\n" + JSON.stringify({ t: "settle", id: "a", micro: 100 }) + "\n");
  assert.throws(() => new Ledger(bad, 1000));
});

test("读密钥文件：.env、JSON、纯文本都行；只认 sk-tst；地址只认 deepseek.com", () => {
  const dir = mkdtempSync(join(ROOT, "keys-"));
  const write = (name: string, text: string) => { const f = join(dir, name); writeFileSync(f, text); return f; };
  let k = readDeepSeekKey(write("a.env", "BFCL_BASE_URL=https://api.deepseek.com/\nBFCL_API_KEY=sk-tstabcdef123456\nOTHER=sk-zzzz11112222\n"));
  assert.deepEqual(k, { apiKey: "sk-tstabcdef123456", variable: "BFCL_API_KEY", baseUrl: "https://api.deepseek.com" });
  k = readDeepSeekKey(write("b.env", "BASE_URL=https://evil.example.com/v1\nKEY=sk-tstabcdef123456\n"));
  assert.equal(k.baseUrl, "https://api.deepseek.com", "别的主机不用");
  k = readDeepSeekKey(write("c.json", JSON.stringify({ deepseek: { apiKey: "sk-tstabcdef123456" } })));
  assert.equal(k.apiKey, "sk-tstabcdef123456");
  k = readDeepSeekKey(write("d.txt", "my key: sk-tstabcdef123456\n"));
  assert.equal(k.apiKey, "sk-tstabcdef123456");
  assert.throws(() => readDeepSeekKey(write("e.env", "KEY=sk-other123456789\n")), /没有 DEEPSEEK_KEY_PREFIX 开头的密钥/);
});

// ---------------------------------------------------------------- 全量实验加的记录（docs\31）

test("密钥前缀只从环境变量 DEEPSEEK_KEY_PREFIX 读：没设、格式不对都报错", () => {
  assert.throws(() => keyPrefix({}), /DEEPSEEK_KEY_PREFIX/);
  assert.throws(() => keyPrefix({ DEEPSEEK_KEY_PREFIX: "abc" }), /DEEPSEEK_KEY_PREFIX/);
  assert.throws(() => keyPrefix({ DEEPSEEK_KEY_PREFIX: "sk-" }), /DEEPSEEK_KEY_PREFIX/);
  assert.equal(keyPrefix({ DEEPSEEK_KEY_PREFIX: " sk-abc " }), "sk-abc");
});

test("完整记录：请求体按摘要去重（重启以后接着去重）、每次尝试的完整回复、写到一半的行先补换行", async () => {
  const s = await setup([ok(), ok(), { status: 500, body: { error: "busy" } }, ok()]);
  let ledgerDir = s.ledgerDir;
  try {
    const body = dittoBody();
    assert.equal((await post(s.egress.url, body)).status, 200);
    assert.equal((await post(s.egress.url, body)).status, 200, "同一个请求体再来一次");
    assert.equal((await post(s.egress.url, dittoBody("keyword"))).status, 200, "500 以后重试成功");
  } finally { await s.egress.close(); }
  const lines = (f: string) => readFileSync(join(ledgerDir, f), "utf8").split("\n").filter(Boolean);
  let bodies = lines("bodies.jsonl").map(l => JSON.parse(l));
  assert.equal(bodies.length, 2, "两个不同的请求体各记一次");
  assert.equal(bodies[0].body, JSON.stringify(dittoBody()), "原样记下 Ditto 发来的请求体");
  assert.equal(bodies[0].sha256, sha256(JSON.stringify(dittoBody())));
  const responses = lines("responses.jsonl").map(l => JSON.parse(l));
  assert.deepEqual(responses.map(r => r.status), [200, 200, 500, 200], "每次尝试一条，含失败的那次");
  const requests = lines("requests.jsonl").map(l => JSON.parse(l));
  assert.deepEqual(responses.map(r => r.id), [`${requests[0].id}#1`, `${requests[1].id}#1`, `${requests[2].id}#1`, `${requests[2].id}#2`]);
  assert.ok(!readFileSync(join(ledgerDir, "responses.jsonl"), "utf8").includes(KEY));
  // 进程被强杀：最后一行写到一半。重启以后先补换行，已记过的请求体不再记
  appendFileSync(join(ledgerDir, "bodies.jsonl"), '{"sha256":"oops');
  const up2 = await upstream([ok()]);
  const again = await startEgress({ port: 0, host: "127.0.0.1", apiKey: KEY, upstream: up2.url, model: "deepseek-flash", ledgerDir, limitMicroCny: 10_000_000 });
  try {
    await fetch(`${again.url}/reset?seed=cellB`, { method: "POST" });
    assert.equal((await post(again.url, dittoBody())).status, 200);
  } finally { await again.close(); await up2.close(); await s.up.close(); }
  const text = readFileSync(join(ledgerDir, "bodies.jsonl"), "utf8");
  assert.ok(text.endsWith("\n") && text.includes('{"sha256":"oops\n'), "残片后面补了换行，新记录不接在残片上");
  assert.equal(lines("bodies.index").length, 2, "重启以后同一个请求体没有再记");
  assert.equal(endsWithNewline(join(ledgerDir, "bodies.jsonl")), true);
});

test("打码不改正常词（task-based、risk-free）；真实密钥和像密钥的串照样换掉；正常回复原样记下", async () => {
  assert.equal(redact("a task-based plan, risk-free rate, whisk-together"), "a task-based plan, risk-free rate, whisk-together");
  assert.equal(redact(`key ${KEY} and sk-tstabcdefgh123`, [KEY]), "key sk-*** and sk-***");
  const reply = { status: 200, body: { id: "x", model: "deepseek-flash", choices: [{ index: 0, message: { role: "assistant", content: "a task-based, risk-free answer" }, finish_reason: "stop" }],
    usage: { prompt_tokens: 30, completion_tokens: 5, prompt_cache_hit_tokens: 10, prompt_cache_miss_tokens: 20 } } };
  const s = await setup([reply]);
  try { assert.equal((await post(s.egress.url, dittoBody())).status, 200); } finally { await s.close(); }
  const responses = readFileSync(join(s.ledgerDir, "responses.jsonl"), "utf8");
  assert.ok(responses.includes("a task-based, risk-free answer"), "正常回复原样记");
  const resets = readFileSync(join(s.ledgerDir, "resets.jsonl"), "utf8").split("\n").filter(Boolean).map(l => JSON.parse(l));
  const row = JSON.parse(readFileSync(join(s.ledgerDir, "requests.jsonl"), "utf8").split("\n").filter(Boolean)[0]!);
  assert.equal(resets.at(-1).cell, "cellA");
  assert.equal(row.rid, sha256(`${resets.at(-1).nonce}|cellA|${row.task}|${row.kind}`).slice(0, 16), "由 resets.jsonl 的随机数能还原防缓存编号");
  assert.ok(row.id.includes(`/${resets.at(-1).nonce.slice(0, 8)}.`), "请求编号带这一格的随机数，重启后不重复");
});
