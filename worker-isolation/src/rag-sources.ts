// rag 场景（docs\24）：把 LongMemEval 一道题的历史对话变成官方 rag-qa 的“内部知识”来源。纯函数，测试在 tests\rag.test.ts。
//
// 两种切法，正式用切法 (a)（见文件末尾的 buildRagTaskA）：一段对话一个来源，只放开官方的两个数字（rag-a\CHANGES.md）。
// 切法 (b)（11-19 的离线测量用过，留着复现）：官方上限一条都不放开——
// - 每个 session 按 LongMemEval 官方 history_format='nl' 写成文字（和 docs\13、02_load_probe\src\prepare.py 相同）；
// - 超过 1600 字的行，在 1600 字以内最后一个空格之后插入换行（只插入换行，不改文字；官方切块要求单行 ≤1600）；
// - 单个 session 就超过 200 块的（9524 个里有 2 个），在空行处分成前后几段（见 splitOversized）；
// - 按日期排序后，把相邻的 session 合并成不超过 20 个来源（官方每题 ≤20 个来源），session 之间空一行
//   （官方切块遇空行就断，块不会跨 session）；在每个来源 ≤10 万字、≤200 块的前提下，让最大的来源尽量小。
// 模型能看到来源编号、标题和块的 uri（含 ref），所以 ref 里只放题号的哈希（token），不放题号：
// 题号里的 “_abs” 是 LongMemEval 的“无法回答”标记，不能让模型看见（和 S4 出口的 Request ID 同一个道理）。
import { chunks, request, sources } from "../official/examples/_shared/tools/rag/domain.ts";
import { chunks as chunksA, request as requestA, sources as sourcesA } from "../rag-a/examples/_shared/tools/rag/domain.ts";
import { sha256 } from "./common.ts";

export const TENANT = "lme", PRINCIPAL = "user";
export const MAX_SOURCES = 20, MAX_SOURCE_CHARS = 100_000, MAX_CHUNKS = 200, MAX_LINE = 1600;
/** 切法 (a) 放开的两个数字（rag-a\CHANGES.md）：每题来源、每个来源的块。 */
export const MAX_SOURCES_A = 100, MAX_CHUNKS_A = 400;

export interface RagSource { id: string; title: string; ref: string }
/** rag-lme-first200.jsonl 的一行：编排器和 INTERACTION 进程都只用到这些（来源原文在 MEMORY 里）。 */
export interface RagTask { id: string; qtype: string; token: string; question: string; sources: RagSource[] }
/** 写进 MEMORY 的一条内部知识，形状照官方 adapters.ts 92–103 行的检查。 */
export interface Knowledge { key: string; content: { kind: "knowledge"; tenant: string; title: string; text: string } }

interface Message { role: string; content: string }
interface LmeSession { id: string; date: string; messages: Message[] }
export interface LmeInput { id: string; qtype: string; question: string; question_date: string; sessions: LmeSession[] }

/** LongMemEval 官方 run_generation.py 239–252 行的 nl 格式，去掉多 session 拼接时才用的 “### Session i:” 标题。 */
export const sessionText = (date: string, messages: readonly Message[]) =>
  `Session Date: ${date}\nSession Content:\n` + messages.map(m => `\n\n${m.role}: ${m.content.trim()}`).join("");

/** 把超过 max 的行折开：在 max 以内最后一个空格之后断（空格留在上一行末尾）；没有合适的空格就硬断。只插入 "\n"。 */
export function wrapLongLines(text: string, max = MAX_LINE): { text: string; wrapped: number } {
  let wrapped = 0;
  const lines = text.split("\n").flatMap(line => {
    if (line.length <= max) return [line];
    wrapped++;
    const out: string[] = [];
    let rest = line;
    while (rest.length > max) {
      let cut = rest.lastIndexOf(" ", max - 1) + 1;
      if (cut < max / 2) cut = max;
      if (/[\uD800-\uDBFF]/.test(rest[cut - 1]!)) cut--; // 不把一个字拆在两行
      out.push(rest.slice(0, cut));
      rest = rest.slice(cut);
    }
    if (rest) out.push(rest);
    return out;
  });
  return { text: lines.join("\n"), wrapped };
}

