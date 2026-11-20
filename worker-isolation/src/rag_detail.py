"""rag-qa 真实模型那一轮的补充分析（docs\\30 用的表）。只读结果，不改。

    python -I src/rag_detail.py <结果目录> <题目文件 rag-a-first200.jsonl>     打印 Markdown

1. 每格：各步模型用时中位、第二次回答 / 核对（官方重来）的次数、官方回答状态、失败原因。
2. 只有一边答对的题：两边发给模型的各步请求是否逐字相同（出口记的是加防缓存编号之前的请求体哈希）。
   回答一步的请求相同、结果一对一错，说明差别来自模型对同一输入的回答不稳定，不是布局。
出口按问题文字的哈希记题（rag:<16 位>），这里用题目文件把它对回题号。
"""
import csv
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT, TASKS = Path(sys.argv[1]), Path(sys.argv[2])
NAME = {"mono": "Monolithic", "direct": "Direct", "split-interaction": "拆 INTERACTION+RETRIEVAL"}
STEPS = ("plan", "select", "answer", "verify")
STEP = {"plan": "理解问题", "select": "挑证据", "answer": "回答", "verify": "核对"}

qid_of = {}
for line in TASKS.read_text(encoding="utf-8").split("\n"):
    if line.strip():
        t = json.loads(line)
        qid_of["rag:" + hashlib.sha256(t["question"].encode("utf-8")).hexdigest()[:16]] = t["id"]

cells = {}
for p in sorted((OUT / "raw").glob("*.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    rows = [r for r in d["fake"]["rows"] if r.get("cell", p.stem) == p.stem]
    cells[p.stem] = {"meta": d["meta"], "tasks": d["tasks"], "rows": rows, "round": d["meta"]["label"].partition("|")[2]}

base = lambda kind: kind.partition("#")[0]
print("## 每格：模型各步、官方重来、回答状态、失败\n")
print("| 格子 | 各步模型用时中位（秒）：理解问题 / 挑证据 / 回答 / 核对 | 回答重来 / 核对重来（次） | 每题模型请求（平均） | 证据块（中位） | 失败原因（次数） |")
print("| --- | --- | --- | --- | --- | --- |")
for name, c in cells.items():
    med = lambda k: statistics.median([r["latency_ms"] for r in c["rows"] if r["ok"] and base(r["kind"]) == k] or [0]) / 1000
    redo = lambda k: sum(1 for r in c["rows"] if r["kind"].startswith(k + "#"))
    ev = [t["evidence"] for t in c["tasks"] if t["ok"] and t.get("evidence") is not None]
    fails = Counter((t.get("failKind") or "?") + "：" + (t.get("error") or "")[:60] for t in c["tasks"] if not t["ok"])
    print(f"| {name} | {' / '.join(f'{med(k):.1f}' for k in STEPS)} | {redo('answer')} / {redo('verify')} | {len(c['rows']) / len(c['tasks']):.2f} | "
          f"{statistics.median(ev) if ev else '—'} | {'；'.join(f'{k}（{v}）' for k, v in fails.most_common(5)) or '—'} |")

judged = {}
jpath = OUT / "judge" / "judged.csv"
if jpath.exists():
    for r in csv.DictReader(open(jpath, encoding="utf-8-sig")):
        judged[(r["格子"], r["题号"])] = r["判对"] == "True" if r["判对"] in ("True", "False") else None

print("\n## 只有一边答对的题：两边发给模型的请求是否相同（出口加防缓存编号之前）\n")
if not judged:
    print("（还没有判分结果：这一节要判分以后才有。）")
    sys.exit(0)
print("回答一步只比第一次；官方回答或核对不合格时会重来（answer#2、verify#2），“整条调用链相同”比这一题的全部请求。\n")
print("| 并发、轮 | 只有一边对的题 | 其中回答一步（第一次）的请求逐字相同 | 理解问题一步相同 | 挑证据一步相同 | 整条调用链逐字相同 | 全部题里回答一步相同 |")
print("| --- | --- | --- | --- | --- | --- | --- |")
by_round = {}
for name, c in cells.items():
    by_round.setdefault(c["round"], {})[c["meta"]["layout"]] = name
# 每一轮里 Monolithic 和其余每种布局（全量实验有 Direct 和拆开两种，docs\\31）各比一次
for rnd, layouts in sorted(by_round.items()):
    if "mono" not in layouts or len(layouts) < 2:
        continue
    for other in sorted(k for k in layouts if k != "mono"):
        a_name, b_name = layouts["mono"], layouts[other]
        a, b = cells[a_name], cells[b_name]
        sha = lambda c: {(qid_of.get(r["task"], r["task"]), r["kind"]): r["sha256"] for r in c["rows"]}
        A, B = sha(a), sha(b)
        same = lambda q, k: (q, k) in A and A[(q, k)] == B.get((q, k))
        chain = lambda q: all(A.get(k) == B.get(k) for k in set(A) | set(B) if k[0] == q) and any(k[0] == q for k in A)
        qs = [t["id"] for t in a["tasks"]]
        disc = [q for q in qs if judged.get((a_name, q)) is not None and judged.get((b_name, q)) is not None and judged[(a_name, q)] != judged[(b_name, q)]]
        print(f"| {rnd}：Monolithic 对 {NAME.get(other, other)} | {len(disc)} | {sum(same(q, 'answer') for q in disc)} | {sum(same(q, 'plan') for q in disc)} | "
              f"{sum(same(q, 'select') for q in disc)} | {sum(chain(q) for q in disc)} | {sum(same(q, 'answer') for q in qs)}/{len(qs)} |")
