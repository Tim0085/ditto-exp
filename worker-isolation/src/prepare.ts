// 在本机跑一次：把两个 bench 整理成 03 用的任务文件。docs\16 第 2 节。
//
//   node --max-old-space-size=6144 src/prepare.ts <实验根目录，例如 sec1-2.0 的完整路径>
//
// LongMemEval_S：从原始 json 取出每题的 session（保留每条消息的角色和正文），写成 memctx-lme.jsonl。
//   同一题里重复出现的 session（13 题，日期不同），第二次的编号加 -r2，和 docs\12 第 4 节相同。
// LongBench v2：读 02 生成的 tasks.jsonl（短段 180、中段 214，已剔除 223 万字符那一题），
//   用官方 chunks()（relaxed 副本，只放开上限）切块，写成 memctx-lb.jsonl。切块不算进测量。
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chunks, type Source } from "../relaxed/examples/_shared/tools/rag/domain.ts";
import { jsonLines, sha256 } from "./common.ts";

const sec = process.argv[2];
if (!sec) throw new Error("用法：node src/prepare.ts <sec1-2.0 目录>");

// ---- LongMemEval_S ----
const lmeRaw = join(sec, "bench_data/longmemeval-s/longmemeval_s_cleaned.json");
const lme = JSON.parse(readFileSync(lmeRaw, "utf8")) as any[];
const lmeLines: string[] = [];
let duplicateSessions = 0, messages = 0;
for (const q of lme) {
  const seen = new Set<string>();
  const sessions = q.haystack_sessions.map((turns: any[], i: number) => {
    let id = String(q.haystack_session_ids[i]);
    if (seen.has(id)) { id = `${id}-r2`; duplicateSessions++; }
    if (seen.has(id)) throw new Error(`第三次重复的 session ${id}`);
    seen.add(id);
    messages += turns.length;
    return { id, date: String(q.haystack_dates[i]), messages: turns.map(t => ({ role: String(t.role), content: String(t.content) })) };
  });
  lmeLines.push(JSON.stringify({ id: String(q.question_id), qtype: String(q.question_type), question: String(q.question),
    question_date: String(q.question_date), sessions }));
}
const lmeOut = join(sec, "bench_data/longmemeval-s/derived/memctx-lme.jsonl");
writeFileSync(lmeOut, lmeLines.join("\n") + "\n");

// ---- LongBench v2 ----
const lbIn = join(sec, "bench_data/longbench-v2/derived/tasks.jsonl");
const lbOut = join(sec, "bench_data/longbench-v2/derived/memctx-lb.jsonl");
const lbLines: string[] = [];
let lbChunks = 0;
// 02 的 tasks.jsonl 由 Python 写出（非 ASCII 字符已转义），用 readline 读也不会断行；为统一仍用 jsonLines。
for await (const line of jsonLines(lbIn)) {
  const t = JSON.parse(line);
  if (!["short", "medium"].includes(t.segment)) continue;
  const source: Source = { id: "doc", tenant: "bench", readers: [], title: String(t.id), kind: "document", ref: "doc.txt" };
  const parts = chunks(source, String(t.context)).map(c => ({ id: c.id, text: c.text }));
  lbChunks += parts.length;
  lbLines.push(JSON.stringify({ id: String(t.id), segment: t.segment, domain: t.domain, question: String(t.question), chars: t.chars, chunks: parts }));
}
writeFileSync(lbOut, lbLines.join("\n") + "\n");

const manifest = {
  generatedBy: "03_memctx_probe/src/prepare.ts",
  longmemeval: { input: lmeRaw, inputSha256: sha256(readFileSync(lmeRaw, "utf8")), output: lmeOut, tasks: lmeLines.length, messages, duplicateSessionsRenamed: duplicateSessions,
    outputSha256: sha256(readFileSync(lmeOut, "utf8")) },
  longbench: { input: lbIn, output: lbOut, tasks: lbLines.length, chunks: lbChunks, outputSha256: sha256(readFileSync(lbOut, "utf8")) },
};
writeFileSync(join(sec, "bench_data/memctx-manifest.json"), JSON.stringify(manifest, null, 1));
console.log(JSON.stringify(manifest, null, 1));
