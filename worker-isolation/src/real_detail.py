"""S4 真实模型验证的详细分析（docs\\20 用的表）。只读结果，不改数据。

    python -I src/real_detail.py <真实运行结果目录>      打印 Markdown

用到：raw\\*.json（每格原始记录）、judge\\judged.csv（每格每题的判分）、judge\\accuracy.json（判分用量）、
bench_data 里的 LongMemEval-S（题型）。上一格晚到的模型请求会混进下一格的记录，按 cell 字段排除。
"""
import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(sys.argv[1])
LAYOUT = {"mono": "Monolithic", "direct": "Direct", "split-memory": "拆 MEMORY", "split-context": "拆 CONTEXT", "split-both": "两个都拆",
          "split-memctx": "MEMORY+CONTEXT 合进一个进程"}

# 格子从结果目录里认：编号 = 布局-轮（例如 split-context-r1），按开跑时间排；stem 是文件名，也是出口记录里的 cell。
raw, STEM = {}, {}
for p in (OUT / "raw").glob("*.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    c = f"{d['meta']['layout']}-{d['meta']['label'].partition('|')[2]}"
    raw[c], STEM[c] = d, p.stem
ORDER = sorted(raw, key=lambda c: raw[c]["meta"]["started"])
NAME = {c: f"{LAYOUT.get(raw[c]['meta']['layout'], raw[c]['meta']['layout'])} {c.rsplit('-', 1)[1]}" for c in ORDER}
ROUNDS = sorted({c.rsplit("-", 1)[1] for c in ORDER})
# 和 Monolithic 比的各布局：Direct 放最前（全量实验，docs\\31），其余按名字
SPLITS = sorted({raw[c]["meta"]["layout"] for c in ORDER} - {"mono"}, key=lambda l: (l != "direct", l))
PAIRS = [(f"mono-{r}", f"{s}-{r}", f"{r}：Monolithic 对 {LAYOUT.get(s, s)}") for s in SPLITS for r in ROUNDS if f"mono-{r}" in raw and f"{s}-{r}" in raw]
PAIRS += [(f"{l}-{ROUNDS[0]}", f"{l}-{ROUNDS[1]}", f"参照：{LAYOUT.get(l, l)} 跑两次") for l in ["mono", *SPLITS]
          if len(ROUNDS) > 1 and f"{l}-{ROUNDS[0]}" in raw and f"{l}-{ROUNDS[1]}" in raw]


def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int((len(s) - 1) * p))] if s else None


def f1(x, fmt="{:.1f}"):
    return "—" if x is None else fmt.format(x)


# ---------------------------------------------------------------- 读数据

rows = {c: [r for r in raw[c]["fake"]["rows"] if r.get("cell", STEM[c]) == STEM[c]] for c in ORDER}
CELL_OF = {STEM[c]: c for c in ORDER}
# 判分结果（演练时没有）：没有就只出 A–E 几节（用时、节点、资源、模型请求、检索词），F–H 要判分
HAVE_JUDGE = (OUT / "judge" / "judged.csv").exists()
judged = {}
if HAVE_JUDGE:
    for r in csv.DictReader(open(OUT / "judge" / "judged.csv", encoding="utf-8-sig")):
        judged.setdefault(CELL_OF[r["格子"]], {})[f"{r['序号']}:{r['题号']}"] = r
qtype = {k: r["题型"] for k, r in judged[ORDER[0]].items()} if HAVE_JUDGE else {}
is_abs = lambda key: key.endswith("_abs")
label = lambda c, k: judged[c].get(k, {}).get("判对") == "True"
# 跑完而且判过（判分没做完时“判对”是空的，不能当判错）。分题型那张表仍按“失败、没判成的算错”。
done = lambda c, k: judged[c].get(k, {}).get("跑完") == "True" and judged[c][k].get("判对") in ("True", "False")
req = {c: {(r["task"], r["kind"]): r for r in rows[c]} for c in ORDER}

# ---------------------------------------------------------------- 1. 性能：用时分布、稳定性、每步用时

