// 真实模型那一套提示词的辅助函数（src\app.ts）。node --test tests/real.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseQueries, renderHistory } from "../src/app.ts";
import { lmeAnswerPrompt } from "../src/common.ts";

const win = (session: string, turn: number, text: string, date: string) =>
  ({ id: `${session}:${turn}`, content: text, metadata: { session, date, speaker: text.split(":")[0] } });
const mem = (session: string, turn: number, text: string, date: string) =>
  ({ id: `memory:${session}-${turn}`, content: JSON.stringify({ text }), metadata: { sourceNode: "MEMORY.SEARCH", session, date, turn } });

test("renderHistory：官方 nl 格式；按日期、session、原轮次排，不按 SELECT 的相关度顺序；跳过当前目标", () => {
  const D1 = "2023/05/20 (Sat) 02:21", D9 = "2023/05/30 (Tue) 10:00";
  const items: any[] = [
    { id: "goal", content: "Question date: 2023/06/01\nQuestion: q?", metadata: { currentGoal: true } },
    win("s9", 1, "assistant: reply", D9),          // SELECT 把助手的回复排在了前面
    win("s9", 0, "user: newest  ", D9),
    mem("s1", 4, "user: old fact", D1),
    mem("s9", 0, "user: newest", D9),               // 和窗口里的 s9 第 0 轮是同一条消息
    { id: "memory:x", content: JSON.stringify({ text: "raw text without role" }), metadata: { sourceNode: "MEMORY.SEARCH", session: "s2", date: "2023/05/21 (Sun) 09:00" } },
  ];
  assert.equal(renderHistory(items),
    "\n### Session 1:\nSession Date: 2023/05/20 (Sat) 02:21\nSession Content:\n\n\nuser: old fact\n" +
    "\n### Session 2:\nSession Date: 2023/05/21 (Sun) 09:00\nSession Content:\n\n\nuser: raw text without role\n" +
    "\n### Session 3:\nSession Date: 2023/05/30 (Tue) 10:00\nSession Content:\n\n\nuser: newest\n\nassistant: reply\n");
});

test("renderHistory：同一日期的两个 session 不交错、不拆段；文字相同但轮次不同的两句都保留", () => {
  const D = "2023/05/22 (Mon) 11:21";
  const items: any[] = [
    win("sB", 0, "user: hi", D), win("sA", 1, "assistant: ok", D), win("sB", 1, "assistant: ok", D),
    mem("sA", 0, "user: hi", D), win("sA", 2, "user: hi", D),
  ];
  assert.equal(renderHistory(items),
    `\n### Session 1:\nSession Date: ${D}\nSession Content:\n\n\nuser: hi\n\nassistant: ok\n\nuser: hi\n` +
    `\n### Session 2:\nSession Date: ${D}\nSession Content:\n\n\nuser: hi\n\nassistant: ok\n`);
});

test("官方回答模板一字未改（run_generation.py 57 行）", () => {
  assert.equal(lmeAnswerPrompt("H", "D", "Q"),
    "I will give you several history chats between you and a user. Please answer the question based on the relevant chat history.\n\n\nHistory Chats:\n\nH\n\nCurrent Date: D\nQuestion: Q\nAnswer:");
});

test("parseQueries：去掉 ```json 围栏、容忍前后的话、取第一个完整的 JSON、最多 3 个、去重不分大小写；没有就算失败", () => {
  assert.deepEqual(parseQueries('```json\n{"queries":["degree","graduate"]}\n```'), ["degree", "graduate"]);
  assert.deepEqual(parseQueries('Sure: {"queries":["a","a","b","c","d"]} done'), ["a", "b", "c"]);
  assert.deepEqual(parseQueries('{"queries":["Coffee","coffee","shop"]} Note: {braces} here'), ["Coffee", "shop"]);
  assert.throws(() => parseQueries("no json here"), /no JSON/);
  assert.throws(() => parseQueries('{"queries":[]}'), /no queries/);
});
