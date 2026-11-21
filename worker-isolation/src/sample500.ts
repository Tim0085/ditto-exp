// 全量 500 题（docs\31）：按 sample.ts 写下的固定顺序 memctx-lme-order.json，写出全部 500 题 memctx-lme-all500.jsonl。
// 在本机跑一次：
//
//   node --max-old-space-size=6144 src/sample500.ts <实验根目录，例如 sec1-2.0 的完整路径>
//       原种子 20261119：读写 derived\。
//   node --max-old-space-size=6144 src/sample500.ts <实验根目录> --dir <目录>
//       别的种子（docs\32）：读写 <目录>（sample.ts --out 写的那个，种子从它的 sample.json 读）；500 题的原文仍从 derived\memctx-lme.jsonl 读。
//
// 前 200 行必须和同一目录的 memctx-lme-first200.jsonl 逐字节相同（11-19、11-20 各轮用的就是原种子的那份）：对不上就报错、不写文件。
// 另写 memctx-lme-all500.manifest.json：题数、各题型题数、摘要。已有文件内容不同就报错，不覆盖（common.ts 的 writeNew）。
import { readFileSync, realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import { arg, jsonLines, sha256, writeNew } from "./common.ts";

const sec = process.argv[2];
if (!sec || sec.startsWith("--")) throw new Error("用法：node src/sample500.ts <sec1-2.0 目录> [--dir <目录>]");
const base = join(sec, "bench_data/longmemeval-s/derived");
const dir = resolve(arg("dir", base));
// 原目录是原种子；别的目录的种子写在 sample.ts 留下的 sample.json 里，并核对它记的顺序摘要和顺序文件对得上。
// 比较目录用 realpathSync.native：Windows 上盘符、大小写写法不同也认得出是同一个目录。
const seed = realpathSync.native(dir) === realpathSync.native(base) ? 20261119 : (() => {
  const s = JSON.parse(readFileSync(join(dir, "sample.json"), "utf8")) as { seed?: unknown; orderSha256?: unknown };
  if (!Number.isSafeInteger(s.seed)) throw new Error(`${join(dir, "sample.json")} 里没有整数的 seed`);
  if (s.orderSha256 !== sha256(readFileSync(join(dir, "memctx-lme-order.json"), "utf8")))
    throw new Error(`${join(dir, "memctx-lme-order.json")} 和 sample.json 记的顺序摘要对不上（顺序文件被换过？）`);
  return s.seed as number;
})();

const byId = new Map<string, string>();
for await (const line of jsonLines(join(base, "memctx-lme.jsonl"))) {
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
writeNew(join(dir, "memctx-lme-all500.jsonl"), text);
const manifest = { tasks: lines.length, order: `memctx-lme-order.json（sample.ts，种子 ${seed}）`, first200SameAsFile: "memctx-lme-first200.jsonl",
  first200Sha256: sha256(first200.toString("utf8")), sha256: sha256(text), bytes: Buffer.byteLength(text), qtypes };
writeNew(join(dir, "memctx-lme-all500.manifest.json"), JSON.stringify(manifest, null, 1) + "\n");
console.log(JSON.stringify(manifest));