print("## A. 每题用时（稳态窗口里完成的题）\n")
print("| 格子 | 窗口内完成 | 吞吐（题/秒） | 每题用时 p50 / p95 / p99 / 最长（秒） | 窗口四段的吞吐 | 模型用时占每题用时（中位） |")
print("| --- | --- | --- | --- | --- | --- |")
for c in ORDER:
    d = raw[c]
    w = d["window"]
    lo, hi = w["from_ms"], w["to_ms"]
    inw = [t for t in d["tasks"] if t["ok"] and lo <= t["end_ms"] <= hi]
    lat = [(t["end_ms"] - t["start_ms"]) / 1000 for t in inw]
    seg = (hi - lo) / 4
    quarters = [sum(1 for t in inw if lo + q * seg <= t["end_ms"] < lo + (q + 1) * seg + (1e-6 if q == 3 else 0)) / (seg / 1000) for q in range(4)]
    share = []
    for t in inw:
        k = f"{t['i']}:{t['id']}"
        m = sum(req[c].get((k, kind), {}).get("latency_ms") or 0 for kind in ("keyword", "answer"))
        share.append(m / (t["end_ms"] - t["start_ms"]))
    print(f"| {NAME[c]} | {len(inw)} | {w['throughput_per_s']:.3f} | {pct(lat, .5):.1f} / {pct(lat, .95):.1f} / {pct(lat, .99):.1f} / {max(lat):.1f} | "
          f"{' / '.join(f'{x:.2f}' for x in quarters)} | {statistics.median(share):.0%} |")

print("\n## B. 每个节点每次调用的用时（稳态窗口；本进程的是执行时间，拆出去的是 HTTP 往返）\n")
nodes = sorted({n for c in ORDER for n in raw[c]["nodeWalls"]}, key=lambda n: (n.split(".")[0], n))
print("| 节点 | " + " | ".join(NAME[c] for c in ORDER) + " |")
print("| --- | " + " | ".join("---" for _ in ORDER) + " |")
for n in nodes:
    cells = []
    for c in ORDER:
        v = raw[c]["nodeWalls"].get(n)
        per = v["calls"] / raw[c]["window"]["completed"] if v else None
        cells.append("—" if not v else f"每题 {per:.1f} 次；平均 {v['mean_ms']:.0f}、p95 {v['p95_ms']:.0f} ms")
    print(f"| {n} | " + " | ".join(cells) + " |")

print("\n## C. 各进程资源（稳态窗口）\n")
print("| 格子 | 进程 | 每题主线程忙（ms） | 主线程忙的比例 | 用了几个核 | 内存峰值（MB） | 事件循环延迟 p99（ms） |")
print("| --- | --- | --- | --- | --- | --- | --- |")
for c in ORDER:
    w = raw[c]["window"]
    procs = {"编排器": w["orchestrator"], **{("MEMORY" if k == "memory" else k): v for k, v in w["remote"].items()}}
    for p, v in procs.items():
        print(f"| {NAME[c]} | {p} | {v['main_busy_ms'] / w['completed']:.0f} | {v['main_busy_share']:.0%} | {v['container_cores_used']:.2f} | "
              f"{v.get('container_mem_peak_mb') or v.get('max_rss_mb') or 0:.0f} | {v['loopDelay']['p99_ms']:.0f} |")

# ---------------------------------------------------------------- 2. 模型：用时、token、费用、检索词、回答

