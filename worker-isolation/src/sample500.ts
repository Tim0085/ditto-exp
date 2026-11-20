// 全量 500 题（docs\31）：按 sample.ts 写下的固定顺序 memctx-lme-order.json，写出全部 500 题 memctx-lme-all500.jsonl。
// 在本机跑一次：
//
//   node --max-old-space-size=6144 src/sample500.ts <实验根目录，例如 sec1-2.0 的完整路径>
//
// 前 200 行必须和 memctx-lme-first200.jsonl 逐字节相同（11-19、11-20 各轮用的就是它）：对不上就报错、不写文件。
// 另写 memctx-lme-all500.manifest.json：题数、各题型题数、摘要。
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { jsonLines, sha256 } from "./common.ts";

const sec = process.argv[2];
if (!sec) throw new Error("用法：node src/sample500.ts <sec1-2.0 目录>");
const dir = join(sec, "bench_data/longmemeval-s/derived");

const byId = new Map<string, string>();
for await (const line of jsonLines(join(dir, "memctx-lme.jsonl"))) {
  const id = JSON.parse(line.slice(0, 200).replace(/,"qtype".*$/, "}")).id as string; // 和 sample.ts 相同的取法
  if (byId.has(id)) throw new Error(`题号重复：${id}`);
  byId.set(id, line);
}
const order = JSON.parse(readFileSync(join(dir, "memctx-lme-order.json"), "utf8")) as string[];
if (order.length !== 500 || new Set(order).size !== 500) throw new Error(`顺序文件应有 500 个不同的题号，实际 ${order.length}`);
if (byId.size !== 500) throw new Error(`memctx-lme.jsonl 应有 500 题，实际 ${byId.size}`);
const lines = order.map(id => { const l = byId.get(id); if (!l) throw new Error(`顺序里的 ${id} 不在 memctx-lme.jsonl`); return l; });

const first200 = readFileSync(join(dir, "memctx-lme-first200.jsonl"));
const head = Buffer.from(lines.slice(0, 200).join("\n") + "\n", "utf8");
if (!head.equals(first200)) throw new Error("前 200 题和 memctx-lme-first200.jsonl 不是逐字节相同：不写文件");

const text = lines.join("\n") + "\n";
const qtypes: Record<string, number> = {};
for (const l of lines) {
  const t = JSON.parse(l) as { id: string; qtype: string };
  const k = t.qtype + (t.id.endsWith("_abs") ? "（无法回答）" : "");
  qtypes[k] = (qtypes[k] ?? 0) + 1;
}
const out = join(dir, "memctx-lme-all500.jsonl");
writeFileSync(`${out}.partial`, text);
renameSync(`${out}.partial`, out);
const manifest = { tasks: lines.length, order: "memctx-lme-order.json（sample.ts，种子 20261119）", first200SameAsFile: "memctx-lme-first200.jsonl",
  first200Sha256: sha256(first200.toString("utf8")), sha256: sha256(text), bytes: Buffer.byteLength(text), qtypes };
writeFileSync(join(dir, "memctx-lme-all500.manifest.json"), JSON.stringify(manifest, null, 1) + "\n");
console.log(JSON.stringify(manifest));
