// 本机假模型：按 OpenAI chat completions 接口收发，不调真实模型。docs\16 第 3 节。
//
// - 关键词那一步：从请求里的题目按 common.ts 的固定规则生成检索词，返回 {"queries":[...]}。
// - 回答那一步：返回固定长度的 JSON 文字。
// - 延迟：对数正态，中位数由参数给；抽样由（题号、步骤、轮次种子）决定，两种布局同一题同一步等一样久。
// - 记录每个请求体的 SHA-256，按题号和步骤汇总，用来比较两种布局发给模型的内容是否逐字相同。
//
//   node src/fake-model.ts --port 8080 [--keyword-ms 1000 --answer-ms 2000 --sigma 0.5]
//   GET /stats 取记录；POST /reset?seed=xxx&keyword-ms=..&answer-ms=..&sigma=.. 清空记录并换设定。
import { createServer } from "node:http";
import { ANSWER_INSTRUCTION, KEYWORD_INSTRUCTION, REAL_KEYWORD_INSTRUCTION, arg, keywordQueries, normalQuantile, sha256, unitFrom } from "./common.ts";
import { ragReply } from "./rag-fake.ts";

const settings = {
  keywordMs: Number(arg("keyword-ms", "0")),
  answerMs: Number(arg("answer-ms", "0")),
  sigma: Number(arg("sigma", "0.5")),
  seed: "r0",
};
const ANSWER = JSON.stringify({ answer: "Offline fixture answer. ".repeat(8).trim() });

interface Row { task: string; kind: string; sha256: string; bytes: number; delay_ms: number; at: number }
let rows: Row[] = [];
let active = 0, peakActive = 0, errors = 0;
/** 同一题同一步第几次（和出口一样，第二次起记成 answer#2，比较请求时键不重复；延迟仍按步骤抽）。复位时清空。 */
let seen = new Map<string, number>();

function delayMs(task: string, kind: string, median: number) {
  if (median <= 0) return 0;
  const u = unitFrom(`${settings.seed}|${task}|${kind}`);
  return median * Math.exp(settings.sigma * normalQuantile(u));
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? "/", "http://x");
    if (req.method === "GET" && url.pathname === "/stats") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ settings, rows, peakActive, errors, realModelCalls: 0 }));
      return;
    }
    if (req.method === "POST" && url.pathname === "/reset") {
      for await (const _ of req) { /* 丢弃 */ }
      rows = []; peakActive = 0; errors = 0; seen = new Map();
      settings.seed = url.searchParams.get("seed") ?? settings.seed;
      for (const [key, field] of [["keyword-ms", "keywordMs"], ["answer-ms", "answerMs"], ["sigma", "sigma"]] as const) {
        const v = url.searchParams.get(key); if (v !== null) settings[field] = Number(v);
      }
      res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(settings)); return;
    }
    if (req.method !== "POST" || !url.pathname.endsWith("/chat/completions")) { res.writeHead(404); res.end(); return; }
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(Buffer.from(c));
    const raw = Buffer.concat(chunks).toString("utf8");
    const body = JSON.parse(raw);
    const system = String(body.messages?.[0]?.content ?? "");
    const user = String(body.messages?.at(-1)?.content ?? "");
    // 经出口（src\egress.ts）转来的真实模式请求：第一条消息开头是一行 “Request ID: <16 位哈希>”，题号已被出口去掉。
    // 按提示词内容认步骤。只在离线演练出口时用到（延迟为 0，用不到题号）。
    const viaEgress = /^Request ID: ([0-9a-f]{16})\n\n/.exec(system);
    const first = viaEgress ? system.slice(viaEgress[0].length) : system;
    // rag 场景（docs\24）：官方 rag-qa 的 4 个推理步骤，回复见 rag-fake.ts。理解问题、挑证据、核对用“理解问题”的延迟，回答用“回答”的延迟。
    // 经出口转来的 rag 请求（真实模型的离线演练）：出口只在 system 开头加了 Request ID，user 没动，照样认。
    const rag = ragReply(first, user);
    const kind: string = rag ? rag.kind : viaEgress
      ? first === REAL_KEYWORD_INSTRUCTION ? "keyword" : first.startsWith("I will give you several history chats") ? "answer" : "unknown"
      : system === KEYWORD_INSTRUCTION ? "keyword" : system === ANSWER_INSTRUCTION ? "answer" : "unknown";
    if (kind === "unknown") throw new Error("unknown prompt");
    // 题号写在请求里（[task:xxx]），两种布局相同；rag 用问题文字的哈希。
    const task = rag ? rag.task : viaEgress ? `rid:${viaEgress[1]}` : /\[task:([^\]]+)\]/.exec(user)?.[1] ?? "?";
    let content = rag ? rag.content : ANSWER;
    if (kind === "keyword") {
      const question = viaEgress ? /Question: ([\s\S]*)$/.exec(user)?.[1] ?? "" : /\[question:([\s\S]*?)\]\[\/question\]/.exec(user)?.[1] ?? "";
      content = JSON.stringify({ queries: keywordQueries(question) });
    }
    const wait = delayMs(task, kind, kind === "answer" ? settings.answerMs : settings.keywordMs);
    const nth = (seen.get(`${task}|${kind}`) ?? 0) + 1;
    seen.set(`${task}|${kind}`, nth);
    rows.push({ task, kind: nth === 1 ? kind : `${kind}#${nth}`, sha256: sha256(raw), bytes: Buffer.byteLength(raw), delay_ms: Math.round(wait), at: Date.now() });
    active++; peakActive = Math.max(peakActive, active);
    try { if (wait > 0) await new Promise(done => setTimeout(done, wait)); } finally { active--; }
    const out = {
      id: `chatcmpl-${rows.length}`, object: "chat.completion", created: Math.floor(Date.now() / 1000), model: body.model,
      choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }],
      usage: { prompt_tokens: Math.ceil(raw.length / 4), completion_tokens: Math.ceil(content.length / 4), total_tokens: Math.ceil((raw.length + content.length) / 4),
        ...(viaEgress ? { prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: Math.ceil(raw.length / 4) } : {}) },
    };
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(out));
  } catch (error) {
    errors++;
    if (!res.headersSent) res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: String(error) } }));
  }
});
server.keepAliveTimeout = 600_000;
server.listen(Number(arg("port", "8080")), "0.0.0.0", () => console.log(`fake-model listening ${arg("port", "8080")}`));
