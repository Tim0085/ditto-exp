"""
全量实验（docs\\36）：按跑之前写定的口径（docs\\36 第 3 节）出结论表。只读结果，不改数据。

    python -I report/full_criteria.py <结果目录>

读 run-info.json、cells.csv、pairs.csv、pairs-direct.csv、pairs-memctx.csv（各布局有才读）、judge\\accuracy.json、
judge\\accuracy.csv、judge\\accuracy-pairs.csv，写 <结果目录>\\结论.md、结论.json，并把 Markdown 打印出来。
开头先删掉上一次的 结论.md、结论.json：中途停下时目录里不留旧的，免得被当成这一次的。

口径（docs\\36 第 3 节，跑之前写定）：
  1. 吞吐：主数是整波平均的两轮合并收益，即两轮吞吐之和的比；两点法照列，每一轮也单列。
     比三组：拆开对 Monolithic（pairs.csv）、各布局对 Direct（pairs-direct.csv）、合并对两个都隔离（pairs-memctx.csv）。
     收益由 cells.csv 的吞吐重算，再和这三张表逐个核对（表里留 4 位小数，差在 5e-4 以内算一致）；缺的、多的、重复的都停下。
     吞吐只计成功的题：rag 里过不了官方输出检查（模型输出不合格）的题不计入，各格的题数另外列出。
  2. 正确率：拆开对 Monolithic 的单轮比较（每种拆开的布局 × 每轮）单独做 Holm 校正，校正后都 > 0.05 就是“没有检出差别”。
     这一族只含本目录（一个计划）的比较：S4、rag 各自一族。
     p 用 judge\\accuracy-pairs.csv 里“只有A对”“只有B对”两个整数按符号检验重算（表里的 p 只留 4 位小数，压在界上时会判反），
     并和表里的 p 核对；正确率差也用这两个整数和共同题数核对。同时列 95% 区间和“同一布局跑两次”的参照。
     两轮合并、对 Direct 的比较照列，作描述，不做校正。report.md 里“Holm 校正后 p”那一列是表里全部比较一起校正的，不是这里的口径。
  3. 另列每格的分段吞吐（第 20–199、199–349、349 题以后完成的题）、每题主线程、正确率。
同一批的核对：run-info.json 记了每格题数的，cells.csv 里每格的题数要等于它；判分做完时，judge\\accuracy.csv 的格子要和 cells.csv 的一样，
每格的题数、跑完的题数、模型输出不合格的题数都要对得上。
"""
import csv
import json
import sys
from math import comb
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")   # 报错（SystemExit）走 stderr
if len(sys.argv) != 2:
    raise SystemExit("用法：python -I report/full_criteria.py <结果目录>")
OUT = Path(sys.argv[1])
for f in ("结论.md", "结论.json"):   # 中途停下时不留上一次的结论
    (OUT / f).unlink(missing_ok=True)
ALPHA = 0.05
TWO, PHASE = "throughput_per_s", "throughput_phase_avg"
SEGS = ("throughput_seg_20_199", "throughput_seg_199_349", "throughput_seg_349_end")
NAME = {"mono": "Monolithic", "direct": "Direct", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT",
        "split-both": "两个都隔离", "split-memctx": "合并", "split-interaction": "隔离 INTERACTION+RETRIEVAL"}
PAIR_KEYS = ("n", "excluded", "a", "b", "only_a", "only_b", "diff", "lo", "hi", "p")


def name(layout):
    return NAME.get(layout, layout)


def label(text):
    """judge.py 的比较名换成这里的叫法：它的“拆开对合并”指拆开对 Monolithic，而这里的“合并”是 split-memctx。"""
    text = text.replace("拆开对合并：", "对 Monolithic：")
    for k in sorted(NAME, key=len, reverse=True):   # 长的先换
        text = text.replace(k, NAME[k])
    return text


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(row, key):
    v = row.get(key)
    return None if v in ("", None) else float(v)