print("\n## D. 模型请求（每格自己的请求）\n")
print("| 格子 | 步骤 | 请求 | 模型用时 p50 / p95（秒） | 平均输入 token | 平均输出 token | 输出最多 | 结束原因 |")
print("| --- | --- | --- | --- | --- | --- | --- | --- |")
tok = lambda r, k: r["usage"].get(k) if isinstance(r.get("usage"), dict) and isinstance(r["usage"].get(k), int) else None  # 用量缺了也不崩
avg = lambda xs, fmt: fmt.format(statistics.mean(xs)) if xs else "—"
for c in ORDER:
    for kind in ("keyword", "answer"):
        rs = [r for r in rows[c] if r["kind"] == kind and r["ok"]]
        lat = [r["latency_ms"] / 1000 for r in rs]
        pt = [tok(r, "prompt_tokens") for r in rs if tok(r, "prompt_tokens") is not None]
        ct = [tok(r, "completion_tokens") for r in rs if tok(r, "completion_tokens") is not None]
        fr = Counter(r.get("finish_reason") for r in rs)
        print(f"| {NAME[c]} | {'检索词' if kind == 'keyword' else '回答'} | {len(rs)} | {pct(lat, .5):.1f} / {pct(lat, .95):.1f} | "
              f"{avg(pt, '{:,.0f}')} | {avg(ct, '{:.0f}')} | {max(ct) if ct else '—'} | {dict(fr)} |")
allrows = [r for c in ORDER for r in rows[c] if r["ok"]]
micro = lambda rs: sum(r.get("micro_cny") or 0 for r in rs)
per_task = micro(allrows) / 1e6 / sum(len(raw[c]["tasks"]) for c in ORDER)  # 分母是题数（不按请求数推）
ans = [r for r in allrows if r["kind"] == "answer"]
share_answer = micro(ans) / micro(allrows) if micro(allrows) else 0
total_tokens = sum(tok(r, "total_tokens") or 0 for r in allrows)
print(f"\n正式 {len(ORDER)} 格每题平均费用（高峰价）{per_task:.4f} 元，其中回答一步占 {share_answer:.1%}；"
      f"回答一步的输入 token 占全部 token 的 {(sum(tok(r, 'prompt_tokens') or 0 for r in ans) / total_tokens if total_tokens else 0):.1%}。"
      f"算不出费用的成功请求 {sum(1 for r in allrows if r.get('micro_cny') is None)} 个（没计入）。")

print("\n## E. 检索词和检索到的记忆\n")
print("| 格子 | 平均检索词个数 | 1 / 2 / 3 个的题数 | 含空格（多词）的检索词 | 平均检索到的记忆条数 | 一条都没检索到的题 | 最多 |")
print("| --- | --- | --- | --- | --- | --- | --- |")
for c in ORDER:
    ok = [t for t in raw[c]["tasks"] if t["ok"]]
    nq = Counter(len(t["queries"]) for t in ok)
    words = [q for t in ok for q in t["queries"]]
    hits = [t["hits"] for t in ok]
    print(f"| {NAME[c]} | {statistics.mean(len(t['queries']) for t in ok):.2f} | {nq[1]} / {nq[2]} / {nq[3]} | "
          f"{sum(1 for q in words if ' ' in q.strip())}/{len(words)} | {statistics.mean(hits):.1f} | {sum(1 for h in hits if h == 0)} | {max(hits)} |")

# ---------------------------------------------------------------- 3. 正确率：分题型、分歧题的来源

if not HAVE_JUDGE:
    print("\n（还没有判分结果：分题型正确率、分歧来源、判分用量这几节要判分以后才有。）")
    sys.exit(0)
types = sorted(set(qtype.values()))
print("\n## F. 分题型正确率（失败的题算错；“无法回答”题单列）\n")
pooled = lambda layout, keys: sum(label(f"{layout}-{r}", k) for r in ROUNDS for k in keys) / (len(ROUNDS) * len(keys))
print("| 题型 | 题数 | " + " | ".join(NAME[c] for c in ORDER) + " | " + " | ".join(f"各轮合计：Monolithic → {LAYOUT.get(s, s)}" for s in SPLITS) + " |")
print("| --- | --- | " + " | ".join("---" for _ in ORDER) + " | " + " | ".join("---" for _ in SPLITS) + " |")
groups = [(t, [k for k, v in qtype.items() if v == t and not is_abs(k)]) for t in types] + [("无法回答（各题型的 _abs 题）", [k for k in qtype if is_abs(k)])]
for name, keys in groups:
    if not keys:
        continue
    accs = [sum(label(c, k) for k in keys) / len(keys) for c in ORDER]
    mono = pooled("mono", keys)
    print(f"| {name} | {len(keys)} | " + " | ".join(f"{a:.0%}" for a in accs) + " | "
          + " | ".join(f"{mono:.0%} → {pooled(s, keys):.0%}（{(pooled(s, keys) - mono) * 100:+.0f}）" for s in SPLITS) + " |")
