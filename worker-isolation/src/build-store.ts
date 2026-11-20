// 建多用户共用的官方 SQLite 记忆库（在容器里跑，写进 Docker 卷）。docs\16 S1“记忆库规模”。
//
//   node src/build-store.ts --tasks /data/longmemeval-s/derived/memctx-lme.jsonl --order /data/longmemeval-s/derived/memctx-lme-order.json
//        --scales 50,100,250,500 --out /store
//
// 按 memctx-lme-order.json 的顺序逐个用户写入：每个 session 一次 MEMORY.WRITE（经 Ditto 运行时和官方 SQLite 适配器，
// 和应用里的写入完全相同），每条用户消息一条记忆，命名空间 lme:<题号>。写到第 N 个用户时关库、复制一份 users-N.sqlite，
// 再接着写。这样规模大的库包含规模小的库的全部记录（记录编号也相同）。
import { copyFileSync, readFileSync, statSync, writeFileSync, rmSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createDitto, loadRuntimeConfig } from "@codesoul-co/ditto/runtime";
import { memoryWorker, userMemories, type LmeTask } from "./app.ts";
import { arg, jsonLines } from "./common.ts";

const order = JSON.parse(readFileSync(arg("order"), "utf8")) as string[];
const scales = arg("scales").split(",").map(Number).sort((a, b) => a - b);
const outDir = arg("out");
const max = scales.at(-1)!;
const wanted = new Set(order.slice(0, max));
const byId = new Map<string, LmeTask>();
for await (const line of jsonLines(arg("tasks"))) {
  const t = JSON.parse(line) as LmeTask;
  if (wanted.has(t.id)) byId.set(t.id, t);
}
const work = "/tmp/build-memory.sqlite";
rmSync(work, { force: true });
const report: any[] = [];
let memory = memoryWorker(work), runtime = createDitto({ config: loadRuntimeConfig({}), workers: [memory.worker] });
let rows = 0;
const t0 = Date.now();
for (let u = 0; u < max; u++) {
  const t = byId.get(order[u]!)!;
  for (const session of t.sessions) {
    const memories = userMemories(`lme:${t.id}`, session);
    if (!memories.length) continue;
    const result = await runtime.invoke("MEMORY.WRITE", { memories });
    if (result.status !== "success") throw new Error(`写入失败 ${t.id} ${JSON.stringify(result.error)}`);
    rows += result.output!.length;
  }
  if (scales.includes(u + 1)) {
    await runtime.close(); await memory.close();
    const target = `${outDir}/users-${u + 1}.sqlite`;
    copyFileSync(work, target);
    const db = new DatabaseSync(target, { readOnly: true });
    const count = (db.prepare("SELECT count(*) AS n FROM memories").get() as { n: number }).n;
    db.close();
    report.push({ users: u + 1, rows: count, bytes: statSync(target).size, elapsedS: (Date.now() - t0) / 1000 });
    console.log(JSON.stringify(report.at(-1)));
    if (count !== rows) throw new Error(`行数不一致 ${count} != ${rows}`);
    memory = memoryWorker(work); runtime = createDitto({ config: loadRuntimeConfig({}), workers: [memory.worker] });
  }
}
await runtime.close(); await memory.close();
writeFileSync(`${outDir}/stores.json`, JSON.stringify({ order: order.slice(0, max), scales: report }, null, 1));