def count(row, key):
    """题数这类整数（表里可能写成 500 或 500.0）。"""
    return int(float(row[key]))


def sign_test(b, c):
    """双侧符号检验，和 judge.py 的 sign_test 相同：只有一方答对的题里，两边各占几题。"""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def holm(ps):
    """Holm 校正：p 从小到大第 i 个（从 0 数）乘 (m − i)，再取到它为止的最大值，不超过 1。"""
    m, running, adj = len(ps), 0.0, {}
    for i, (k, p) in enumerate(sorted(ps.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def pct(x):
    return "—" if x is None else f"{x * 100:+.2f}%"


def fmt(x, spec):
    return "—" if x is None else format(x, spec)


def pfmt(p):
    """p 值：很小的写“<0.001”；离 0.05 不到 0.001 的多印几位，免得印出来的数和结论的话对不上。"""
    if p < 0.001:
        return "<0.001"
    if abs(p - ALPHA) < 0.001:
        return f"{p:.6f}"
    return f"{p:.4f}"


def peq(p):
    return "< 0.001" if p < 0.001 else f"= {pfmt(p)}"


# ---------------------------------------------------------------- 格子、计划
cells = {}
for r in read_csv(OUT / "cells.csv"):
    key = (r["layout"], r["round"])
    if key in cells:
        raise SystemExit(f"cells.csv 里 {key} 出现两次")
    cells[key] = r
layouts = sorted({l for l, _ in cells}, key=lambda l: (l != "mono", l != "direct", l))
rounds = sorted({r for _, r in cells})
splits = [l for l in layouts if l not in ("mono", "direct")]
if "mono" not in layouts or not splits or len(rounds) != 2:
    raise SystemExit(f"要有 Monolithic、至少一种拆法、正好两轮：实际布局 {layouts}，轮 {rounds}")
missing = [(l, r) for l in layouts for r in rounds if (l, r) not in cells]
if missing:
    raise SystemExit(f"缺格子：{missing}")
concs = {r["concurrency"] for r in cells.values()}
if len(concs) != 1:
    raise SystemExit(f"各格的并发不一样：{sorted(concs)}")
CONC = concs.pop()
# 整种布局缺了、多了，只有和计划比才查得出来，所以 run-info.json 必须在
if not (OUT / "run-info.json").exists():
    raise SystemExit(f"{OUT} 里没有 run-info.json：没法和计划核对格子，不出结论")
plan = json.loads((OUT / "run-info.json").read_text(encoding="utf-8"))["plan"]
want = sorted(tuple(s.split(":")[:2]) for s in plan["formal"].split())
if sorted(cells) != want:
    raise SystemExit(f"cells.csv 的格子和 run-info.json 的计划对不上：{sorted(cells)} 对 {want}")
if plan.get("conc") not in (None, "", CONC):
    raise SystemExit(f"run-info.json 记的并发是 {plan['conc']}，cells.csv 里是 {CONC}")
if plan.get("count") not in (None, ""):
    wrong = [c["cell"] for c in cells.values() if count(c, "tasks") != int(plan["count"])]
    if wrong:
        raise SystemExit(f"run-info.json 记的每格题数是 {plan['count']}，这几格不是：{wrong}")
plan_note = f"格子和 run-info.json 的计划一致（{len(want)} 格）"
# 布局按计划里第一次出现的顺序排（Monolithic、Direct 在前），每格按开跑的顺序列
run_order = [tuple(s.split(":")[:2]) for s in plan["formal"].split()]
first = list(dict.fromkeys(l for l, _ in run_order))
layouts = sorted(layouts, key=lambda l: (l != "mono", l != "direct", first.index(l)))
splits = [l for l in layouts if l not in ("mono", "direct")]


def tp(layout, rnd, key):
    v = num(cells[(layout, rnd)], key)
    if v is None:
        raise SystemExit(f"{cells[(layout, rnd)]['cell']} 的 {key} 是空的（窗口无效？）")
    return v


def model_fail(c):
    """这一格模型输出不合格的题数（rag 才有这一列；S4 是 None）。"""
    return None if c.get("failed_model_output") in (None, "") else count(c, "failed_model_output")


# ---------------------------------------------------------------- 吞吐
def gain(base, layout, key, rs):
    """layout 对 base 的吞吐收益：rs 里各轮吞吐之和的比 − 1（单轮就是那一轮的比）。"""
    return sum(tp(layout, r, key) for r in rs) / sum(tp(base, r, key) for r in rs) - 1


def gains(base, layout):
    out = {r: {"two": gain(base, layout, TWO, [r]), "phase": gain(base, layout, PHASE, [r])} for r in rounds}
    out["pooled"] = {"two": gain(base, layout, TWO, rounds), "phase": gain(base, layout, PHASE, rounds)}
    return out


G = {s: gains("mono", s) for s in splits}                                                  # 拆开对 Monolithic
D = {l: gains("direct", l) for l in layouts if l != "direct"} if "direct" in layouts else {}  # 各布局对 Direct
M = {"split-memctx": gains("split-both", "split-memctx")} if {"split-both", "split-memctx"} <= set(layouts) else {}  # 合并对两个都隔离
COLS = (("two", "throughput_gain"), ("phase", "throughput_gain_phase"))


def cross_check(table, results):
    """export.py 写的表里，results 的每种布局都要有两轮加两轮合并、两点法和整波平均两列，逐个和这里算的一致；缺的、多的、重复的都停。"""
    if not results:
        return
    if not (OUT / table).exists():
        raise SystemExit(f"缺 {table}")
    seen = set()
    for r in read_csv(OUT / table):
        l = r["layout"]
        if l not in results:
            raise SystemExit(f"{table} 里有多出来的布局：{l} {r['round']}")
        rnd = "pooled" if r["round"].startswith("两轮合并") else r["round"]
        if rnd not in results[l]:
            raise SystemExit(f"{table} 里有不认识的轮：{l} {r['round']}")
        for key, col in COLS:
            if (l, rnd, col) in seen:
                raise SystemExit(f"{table} 里 {l} {r['round']} 出现两次")
            seen.add((l, rnd, col))
            v = num(r, col)
            if v is None:
                raise SystemExit(f"{table} 里 {l} {r['round']} 的 {col} 是空的")
            if abs(results[l][rnd][key] - v) > 5e-4:
                raise SystemExit(f"{table} 里 {l} {r['round']} 的 {col}：这里算出 {results[l][rnd][key]:.4f}，表里是 {v}")
    need = {(l, rnd, col) for l in results for rnd in [*rounds, "pooled"] for _, col in COLS}
    if seen != need:
        raise SystemExit(f"{table} 里缺：{sorted(need - seen)}")


cross_check("pairs.csv", G)
cross_check("pairs-direct.csv", D)
cross_check("pairs-memctx.csv", M)

# ---------------------------------------------------------------- 正确率
acc = {"complete": False}
per_cell_acc = {}
acc_file = OUT / "judge" / "accuracy.json"
if acc_file.exists() and json.loads(acc_file.read_text(encoding="utf-8")).get("complete") is True:
    # 同一批：判分表的格子、每格题数、跑完的题数、模型输出不合格的题数，都要和 cells.csv 对得上
    acc_rows = read_csv(OUT / "judge" / "accuracy.csv")
    per_cell_acc = {r["格子"]: r for r in acc_rows}
    by_name = {c["cell"]: c for c in cells.values()}
    if len(acc_rows) != len(per_cell_acc) or sorted(per_cell_acc) != sorted(by_name):
        raise SystemExit(f"judge\\accuracy.csv 的格子和 cells.csv 的不一样：{sorted(per_cell_acc)} 对 {sorted(by_name)}")
    for k, a in per_cell_acc.items():
        c = by_name[k]
        if count(a, "题数") != count(c, "tasks") or count(a, "跑完") != count(c, "tasks") - count(c, "failed"):
            raise SystemExit(f"{k}：判分表的题数 {a['题数']}、跑完 {a['跑完']}，和 cells.csv 的 {c['tasks']} 题、失败 {c['failed']} 对不上")
        if a.get("模型输出不合格") not in (None, ""):
            if model_fail(c) is None or count(a, "模型输出不合格") != model_fail(c):
                raise SystemExit(f"{k}：判分表的模型输出不合格 {a['模型输出不合格']} 题，cells.csv 是 {c.get('failed_model_output')}")
    rows = read_csv(OUT / "judge" / "accuracy-pairs.csv")

    def pair(x):
        """一行比较：p 按两个整数重算，并和表里的（留 4 位小数）核对；正确率差也核对。没有共同题时返回 None。"""
        if x["符号检验p"] in ("", None):
            return None
        b, c, n = int(x["只有A对"]), int(x["只有B对"]), int(x["共同题数"])
        p = sign_test(b, c)
        if abs(p - float(x["符号检验p"])) > 6e-5:
            raise SystemExit(f"{x['比较']}（{x['A']} 对 {x['B']}）：按 {b}/{c} 重算的 p 是 {p:.6f}，表里是 {x['符号检验p']}")
        d = float(x["正确率差B减A"])
        if n <= 0 or abs((c - b) / n - d) > 5e-5 or abs(float(x["B正确率"]) - float(x["A正确率"]) - d) > 1.5e-4:
            raise SystemExit(f"{x['比较']}（{x['A']} 对 {x['B']}）：正确率差 {d}，和只有一方对的 {b}/{c}、共同题数 {n}、两边正确率对不上")
        return {"n": n, "excluded": int(x["排除的题（没跑完或没判成）"]), "a": float(x["A正确率"]), "b": float(x["B正确率"]),
                "only_a": b, "only_b": c, "diff": d, "lo": float(x["差的95%区间下限"]), "hi": float(x["差的95%区间上限"]), "p": p}

    fam = {}
    for s in splits:
        for r in rounds:
            hit = [x for x in rows if x["比较"] == f"拆开对合并：{s}" and x["A"] == f"mono {r}" and x["B"] == f"{s} {r}"]
            if len(hit) != 1:
                raise SystemExit(f"accuracy-pairs.csv 里“拆开对合并：{s}”（mono {r} 对 {s} {r}）应有一行，实际 {len(hit)} 行")
            v = pair(hit[0])
            if v is None:
                raise SystemExit(f"{s} {r}：没有两边都判过的题，比不了")
            fam[(s, r)] = v
    adj = holm({k: v["p"] for k, v in fam.items()})
    for k in fam:
        fam[k]["p_holm"] = adj[k]
    detected = [k for k, p in adj.items() if p <= ALPHA]
    others = [{"比较": x["比较"], "A": x["A"], "B": x["B"], **(pair(x) or dict.fromkeys(PAIR_KEYS))} for x in rows
              if x["比较"].startswith("参照：") or "两轮合并" in x["比较"] or x["比较"].startswith("对 Direct：")]
    acc = {"complete": True, "family": len(fam), "detected": [f"{s} {r}" for s, r in detected],
           "rows": {f"{s} {r}": v for (s, r), v in fam.items()}, "others": others}
    if detected:
        acc_verdict = "有检出差别：" + "、".join(
            f"{name(s)} {r}（拆开 − Monolithic {fam[(s, r)]['diff'] * 100:+.1f} 个百分点，校正后 p {peq(adj[(s, r)])}）" for s, r in detected)
    else:
        acc_verdict = f"{len(fam)} 组校正后 p 都 > {ALPHA:g}，没有检出差别（这一族只含本目录的 {len(fam)} 组）"
else:
    acc_verdict = "判分没做完（judge\\accuracy.json 不在，或 complete 不是 true），不出正确率的结论"

# ---------------------------------------------------------------- 输出
fails = {c["cell"]: model_fail(c) for c in cells.values()}
L = [f"# {plan.get('plan') or OUT.name} 的结论表（docs/36 第 3 节的口径）", "",
     f"结果目录 `{OUT.name}`；计划 {plan.get('plan', '—')}（mode={plan.get('mode', '—')}，每格 {plan.get('count', '—')} 题）；{plan_note}；并发 {CONC}。",
     "只读结果算出：吞吐的主数是整波平均的两轮合并收益，正确率按拆开对 Monolithic 的单轮比较单独做 Holm。", "",
     "## 结论", "",
     f"| 拆法 | 吞吐收益：整波平均，两轮合并（主数） | 两点法，两轮合并 | 整波平均：{' / '.join(rounds)} | 正确率：单轮比较的差，百分点（Holm 校正后 p） |",
     "| --- | --- | --- | --- | --- |"]
for s in splits:
    g = G[s]
    if acc["complete"]:
        a = "；".join(f"{r} {fam[(s, r)]['diff'] * 100:+.1f}（{pfmt(fam[(s, r)]['p_holm'])}）" for r in rounds)
    else:
        a = "—"
    L.append(f"| {name(s)} | {pct(g['pooled']['phase'])} | {pct(g['pooled']['two'])} | {' / '.join(pct(g[r]['phase']) for r in rounds)} | {a} |")
L += ["", f"**正确率**：{acc_verdict}。", ""]
if acc["complete"]:
    refs = [x["diff"] for x in acc["others"] if x["比较"].startswith("参照：") and x["diff"] is not None]
    diffs = [v["diff"] for v in fam.values()]
    if refs:
        L += [f"**参照**：同一布局跑两次，正确率差 {min(refs) * 100:+.1f} 到 {max(refs) * 100:+.1f} 个百分点；"
              f"拆开对 Monolithic 的单轮差 {min(diffs) * 100:+.1f} 到 {max(diffs) * 100:+.1f} 个百分点。", ""]
if M:
    m = M["split-memctx"]["pooled"]
    L += [f"**合并对两个都隔离**（整波平均，两轮合并）：{pct(m['phase'])}；两点法 {pct(m['two'])}。", ""]
if D:
    L += [f"**Monolithic 相对 Direct 的吞吐变化**（整波平均，两轮合并；负数才是 Ditto 运行时的代价）：{pct(D['mono']['pooled']['phase'])}；"
          f"两点法 {pct(D['mono']['pooled']['two'])}。", ""]
if any(fails.values()):
    xs = [v for v in fails.values() if v is not None]
    L += [f"**吞吐只计成功的题**：过不了官方输出检查（模型输出不合格）的题不计入，各格 {min(xs)}–{max(xs)} 题，见下面每格的表。", ""]

L += ["## 每格", "",
      "| 格子 | 跑完 / 失败 | 吞吐：两点法 / 整波平均（题/秒） | 分段吞吐：第 20–199 / 199–349 / 349 题以后 | 每题主线程（毫秒） | 每题 CPU（毫秒） | 正确率 |",
      "| --- | --- | --- | --- | --- | --- | --- |"]
for (l, r) in run_order:
    c = cells[(l, r)]
    seg = " / ".join(fmt(num(c, k), ".2f") for k in SEGS)
    bad = f"（模型输出不合格 {fails[c['cell']]}）" if fails[c["cell"]] else ""
    ac = per_cell_acc.get(c["cell"])
    acc_cell = f"{float(ac['正确率']) * 100:.1f}%（{ac['判对']}/{ac['题数']}）" if ac else "—"
    L.append(f"| {name(l)} {r} | {count(c, 'tasks') - count(c, 'failed')} / {c['failed']}{bad} | {tp(l, r, TWO):.3f} / {tp(l, r, PHASE):.3f} | {seg} | "
             f"{fmt(num(c, 'orchestrator_busy_ms_per_task'), '.0f')} | {fmt(num(c, 'cpu_ms_per_task'), '.0f')} | {acc_cell} |")


def gain_table(title, results, base):
    out = ["", f"## {title}", "", "| 布局 | 轮 | 两点法 | 整波平均（两轮合并那一行是主数） |", "| --- | --- | --- | --- |"]
    for l, g in results.items():
        for r in [*rounds, "pooled"]:
            out.append(f"| {name(l)} 对 {name(base)} | {'两轮合并' if r == 'pooled' else r} | {pct(g[r]['two'])} | {pct(g[r]['phase'])} |")
    return out


L += gain_table("吞吐收益：拆开对 Monolithic", G, "mono")
if D:
    L += gain_table("吞吐收益：各布局对 Direct", D, "direct")
if M:
    L += gain_table("吞吐收益：合并对两个都隔离", M, "split-both")

L += ["", f"## 正确率：{acc.get('family', len(splits) * 2)} 组单轮比较单独做 Holm", ""]
if acc["complete"]:
    L += ["| 比较 | 共同题数（排除的题） | Monolithic | 拆开 | 只有 Monolithic 对 / 只有拆开对 | 差（拆开 − Monolithic，百分点，95% 区间） | 符号检验 p | Holm 校正后 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for (s, r), v in fam.items():
        L.append(f"| {name(s)} {r} | {v['n']}（{v['excluded']}） | {v['a'] * 100:.1f}% | {v['b'] * 100:.1f}% | {v['only_a']} / {v['only_b']} | "
                 f"{v['diff'] * 100:+.1f}（{v['lo'] * 100:+.1f} 到 {v['hi'] * 100:+.1f}） | {pfmt(v['p'])} | {pfmt(v['p_holm'])} |")
    L += ["", "参照和描述（不做校正）：同一布局跑两次、两轮合并（同一题算了两次）、对 Direct。", "",
          "| 比较 | A | B | 共同题数（排除的题） | 差 B − A，百分点（95% 区间） | 符号检验 p |", "| --- | --- | --- | --- | --- | --- |"]
    for x in acc["others"]:
        if x["p"] is not None:
            L.append(f"| {label(x['比较'])} | {label(x['A'])} | {label(x['B'])} | {x['n']}（{x['excluded']}） | "
                     f"{x['diff'] * 100:+.1f}（{x['lo'] * 100:+.1f} 到 {x['hi'] * 100:+.1f}） | {pfmt(x['p'])} |")
        else:
            L.append(f"| {label(x['比较'])} | {label(x['A'])} | {label(x['B'])} | — | —（没有两边都判过的题） | — |")
    L += ["", "没检出差别，不等于没有差别。report.md 里“Holm 校正后 p”那一列是全部比较一起校正的，不是这里的口径。"]
else:
    L.append(acc_verdict)
md = "\n".join(L) + "\n"
per_cell = {c["cell"]: {"layout": l, "round": r, "tasks": count(c, "tasks"), "failed": count(c, "failed"), "failed_model_output": fails[c["cell"]],
                        "throughput_two": tp(l, r, TWO), "throughput_phase": tp(l, r, PHASE), "segments": [num(c, k) for k in SEGS],
                        "busy_ms_per_task": num(c, "orchestrator_busy_ms_per_task"), "cpu_ms_per_task": num(c, "cpu_ms_per_task"),
                        "accuracy": float(per_cell_acc[c["cell"]]["正确率"]) if c["cell"] in per_cell_acc else None}
            for (l, r), c in ((k, cells[k]) for k in run_order)}
summary = {"out": str(OUT), "plan": {k: plan.get(k) for k in ("plan", "mode", "count", "conc", "formal")}, "plan_check": plan_note,
           "concurrency": CONC, "layouts": layouts, "rounds": rounds,
           "main": "gains_vs_mono 里各拆法的 pooled.phase：整波平均的两轮合并收益",
           "gains_vs_mono": G, "gains_vs_direct": D, "memctx_vs_both": M, "cells": per_cell,
           "accuracy_verdict": acc_verdict, "accuracy": acc}
(OUT / "结论.md").write_text(md, encoding="utf-8")
(OUT / "结论.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(md, end="")