keys = list(qtype)
print(f"| 全部 | {len(keys)} | " + " | ".join(f"{sum(label(c, k) for k in keys) / len(keys):.1%}" for c in ORDER) + " | " + " | ".join("—" for _ in SPLITS) + " |")

print("\n## G. 只有一方答对的题从哪里来\n")
print("把两边都跑完的题分成两类：回答一步的请求逐字相同（出口加防缓存编号之前：检索词一样，模型看到的历史一样，只差开头一行不透明的编号）和不同。")
print("请求相同的题里出现的分歧，来自模型这一次的输出。\n")
print("| 比较 | 请求相同：题数 / 只有一方对（A 对 / B 对） | 请求不同：题数 / 只有一方对（A 对 / B 对） | 合计只有一方对 |")
print("| --- | --- | --- | --- |")
for a, b, title in PAIRS:
    common = [k for k in qtype if done(a, k) and done(b, k)]
    sha = lambda c, k: req[c].get((k, "answer"), {}).get("sha256")
    same = [k for k in common if sha(a, k) is not None and sha(a, k) == sha(b, k)]  # 两边都缺记录不算相同
    diff = [k for k in common if k not in same]
    def disc(ks):
        oa = sum(1 for k in ks if label(a, k) and not label(b, k))
        ob = sum(1 for k in ks if label(b, k) and not label(a, k))
        return f"{len(ks)} / {oa + ob}（{oa} / {ob}）"
    total = sum(1 for k in common if label(a, k) != label(b, k))
    print(f"| {title} | {disc(same)} | {disc(diff)} | {total} |")

# 各格里对错不一致的题：同一题几次里有对有错
mixed = [k for k in qtype if all(done(c, k) for c in ORDER) and 0 < sum(label(c, k) for c in ORDER) < len(ORDER)]  # 按实际格数
always = sum(1 for k in qtype if all(done(c, k) for c in ORDER) and all(label(c, k) for c in ORDER))
never = sum(1 for k in qtype if all(done(c, k) for c in ORDER) and not any(label(c, k) for c in ORDER))
print(f"\n{len(ORDER)} 格都跑完、判过的题里：{len(ORDER)} 次都对 {always} 题，{len(ORDER)} 次都错 {never} 题，有对有错 {len(mixed)} 题"
      f"（题型：{dict(Counter(qtype[k] + ('（无法回答）' if is_abs(k) else '') for k in mixed))}）。")
pattern = Counter("".join("对" if label(c, k) else "错" for c in ORDER) for k in mixed)
print(f"有对有错的题，{len(ORDER)} 格（" + " / ".join(NAME[c] for c in ORDER) + "）的对错模式：" + "，".join(f"{p} {n} 题" for p, n in pattern.most_common()))

# ---------------------------------------------------------------- 4. 判分用量

acc = json.loads((OUT / "judge" / "accuracy.json").read_text(encoding="utf-8"))
u = acc.get("judge_usage_this_run") or {}
print(f"\n## H. 判分\n\n判分请求 {acc['judge_calls_this_run']} 次，失败 {acc['judge_failures']}；返回的模型 {acc['judge_model_returned']}；用量 {u}。")
if u.get("prompt_tokens") is not None:
    print(f"按 OpenAI gpt-4o-2024-08-06 价格（输入 2.5、输出 10 美元/百万 token）约 ${u['prompt_tokens'] * 2.5e-6 + u.get('completion_tokens', 0) * 10e-6:.3f}（没核对账单）。")
answers = sum(1 for c in ORDER for k in qtype if judged[c].get(k, {}).get("跑完") == "True")
print(f"{len(ORDER)} 格跑完的回答共 {answers} 段，同一题文字相同的回答只判一次。")