/** 官方切块（domain.ts 84–125 行）会切出几块。规则照抄，只数块数、不做上限检查；最终的来源仍用官方 chunks() 校验。 */
export function countChunks(raw: string): number {
  let n = 0, group: string[] = [];
  const flush = () => { if (group.some(s => s.trim())) n++; group = []; };
  for (const line of raw.split("\n")) {
    if (!line.trim()) { flush(); continue; }
    if (group.join("\n").length + line.length > 1800) flush();
    group.push(line);
  }
  flush();
  return n;
}

/**
 * 单个 session 就超过 200 块时（前 200 题里 9524 个 session 中有 2 个：只有 2 轮，但一条消息有几百段），
 * 在空行处（官方切块本来就在这里断）把它分成前后几段，每段 ≤200 块，再和别的 session 一样装进来源。文字不变。
 */
export function splitOversized(text: string): string[] {
  if (countChunks(text) <= MAX_CHUNKS) return [text];
  const paragraphs = text.split("\n\n"), parts: string[] = [];
  let current: string[] = [];
  for (const p of paragraphs) {
    if (current.length && countChunks([...current, p].join("\n\n")) > MAX_CHUNKS) { parts.push(current.join("\n\n")); current = []; }
    current.push(p);
  }
  if (current.length) parts.push(current.join("\n\n"));
  return parts;
}

export interface Piece { date: string; text: string; chunks: number }

/** 按顺序贪心装组：一组的字数（含 session 之间的空行）不超过 cap、块数不超过 200。 */
function greedy(pieces: readonly Piece[], cap: number): Piece[][] {
  const groups: Piece[][] = [];
  let current: Piece[] = [], chars = 0, count = 0;
  for (const p of pieces) {
    if (current.length && (chars + 2 + p.text.length > cap || count + p.chunks > MAX_CHUNKS)) {
      groups.push(current); current = []; chars = 0; count = 0;
    }
    chars += (current.length ? 2 : 0) + p.text.length; count += p.chunks; current.push(p);
  }
  if (current.length) groups.push(current);
  return groups;
}

/** 分成不超过 20 组、每组 ≤10 万字且 ≤200 块，并让最大一组尽量小（对上限二分，每次贪心装组）。 */
export function packSessions(pieces: readonly Piece[]): Piece[][] {
  if (!pieces.length) throw new Error("没有 session");
  for (const p of pieces) if (p.text.length > MAX_SOURCE_CHARS || p.chunks > MAX_CHUNKS) throw new Error("单个 session 超过官方来源上限");
  let lo = Math.max(...pieces.map(p => p.text.length)), hi = MAX_SOURCE_CHARS;
  if (greedy(pieces, hi).length > MAX_SOURCES) throw new Error("在官方上限内分不到 20 个来源以内");
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (greedy(pieces, mid).length <= MAX_SOURCES) hi = mid; else lo = mid + 1;
  }
  return greedy(pieces, lo);
}

/** 按日期排 session，同一时刻的保持原顺序。日期写法 “2023/05/20 (Sat) 02:21” 按字符串排就是按时间排。 */
const byDate = (sessions: readonly LmeSession[]) =>
  sessions.map((s, i) => ({ s, i })).sort((a, b) => (a.s.date < b.s.date ? -1 : a.s.date > b.s.date ? 1 : a.i - b.i)).map(({ s }) => s);

