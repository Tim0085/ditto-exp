// tests\direct.test.ts 的子进程（MEMCTX_REAL=1：真实模型那一套提示词只能在进程启动时选）。
// S4 经出口（egress.ts）到假模型：Monolithic 和 Direct 各跑同样几道题（同时跑），最后一行打印两边交给出口的请求
// （“题|步骤” → 请求体摘要）和两边的结果是否相同。不联网、不碰 Docker。
//
//   MEMCTX_REAL=1 node tests/direct-real-child.ts <假模型的 /v1 地址> <出口账本目录> <题数>
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDitto } from "@codesoul-co/ditto/runtime";
import { contextWorker, inferWorker, memoryWorker, runtimeConfig, taskLoop, type Job, type LmeTask } from "../src/app.ts";
import { REAL, jsonLines } from "../src/common.ts";
import { CallLog } from "../src/calls.ts";
import { createDirectS4 } from "../src/direct.ts";
import { startEgress } from "../src/egress.ts";

const [fakeUrl, ledger, n] = process.argv.slice(2) as [string, string, string];
const work = join(dirname(ledger), "real-child");
mkdirSync(work, { recursive: true });
const tasks: LmeTask[] = [];
for await (const line of jsonLines(fileURLToPath(new URL("../../bench_data/longmemeval-s/derived/memctx-lme-first200.jsonl", import.meta.url)))) {
  tasks.push(JSON.parse(line)); if (tasks.length === Number(n)) break;
}
const jobs: Job[] = tasks.map((t, i) => ({ scenario: "s4", key: `${i}:${t.id}`, run: `${i}:${t.id}`, tag: "direct-real", lme: t }));

const egress = await startEgress({ port: 0, host: "127.0.0.1", apiKey: "sk-tstCHILD0000000000", upstream: fakeUrl, model: "deepseek-flash",
  ledgerDir: ledger, limitMicroCny: 1_000_000_000 });
const config = runtimeConfig(`${egress.url}/v1`);
async function cell(name: string) {
  const r = await fetch(`${egress.url}/reset?seed=${name}`, { method: "POST" });
  if (!r.ok) throw new Error(`出口复位失败 ${r.status}`);
  return async () => {
    const s = await (await fetch(`${egress.url}/stats`)).json() as { rows: { task: string; kind: string; sha256: string; ok: boolean }[] };
    if (!s.rows.every(r => r.ok)) throw new Error("出口有没成功的请求");
    return Object.fromEntries(s.rows.map(r => [`${r.task}|${r.kind}`, r.sha256]));
  };
}

const monoRows = await cell("mono");
const mem = memoryWorker(join(work, "mono.sqlite"));
const runtime = createDitto({ config, workers: [inferWorker(), mem.worker, contextWorker("memory", "default")] });
const mono = await Promise.all(jobs.map(job => runtime.loop(taskLoop, job)));
await runtime.close(); await mem.close();
const a = await monoRows();

const directRows = await cell("direct");
const d = createDirectS4({ config, db: join(work, "direct.sqlite"), contextStore: "memory", calls: new CallLog() });
const direct = await Promise.all(jobs.map(job => d.run(job)));
await d.close();
const b = await directRows();
await egress.close();

console.log(JSON.stringify({ real: REAL, mono: a, direct: b, answersSame: JSON.stringify(mono) === JSON.stringify(direct) }));
