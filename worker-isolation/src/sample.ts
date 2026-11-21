// 在本机跑一次：固定题目顺序，切出各场景要用的任务文件。docs\16 第 4 节；换种子见 docs\32。
//
//   node --max-old-space-size=6144 src/sample.ts <实验根目录，例如 sec1-2.0 的完整路径>
//       原种子 20261119（11-19 起各轮用的）：写 derived\ 下的 memctx-lme-order.json、memctx-lme-first200.jsonl，
//       以及 LongBench v2 的两份和 bench_data\memctx-sample.json。
//   node --max-old-space-size=6144 src/sample.ts <实验根目录> --seed 42 --out <目录>
//       别的种子（docs\32）：只写 LongMemEval 的 memctx-lme-order.json、memctx-lme-first200.jsonl 和 sample.json 到 <目录>，
//       LongBench 的文件和 memctx-sample.json 不碰。只给 --out、不给 --seed 时是原种子（用来核对能原样复现）。
//
// - LongMemEval_S：500 个用户（每题一个用户）按固定种子打乱成一个顺序，写 memctx-lme-order.json；
//   前 200 个用户的完整历史写 memctx-lme-first200.jsonl（阶段一取前 50 个，阶段三取 200 个）。
//   记忆库规模 N 个用户 = 这个顺序的前 N 个，所以问答用到的用户一定在库里。
// - LongBench v2：短段、中段各按固定种子打乱，分别写 memctx-lb-short.jsonl、memctx-lb-medium.jsonl。
// 要写的文件已经存在时，内容相同就不重写，不同就报错停下（common.ts 的 writeNew）：已有的数据不会被换掉。
import { mkdirSync, realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import { arg, jsonLines, sha256, writeNew } from "./common.ts";

const sec = process.argv[2];
if (!sec || sec.startsWith("--")) throw new Error("用法：node src/sample.ts <sec1-2.0 目录> [--seed 42 --out <目录>]");
const ORIGINAL_SEED = 20261119;
const seed = Number(arg("seed", String(ORIGINAL_SEED)));
if (!Number.isSafeInteger(seed)) throw new Error(`--seed 要是整数，不是 ${arg("seed")}`);
const outArg = arg("out", "");
if (!outArg && seed !== ORIGINAL_SEED) throw new Error("换种子要用 --out 指定另外的目录：derived\\ 里放的是原种子的文件");

/** mulberry32：固定种子的伪随机数，打乱顺序可复现。 */
function rng(seed: number) {
  return () => { seed |= 0; seed = seed + 0x6d2b79f5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
function shuffle<T>(items: T[], seed: number): T[] {
  const a = [...items], r = rng(seed);
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [a[i], a[j]] = [a[j]!, a[i]!]; }
  return a;
}
async function lines(file: string) {
  const out: string[] = [];
  for await (const line of jsonLines(file)) out.push(line);
  return out;
}

const lmeDir = join(sec, "bench_data/longmemeval-s/derived"), lbDir = join(sec, "bench_data/longbench-v2/derived");
const outDir = outArg ? resolve(outArg) : lmeDir;
if (outArg) {
  mkdirSync(outDir, { recursive: true });
  // realpathSync.native：Windows 上盘符、大小写写法不同也认得出是同一个目录
  if (realpathSync.native(outDir) === realpathSync.native(lmeDir)) throw new Error("--out 不能是 derived\\ 本身：那里放的是原种子的文件");
}
const lme = await lines(join(lmeDir, "memctx-lme.jsonl"));
const lmeIds = lme.map(l => JSON.parse(l.slice(0, 200).replace(/,"qtype".*$/, "}")).id as string);
const order = shuffle(lmeIds.map((_, i) => i), seed);
const orderText = JSON.stringify(order.map(i => lmeIds[i]));
writeNew(join(outDir, "memctx-lme-order.json"), orderText);
const first200 = order.slice(0, 200).map(i => lme[i]!).join("\n") + "\n";
writeNew(join(outDir, "memctx-lme-first200.jsonl"), first200);

if (outArg) {
  // 别的种子：只有 LongMemEval 的两份，加一份说明（sample500.ts 从这里读种子）
  const summary = { seed, lmeUsers: order.length, first200: 200, first200Sha256: sha256(first200), orderSha256: sha256(orderText),
    firstUsers: order.slice(0, 5).map(i => lmeIds[i]), input: "derived/memctx-lme.jsonl", command: `node src/sample.ts <实验根目录> --seed ${seed} --out <这个目录>` };
  writeNew(join(outDir, "sample.json"), JSON.stringify(summary, null, 1) + "\n");
  console.log(JSON.stringify(summary));
} else {
  // CONTEXT 单条上限最大只能设到 1,000,000 字节（政策校验）。有一道题的某一块超过它（整行没有换行），载入必然失败，剔除并记下。
  const MAX_ITEM_BYTES = 1_000_000;
  const excluded: string[] = [];
  const lb = (await lines(join(lbDir, "memctx-lb.jsonl"))).filter(l => {
    const t = JSON.parse(l) as { id: string; chunks: { text: string }[] };
    if (t.chunks.some(c => Buffer.byteLength(c.text) > MAX_ITEM_BYTES)) { excluded.push(t.id); return false; }
    return true;
  });
  const seg = (l: string) => /"segment":"(short|medium)"/.exec(l.slice(0, 200))![1];
  const short = shuffle(lb.filter(l => seg(l) === "short"), 20261120), medium = shuffle(lb.filter(l => seg(l) === "medium"), 20261121);
  writeNew(join(lbDir, "memctx-lb-short.jsonl"), short.join("\n") + "\n");
  writeNew(join(lbDir, "memctx-lb-medium.jsonl"), medium.join("\n") + "\n");
  const summary = { lmeUsers: order.length, first200: 200, first200Sha256: sha256(first200), lbShort: short.length, lbMedium: medium.length,
    lbExcludedOverItemLimit: excluded, firstUsers: order.slice(0, 5).map(i => lmeIds[i]) };
  writeNew(join(sec, "bench_data/memctx-sample.json"), JSON.stringify(summary, null, 1));
  console.log(JSON.stringify(summary));
}
