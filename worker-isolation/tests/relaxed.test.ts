// 对官方文件的改动只有 relaxed\CHANGES.md 写的那些。node --test tests/relaxed.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { memoryPointId as relaxedId } from "../relaxed/examples/_shared/tools/storage/sql-memory.ts";
import { memoryPointId as officialId } from "../official/examples/_shared/tools/storage/qdrant-memory.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

test("sqlite-memory.ts 和官方逐字相同", () => {
  assert.equal(read("../relaxed/examples/_shared/tools/storage/sqlite-memory.ts"), read("../official/examples/_shared/tools/storage/sqlite-memory.ts"));
});

test("sql-memory.ts 只多了 memoryPointId，并只改了 write 的 id 一行", () => {
  const official = read("../official/examples/_shared/tools/storage/sql-memory.ts").split("\n");
  const relaxed = read("../relaxed/examples/_shared/tools/storage/sql-memory.ts").split("\n");
  const removed = official.filter(line => !relaxed.includes(line));
  const added = relaxed.filter(line => !official.includes(line));
  assert.deepEqual(removed, [
    'import { randomUUID } from "node:crypto";',
    "      const items = input.memories.map(memory => ({ ...memory, id: randomUUID() }));",
  ]);
  // 新加的 6 行：import、注释、函数头、两行函数体、write 的 id（函数结尾的 "}" 官方原文里也有，不算新行）。
  assert.equal(added.length, 6);
  assert.ok(added.includes("      const items = input.memories.map(memory => ({ ...memory, id: memory.key ? memoryPointId(memory.key) : randomUUID() }));"));
});

test("memoryPointId 和官方 qdrant-memory.ts 的结果相同", () => {
  for (const key of ["lme:abc:sharegpt_x_0:3", "s4:12_e47becba:answer_1:0", "中文"]) assert.equal(relaxedId(key), officialId(key));
});

test("BM25 用官方默认 12 条，SELECT/COMPRESS 用官方默认 256 条", async () => {
  const app = await import("../src/app.ts");
  assert.equal(app.BM25_LIMIT, 12);
  assert.equal(app.OFFICIAL_MAX_ITEMS, 256);
});
