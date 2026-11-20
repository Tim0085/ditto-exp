// 假模型里 rag-qa 那 4 个推理步骤的回复（docs\24 第 5 节）。按官方 rag-qa 的输出格式回按固定规则生成的内容，
// 过得了官方的校验（evidence.ts 的 plan、selection、answer、verify）；同一题同一步，不管哪种布局，回的完全一样。
// 测试在 tests\rag.test.ts（用官方校验函数检查这里的回复）。
//
// 官方 inferGraph（rag-qa/index.ts 77–90 行）发的是：system = 这一步的提示词；user = JSON.stringify(CONTEXT 里的条目)，
// 条目里有 id 为 "question" 的问题、id 为 "working-set" 的这一步的数据（index.ts 149–160 行）。
// ragKind、ragTask 出口（egress.ts）也用：真实模型时出口靠它们认出官方 rag-qa 的请求是哪一题、哪一步（官方提示词不能加标记）。
import { tokens } from "../rag-a/examples/_shared/tools/evidence.ts";
import { keywordQueries, sha256 } from "./common.ts";

export type RagKind = "plan" | "select" | "answer" | "verify";
/** 按官方各步提示词里独有的一句认步骤（index.ts 187–198 行）。 */
const MARKS: readonly (readonly [RagKind, string])[] = [
  ["plan", "Understand the question and construct retrieval queries"],
  ["select", "Select relevant chunks from the retrieved pool"],
  ["answer", "Generate a useful grounded answer"],
  ["verify", "Independently verify the draft"],
];
export const ragKind = (system: string): RagKind | undefined => MARKS.find(([, mark]) => system.includes(mark))?.[0];

/** 题号：问题文字的哈希（请求里没有题号，问题文字每题不同；问题里带提问日期）。 */
const taskOf = (question: string) => `rag:${sha256(question).slice(0, 16)}`;

/** 出口用：从 user 消息（CONTEXT 条目的 JSON）里认出是哪一题。不是条目数组、或者没有非空的问题，返回 undefined。 */
export function ragTask(user: string): string | undefined {
  let items: unknown;
  try { items = JSON.parse(user); } catch { return undefined; }
  if (!Array.isArray(items)) return undefined;
  const question = items.find(i => i && typeof i === "object" && (i as { id?: unknown }).id === "question")?.content;
  return typeof question === "string" && question.trim() ? taskOf(question) : undefined;
}

/** 挑证据时最多留多少字：官方编排器在证据超过 1 万字时报错（index.ts 354 行），留点余地。 */
export const EVIDENCE_CHARS = 9000;

interface Chunk { id: string; text: string }

/**
 * 返回这一步的回复文字和用来抽延迟、记录请求的题号（问题文字的哈希：请求里没有题号，问题文字每题不同）。
 * 不是 rag-qa 的提示词就返回 undefined。
 */
export function ragReply(system: string, user: string): { kind: RagKind; task: string; content: string } | undefined {
  const kind = ragKind(system);
  if (!kind) return undefined;
  const items = JSON.parse(user) as { id: string; content: unknown }[];
  const question = String(items.find(i => i.id === "question")?.content ?? "");
  const data = (items.find(i => i.id === "working-set")?.content ?? {}) as Record<string, any>;
  const task = taskOf(question);
  let reply: unknown;
  if (kind === "plan") {
    // 检索词：和 S4 假模型相同的规则（题目里前 3 个实词），只取官方分词认得的。
    const asked = /Question: ([\s\S]*)$/.exec(question)?.[1] ?? question;
    const queries = keywordQueries(asked).filter(q => tokens(q).length).slice(0, 3);
    reply = { intent: `Answer the user's question: ${asked.slice(0, 200)}`, queries: queries.length ? queries : ["history"],
      facets: ["the requested fact"], clarification: null };
  } else if (kind === "select") {
    // 按检索结果的顺序挑，最多 8 块、合计不超过 EVIDENCE_CHARS 字。
    const picked: string[] = [];
    let chars = 0;
    for (const c of (data.pool ?? []) as Chunk[]) {
      if (picked.length === 8) break;
      if (chars + c.text.length > EVIDENCE_CHARS) continue;
      picked.push(c.id); chars += c.text.length;
    }
    reply = { selectedIds: picked, missing: [], conflicts: [] };
  } else if (kind === "answer") {
    // 每块证据一条结论（最多 2 条），引用这块里第一行够长的原文（官方要求逐字出现在块里、去掉空白后至少 4 个字）。
    const claims = ((data.evidence ?? []) as Chunk[]).slice(0, 2).map(c => {
      const line = c.text.split("\n").find(l => l.trim().length >= 4) ?? c.text;
      return { text: "The chat history records the following.", citations: [{ chunkId: c.id, quote: Array.from(line).slice(0, 200).join("") }] };
    });
    const status = String(data.expectedStatus ?? "answered");
    reply = { status, claims, limitations: status === "answered" ? [] : ["Some requested facts were not found."] };
  } else {
    const n = Array.isArray(data.draft?.claims) ? data.draft.claims.length : 0;
    reply = { complete: true, claims: Array.from({ length: n }, (_, index) => ({ index, supported: true })) };
  }
  return { kind, task, content: JSON.stringify(reply) };
}