/** 一道题 → 请求要用的来源清单、写进 MEMORY 的内部知识，并用官方的 request()、sources()、chunks() 原样校验一遍。 */
export function buildRagTask(t: LmeInput) {
  const token = sha256(t.id).slice(0, 12);
  let wrapped = 0, split = 0;
  const pieces: Piece[] = byDate(t.sessions)
    .flatMap(s => {
      const w = wrapLongLines(sessionText(s.date, s.messages));
      wrapped += w.wrapped;
      const parts = splitOversized(w.text);
      split += parts.length > 1 ? 1 : 0;
      return parts.map(text => ({ date: s.date, text, chunks: countChunks(text) }));
    });
  const groups = packSessions(pieces);
  const catalog: RagSource[] = [], knowledge: Knowledge[] = [];
  groups.forEach((g, k) => {
    const id = `s${String(k + 1).padStart(2, "0")}`;
    const d0 = g[0]!.date.slice(0, 10), d1 = g.at(-1)!.date.slice(0, 10);
    const title = d0 === d1 ? `Chat history ${d0}` : `Chat history ${d0} to ${d1}`;
    const ref = `knowledge:${TENANT}:${token}-${id}`;
    catalog.push({ id, title, ref });
    knowledge.push({ key: ref, content: { kind: "knowledge", tenant: TENANT, title, text: g.map(p => p.text).join("\n\n") } });
  });
  const question = `Current Date: ${t.question_date}\nQuestion: ${t.question}`;
  // 官方校验（任何一条不过就抛错，说明切法没守住官方上限）
  const checked = sources(catalog.map(s => ({ ...s, tenant: TENANT, readers: [PRINCIPAL], kind: "internal" })));
  request({ id: `t000000000000-${token}`, tenant: TENANT, principal: PRINCIPAL, question, sourceIds: catalog.map(s => s.id) });
  const chunkCounts = knowledge.map((m, k) => chunks(checked[k]!, m.content.text).length);
  const task: RagTask = { id: t.id, qtype: t.qtype, token, question, sources: catalog };
  return { task, knowledge, stats: { sessions: t.sessions.length, sources: catalog.length, wrappedLines: wrapped, splitSessions: split,
    sourceChars: knowledge.map(m => m.content.text.length), chunks: chunkCounts, questionChars: question.length } };
}

/**
 * 切法 (a)：一段对话一个来源，不合并、不拆分。
 * - 按日期排序，来源编号 s01、s02……（原始 session 编号里有 “answer_”，会泄露哪段有答案，不能用）；
 * - 来源标题是这段对话的日期（“Session 2023/05/20 (Sat) 02:21”）：官方给检索到的每一块都带上来源标题，模型能看到这块是哪天说的；
 * - 文字和切法 (b) 相同：官方 nl 格式，超过 1600 字的行折行（只插入换行）。
 * 用 rag-a\ 的官方副本校验（只放开两个数字：每题来源 20→100、每个来源的块 200→400），其余上限照官方。
 */
export function buildRagTaskA(t: LmeInput) {
  const token = sha256(t.id).slice(0, 12);
  let wrapped = 0;
  const catalog: RagSource[] = [], knowledge: Knowledge[] = [];
  byDate(t.sessions).forEach((s, k) => {
    const w = wrapLongLines(sessionText(s.date, s.messages));
    wrapped += w.wrapped;
    const id = `s${String(k + 1).padStart(2, "0")}`, title = `Session ${s.date}`, ref = `knowledge:${TENANT}:${token}-${id}`;
    catalog.push({ id, title, ref });
    knowledge.push({ key: ref, content: { kind: "knowledge", tenant: TENANT, title, text: w.text } });
  });
  const question = `Current Date: ${t.question_date}\nQuestion: ${t.question}`;
  const checked = sourcesA(catalog.map(s => ({ ...s, tenant: TENANT, readers: [PRINCIPAL], kind: "internal" })));
  requestA({ id: `t000000000000-${token}`, tenant: TENANT, principal: PRINCIPAL, question, sourceIds: catalog.map(s => s.id) });
  const chunkCounts = knowledge.map((m, k) => chunksA(checked[k]!, m.content.text).length);
  const task: RagTask = { id: t.id, qtype: t.qtype, token, question, sources: catalog };
  return { task, knowledge, stats: { sessions: t.sessions.length, sources: catalog.length, wrappedLines: wrapped, splitSessions: 0,
    sourceChars: knowledge.map(m => m.content.text.length), chunks: chunkCounts, questionChars: question.length } };
}
