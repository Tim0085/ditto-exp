// rag 场景的题目和内部知识（docs\24 第 4 节）。在宿主机上跑，只读题目文件，写到 bench_data 的 derived 目录：
//
//   node src/prepare-rag.ts --cut a --tasks ../bench_data/longmemeval-s/derived/memctx-lme-first200.jsonl --out ../bench_data/longmemeval-s/derived
//   node src/prepare-rag.ts --cut a --tasks ../bench_data/longmemeval-s/derived/memctx-lme-all500.jsonl --out ../bench_data/longmemeval-s/derived --prefix rag-a-all500
//   种子 42（docs\32）：--tasks 和 --out 换成 derived/s42/ 下的（例如 --tasks ../bench_data/longmemeval-s/derived/s42/memctx-lme-first200.jsonl --out ../bench_data/longmemeval-s/derived/s42）
//
// 要写的文件已经存在时，内容相同就不重写，不同就报错停下（common.ts 的 writeNew）：已有的数据不会被换掉。
// --cut a：切法 (a)，一段对话一个来源（正式用这个），输出文件名前缀 rag-a-first200；
// --cut b：切法 (b)，相邻对话合并成 ≤20 个来源（11-19 的离线测量），前缀 rag-lme-first200。两种切法见 rag-sources.ts。
// --prefix：换输出文件名前缀。全量 500 题（docs\31）用 rag-a-all500，输入是 S4 全量用的 memctx-lme-all500.jsonl（前 200 题和 first200 相同）。
// 输入：S4 用的同一份题目（固定顺序，docs\16）。输出：
//   <前缀>.jsonl            每题一行：题号、题型、token、问题（带提问日期）、来源清单（编号、标题、ref）。编排器和 INTERACTION 用
//   <前缀>.knowledge.jsonl  每条内部知识一行（key、content），build-rag-store.ts 写进 MEMORY
//   <前缀>.manifest.json    统计：来源数、字数、块数、折行数，以及每条上限是否都守住
// 每道题都用官方的 request()、sources()、chunks() 原样校验（切法 (a) 用 rag-a\ 的副本），任何一条不过就停下。
import { join } from "node:path";
import { buildRagTask, buildRagTaskA, MAX_CHUNKS, MAX_CHUNKS_A, MAX_SOURCE_CHARS, MAX_SOURCES, MAX_SOURCES_A, type LmeInput } from "./rag-sources.ts";
import { arg, jsonLines, sha256, writeNew } from "./common.ts";

const cut = arg("cut");
if (cut !== "a" && cut !== "b") throw new Error("--cut 是 a 或 b");
const CUT = cut === "a"
  ? { build: buildRagTaskA, prefix: "rag-a-first200", rule: "切法 (a)：一段对话一个来源；只放开每题来源 20→100、每个来源的块 200→400（rag-a\\CHANGES.md）",
      limits: { sources_per_request: MAX_SOURCES_A, chars_per_source: MAX_SOURCE_CHARS, chunks_per_source: MAX_CHUNKS_A, chars_per_line: 1600, question_chars: 1200 } }
  : { build: buildRagTask, prefix: "rag-lme-first200", rule: "docs\\24 第 4 节切法 (b)：官方上限一条不放开",
      limits: { sources_per_request: MAX_SOURCES, chars_per_source: MAX_SOURCE_CHARS, chunks_per_source: MAX_CHUNKS, chars_per_line: 1600, question_chars: 1200 } };
const outDir = arg("out");
const prefix = arg("prefix", CUT.prefix);
if (!/^[a-z0-9-]+$/.test(prefix)) throw new Error(`--prefix 只能用小写字母、数字和 -：${prefix}`);
const tasks: string[] = [], knowledge: string[] = [];
const stats: any[] = [];
const tokens = new Set<string>(), questions = new Map<string, string>();
for await (const line of jsonLines(arg("tasks"))) {
  const t = JSON.parse(line) as LmeInput;
  const built = CUT.build(t);
  if (tokens.has(built.task.token)) throw new Error(`token 重复：${t.id}`);
  tokens.add(built.task.token);
  // 假模型和出口按问题文字认题（rag-fake.ts）：问题文字重复的题会共用延迟抽样，记下来
  const q = sha256(built.task.question);
  if (questions.has(q)) console.log(`问题文字相同：${questions.get(q)} 和 ${t.id}`);
  questions.set(q, t.id);
  tasks.push(JSON.stringify(built.task));
  for (const k of built.knowledge) knowledge.push(JSON.stringify(k));
  stats.push({ id: t.id, ...built.stats });
}
// 文件名前缀和题数要对得上：给 500 题忘了传 --prefix 会盖掉 first200 的三个文件，反过来也不行
for (const [mark, n] of [["first200", 200], ["all500", 500]] as const) {
  if (prefix.includes(mark) && stats.length !== n) throw new Error(`前缀 ${prefix} 应当是 ${n} 题，输入有 ${stats.length} 题：检查 --tasks 和 --prefix`);
}
const all = <K extends string>(key: K) => stats.flatMap(s => s[key] as number[]);
const q = (xs: number[], p: number) => [...xs].sort((a, b) => a - b)[Math.floor((xs.length - 1) * p)]!;
const manifest = {
  input: arg("tasks"), tasks: stats.length, rule: CUT.rule,
  // 切法 (b) 的清单沿用原来的字段名（11-19 生成的那份逐字可复现）；切法 (a) 的上限有两个是放开后的数
  [cut === "a" ? "limits" : "official_limits"]: CUT.limits,
  sessions_per_task: { median: q(stats.map(s => s.sessions), 0.5), max: Math.max(...stats.map(s => s.sessions)) },
  sources_per_task: { median: q(stats.map(s => s.sources), 0.5), min: Math.min(...stats.map(s => s.sources)), max: Math.max(...stats.map(s => s.sources)) },
  chars_per_source: { median: q(all("sourceChars"), 0.5), p95: q(all("sourceChars"), 0.95), max: Math.max(...all("sourceChars")) },
  chunks_per_source: { median: q(all("chunks"), 0.5), max: Math.max(...all("chunks")) },
  chunks_per_task: { median: q(stats.map(s => s.chunks.reduce((a: number, b: number) => a + b, 0)), 0.5) },
  wrapped_lines: stats.reduce((n, s) => n + s.wrappedLines, 0), tasks_with_wrapped_lines: stats.filter(s => s.wrappedLines).length,
  split_sessions: stats.reduce((n, s) => n + s.splitSessions, 0),
  ...(cut === "a" ? { sources_over_official_200_chunks: all("chunks").filter(n => n > MAX_CHUNKS).length,
    tasks_over_official_20_sources: stats.filter(s => s.sources > MAX_SOURCES).length } : {}),
  question_chars_max: Math.max(...stats.map(s => s.questionChars)), duplicate_questions: stats.length - questions.size,
  knowledge_records: knowledge.length,
  sha256: { tasks: sha256(tasks.join("\n") + "\n"), knowledge: sha256(knowledge.join("\n") + "\n") },
};
writeNew(join(outDir, `${prefix}.jsonl`), tasks.join("\n") + "\n");
writeNew(join(outDir, `${prefix}.knowledge.jsonl`), knowledge.join("\n") + "\n");
writeNew(join(outDir, `${prefix}.manifest.json`), JSON.stringify(manifest, null, 1) + "\n");
console.log(JSON.stringify(manifest));
