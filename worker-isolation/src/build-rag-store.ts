// rag 场景的内部知识库（docs\24）：把 rag-lme-first200.knowledge.jsonl 的每条知识写进官方 SQLite 记忆库。在容器里跑，写进 Docker 卷：
//
//   node src/build-rag-store.ts --knowledge /data/longmemeval-s/derived/rag-lme-first200.knowledge.jsonl --out /store/rag-first200.sqlite
//
// 经 Ditto 运行时的 MEMORY.WRITE 写（和 build-store.ts 相同的路子），用 rag 场景的 MEMORY Worker（rag.ts，官方写法）。
// 每题的来源一次写完。官方 rag-qa 用 MEMORY.GET 按 key 读出（index.ts 284–292 行）。每格从这份库的副本开始（app.ts 的 workingStore）。
// 先核对知识文件和 prepare-rag.ts 写的清单（摘要、条数）一致；库先写成 <out>.partial，行数对上才改成正式名字：
// 中途断了不会留下一个“非空但不完整”的库，被下次当成建好的（11-20）。
import { createHash } from "node:crypto";
import { copyFileSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createDitto, loadRuntimeConfig } from "@codesoul-co/ditto/runtime";
import { ragMemoryWorker } from "./rag.ts";
import type { Knowledge } from "./rag-sources.ts";
import { arg, jsonLines } from "./common.ts";

const manifestFile = arg("knowledge").replace(/\.knowledge\.jsonl$/, ".manifest.json");
const manifest = JSON.parse(readFileSync(manifestFile, "utf8")) as { knowledge_records: number; sha256: { knowledge: string } };
const knowledgeHash = createHash("sha256").update(readFileSync(arg("knowledge"))).digest("hex");
if (knowledgeHash !== manifest.sha256.knowledge) throw new Error(`知识文件的摘要和清单 ${manifestFile} 对不上：文件不完整或被改过`);
const work = "/tmp/build-rag-memory.sqlite";
rmSync(work, { force: true });
const config = loadRuntimeConfig({});
const memory = ragMemoryWorker(work, config);
const runtime = createDitto({ config, workers: [memory.worker] });
let batch: Knowledge[] = [], written = 0, task = "";
const flush = async () => {
  if (!batch.length) return;
  const result = await runtime.invoke("MEMORY.WRITE", { memories: batch });
  if (result.status !== "success") throw new Error(`写入失败 ${task} ${JSON.stringify(result.error)}`);
  written += (result.output as unknown[]).length; batch = [];
};
const t0 = Date.now();
for await (const line of jsonLines(arg("knowledge"))) {
  const k = JSON.parse(line) as Knowledge;
  const owner = k.key.replace(/-s\d+$/, "");
  if (owner !== task) { await flush(); task = owner; }
  batch.push(k);
}
await flush();
await runtime.close(); await memory.close();
// 在容器里的工作库上核对行数（打开库会在旁边生成 -shm、-wal 文件，不让它们出现在卷里），再整份复制、改名。
const db = new DatabaseSync(work, { readOnly: true });
const rows = (db.prepare("SELECT count(*) AS n FROM memories").get() as { n: number }).n;
db.close();
if (rows !== written || rows !== manifest.knowledge_records) throw new Error(`行数不一致：库里 ${rows}，写入 ${written}，清单 ${manifest.knowledge_records}`);
const out = arg("out"), partial = `${out}.partial`;
copyFileSync(work, partial);
renameSync(partial, out);
console.log(JSON.stringify({ out, rows, bytes: statSync(out).size, knowledgeSha256: knowledgeHash.slice(0, 16), elapsedS: (Date.now() - t0) / 1000 }));
