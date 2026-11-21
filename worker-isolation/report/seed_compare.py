"""
种子对照（docs\\32 第 4 节）：rag 那一轮（results\\rag-real-a，原种子 20261119 的前 200 题）和
用种子 42 的前 200 题原样再跑的一轮（results\\rag-real-a-s42）放在一起，按跑之前写定的两条判据下结论。

    python -I report/seed_compare.py [新结果目录] [原结果目录]

默认新 = results\\rag-real-a-s42，原 = results\\rag-real-a。只读两边的 cells.csv、judge\\judged.csv、judge\\accuracy-pairs.csv，
写 <新结果目录>\\种子对照.md、种子对照.json，并把 Markdown 打印出来。

判据（docs\\32 4.4 节，跑之前写定）：
  1. 吞吐：四组（并发 16、37 × 两轮）都是拆开更快；每档两轮合并的收益（拆开两轮吞吐之和 ÷ Monolithic 两轮之和 − 1）
     和原来相差不超过 10 个百分点。
  2. 正确率：拆开对 Monolithic 的四组配对比较（judge.py 算的符号检验 p）一起做 Holm 校正，校正后都大于 0.05（没有检出差别）。
其余各节只作描述。
"""
import csv
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")   # 报错（SystemExit）走 stderr
PROBE = Path(__file__).resolve().parents[1]
SEC = PROBE.parent
NEW = Path(sys.argv[1]) if len(sys.argv) > 1 else SEC / "results" / "rag-real-a-s42"
OLD = Path(sys.argv[2]) if len(sys.argv) > 2 else SEC / "results" / "rag-real-a"
if NEW.resolve() == OLD.resolve():
    raise SystemExit("新、原是同一个目录。用法：python -I report/seed_compare.py [新结果目录] [原结果目录]")
CONCS, ROUNDS, SPLIT = ("16", "37"), ("r1", "r2"), "split-interaction"
LAYOUTS = (("mono", "Monolithic"), (SPLIT, "拆开"))
GAIN_TOL, ALPHA = 0.10, 0.05


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load(root):
    """cells[(并发, 布局, 轮)] = cells.csv 的一行；judged[格子名][题号] = judged.csv 的一行；pairs = accuracy-pairs.csv。"""
    # 判分没做完（到上限、中转拒绝、被停下）时 judge.py 照样写表，没判成的题在配对比较里被排除：不下结论
    done = json.loads((root / "judge" / "accuracy.json").read_text(encoding="utf-8")).get("complete")
    if done is not True:
        raise SystemExit(f"{root} 的判分没做完（judge\\accuracy.json 里 complete 不是 true）：先补完判分，再出对照表")
    cells = {}
    for r in read_csv(root / "cells.csv"):
        key = (r["concurrency"], r["layout"], r["round"].split("-")[-1])
        if key in cells:
            raise SystemExit(f"{root} 的 cells.csv 里 {key} 出现两次")
        cells[key] = r
    judged = {}
    for r in read_csv(root / "judge" / "judged.csv"):
        judged.setdefault(r["格子"], {})[r["题号"]] = r
    missing = [k for k in ((c, lay, rd) for c in CONCS for lay, _ in LAYOUTS for rd in ROUNDS) if k not in cells or cells[k]["cell"] not in judged]
    if missing:
        raise SystemExit(f"{root} 缺格子或缺判分：{missing}")
    return cells, judged, read_csv(root / "judge" / "accuracy-pairs.csv")


def qtype(row):
    return row["题型"] + ("（无法回答）" if row["题号"].endswith("_abs") else "")


def right(row):
    return row["判对"] == "True"


def gains(cells):
    """拆开对 Monolithic 的吞吐收益：每轮各一个，另加两轮合并（吞吐之和的比）。"""
    tp = lambda c, lay, rd: float(cells[(c, lay, rd)]["throughput_per_s"])
    return {c: {"rounds": {rd: tp(c, SPLIT, rd) / tp(c, "mono", rd) - 1 for rd in ROUNDS},
                "pooled": sum(tp(c, SPLIT, rd) for rd in ROUNDS) / sum(tp(c, "mono", rd) for rd in ROUNDS) - 1} for c in CONCS}


def acc_pairs(pairs):
    """accuracy-pairs.csv 里拆开对 Monolithic 的四组（同一轮、同一并发；不要“两轮合并”的行）。"""
    out = {}
    for r in pairs:
        a, b = r["A"], r["B"]
        if "+" in a or not (a.startswith("mono ") and b.startswith(SPLIT + " ")) or a.split(" ", 1)[1] != b.split(" ", 1)[1]:
            continue
        label = a.split(" ", 1)[1]
        if r["符号检验p"] in ("", None):
            raise SystemExit(f"{label} 没有两边都判过的题，比不了")
        out[label] = {"n": int(r["共同题数"]), "excluded": int(r["排除的题（没跑完或没判成）"]), "mono": float(r["A正确率"]),
                      "split": float(r["B正确率"]), "only_mono": int(r["只有A对"]), "only_split": int(r["只有B对"]),
                      "diff": float(r["正确率差B减A"]), "lo": float(r["差的95%区间下限"]), "hi": float(r["差的95%区间上限"]),
                      "p": float(r["符号检验p"])}
    want = [f"c{c}-{rd}" for c in CONCS for rd in ROUNDS]
    if sorted(out) != sorted(want):
        raise SystemExit(f"accuracy-pairs.csv 里拆开对 Monolithic 的比较应是 {want}，实际 {sorted(out)}")
    return out


def holm(ps):
    """Holm 校正：p 从小到大第 i 个（从 0 数）乘 (m − i)，再取到它为止的最大值，不超过 1。"""
    m, running, adj = len(ps), 0.0, {}
    for i, (k, p) in enumerate(sorted(ps.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def cell_acc(cells, judged, key, ids=None):
    rows = judged[cells[key]["cell"]]
    picked = [rows[i] for i in rows if ids is None or i in ids]
    return sum(map(right, picked)) / len(picked) if picked else None


pct = lambda x: "—" if x is None else f"{x * 100:.1f}%"
pp = lambda x: f"{x * 100:+.1f}"

old_cells, old_judged, old_pairs = load(OLD)
new_cells, new_judged, new_pairs = load(NEW)
# 两个参数写反、或者拿同一批题来比，都会给出“差全为 0、判据满足”的假结论：先拦下
if not all("-s42-" in r["cell"] for r in new_cells.values()) or any("-s42-" in r["cell"] for r in old_cells.values()):
    raise SystemExit("新结果应是种子 42 的格子（格子名带 -s42-），原结果不应是：检查两个参数是不是写反了")
og, ng = gains(old_cells), gains(new_cells)
oa, na = acc_pairs(old_pairs), acc_pairs(new_pairs)
o_holm, n_holm = holm({k: v["p"] for k, v in oa.items()}), holm({k: v["p"] for k, v in na.items()})

# 判据 1、2
all_faster = all(ng[c]["rounds"][rd] > 0 for c in CONCS for rd in ROUNDS)
pooled_close = {c: abs(ng[c]["pooled"] - og[c]["pooled"]) <= GAIN_TOL for c in CONCS}
c1 = all_faster and all(pooled_close.values())
c2 = all(p > ALPHA for p in n_holm.values())

# 题和题型
ids_of = lambda judged: set().union(*(set(rows) for rows in judged.values()))
old_ids, new_ids = ids_of(old_judged), ids_of(new_judged)
shared = old_ids & new_ids
if old_ids == new_ids:
    raise SystemExit("新、原两边的题完全相同：不是两个种子的对照（参数给错了？）")
if not shared:
    raise SystemExit("新、原两边没有共同的题：第 5 节算不了（两个种子的前 200 题应有 77 题相同）")
types_old, types_new = {}, {}
for judged, types in ((old_judged, types_old), (new_judged, types_new)):
    for rows in judged.values():
        for r in rows.values():
            t = types.setdefault(qtype(r), {"ids": set(), "right": 0, "n": 0})
            t["ids"].add(r["题号"]); t["right"] += right(r); t["n"] += 1
overall_old = sum(t["right"] for t in types_old.values()) / sum(t["n"] for t in types_old.values())
rate_old = {k: t["right"] / t["n"] for k, t in types_old.items()}
new_type_of = {i: k for k, t in types_new.items() for i in t["ids"]}
expected = sum(rate_old.get(new_type_of[i], overall_old) for i in new_ids) / len(new_ids)
check_old = sum(rate_old[k] * len(t["ids"]) for k, t in types_old.items()) / len(old_ids)
mean_acc = lambda cells, judged: sum(cell_acc(cells, judged, (c, lay, rd)) for c in CONCS for lay, _ in LAYOUTS for rd in ROUNDS) / 8

# 两边都有的题
shared_rows, between, within_old, within_new = [], [], [], []
for c in CONCS:
    for lay, name in LAYOUTS:
        o = {rd: cell_acc(old_cells, old_judged, (c, lay, rd), shared) for rd in ROUNDS}
        n = {rd: cell_acc(new_cells, new_judged, (c, lay, rd), shared) for rd in ROUNDS}
        shared_rows.append((c, name, o, n))
        between += [n[rd] - o[rd] for rd in ROUNDS]
        within_old.append(abs(o["r1"] - o["r2"])); within_new.append(abs(n["r1"] - n["r2"]))
avg = lambda xs: sum(xs) / len(xs)

out = []
w = out.append
w("# 种子对照：rag-qa 切法 (a)，原种子 20261119 对种子 42\n")
w(f"原 = `{OLD.name}`（原种子 20261119 的前 200 题，11-20 凌晨）；新 = `{NEW.name}`（种子 42 的前 200 题）。"
  "两边设定相同：2 核、8 格（并发 16、37 各一组 Monolithic、拆开、拆开、Monolithic）、每格 200 题、热身 20。判据是跑之前写在 docs\\32 4.4 节的。\n")
w("## 结论\n")
w(f"- 判据 1（吞吐）：{'满足' if c1 else '**不满足**'}。新的四组{'都是拆开更快' if all_faster else '**不是都拆开更快**'}；"
  + "；".join(f"并发 {c} 两轮合并的收益 原 {pp(og[c]['pooled'])}% → 新 {pp(ng[c]['pooled'])}%，差 {pp(ng[c]['pooled'] - og[c]['pooled'])} 个百分点"
              f"（{'在' if pooled_close[c] else '**超出**'} ±{GAIN_TOL * 100:.0f} 以内）" for c in CONCS) + "。")
w(f"- 判据 2（正确率）：{'满足' if c2 else '**不满足**'}。拆开对 Monolithic 的四组，Holm 校正后的 p 为 "
  + "、".join(f"{k} {n_holm[k]:.3f}" for k in sorted(n_holm)) + f"，{'都大于' if c2 else '**有的不大于**'} {ALPHA}。")
w("- " + ("两项都满足：换种子不改变这一轮的结论，之后用种子 42（docs\\32）。" if c1 and c2 else
          "**有判据不满足：停下来，先做 docs\\32 4.5 节推迟的那项核对（新程序在原种子数据上的第一步请求是否和 11-20 逐字相同），再分析原因。**") + "\n")

w("## 1. 每格\n")
w("| 并发 | 布局 | 轮 | 吞吐（题/秒）原 → 新 | 每题用时 p50 / p95（秒）原 → 新 | 失败（其中模型输出不合格）原 → 新 | 正确率 原 → 新 |")
w("| --- | --- | --- | --- | --- | --- | --- |")
for c in CONCS:
    for lay, name in LAYOUTS:
        for rd in ROUNDS:
            o, n = old_cells[(c, lay, rd)], new_cells[(c, lay, rd)]
            lat = lambda r: f"{float(r['latency_p50_ms']) / 1000:.1f} / {float(r['latency_p95_ms']) / 1000:.1f}"
            fail = lambda r: f"{r['failed']}（{r.get('failed_model_output') or 0}）"
            w(f"| {c} | {name} | {rd} | {float(o['throughput_per_s']):.3f} → {float(n['throughput_per_s']):.3f} | {lat(o)} → {lat(n)} | "
              f"{fail(o)} → {fail(n)} | {pct(cell_acc(old_cells, old_judged, (c, lay, rd)))} → {pct(cell_acc(new_cells, new_judged, (c, lay, rd)))} |")
w("\n吞吐的绝对值和原来比，会受日期、机器状态、DeepSeek 快慢影响（docs\\20 第 3.4 节），只作描述；下结论看同一轮里比出来的收益。\n")

w("## 2. 判据 1：拆开对 Monolithic 的吞吐收益\n")
w("| 并发 | 原 r1 | 原 r2 | 原两轮合并 | 新 r1 | 新 r2 | 新两轮合并 | 合并的差（新 − 原，百分点） |")
w("| --- | --- | --- | --- | --- | --- | --- | --- |")
for c in CONCS:
    w(f"| {c} | {pp(og[c]['rounds']['r1'])}% | {pp(og[c]['rounds']['r2'])}% | {pp(og[c]['pooled'])}% | {pp(ng[c]['rounds']['r1'])}% | "
      f"{pp(ng[c]['rounds']['r2'])}% | {pp(ng[c]['pooled'])}% | {pp(ng[c]['pooled'] - og[c]['pooled'])} |")
w("\n两轮合并 = 拆开两轮吞吐之和 ÷ Monolithic 两轮之和 − 1。\n")

w("## 3. 判据 2：拆开对 Monolithic 的正确率（同一题配对）\n")
w("| 种子 | 并发-轮 | 共同题数（排除） | Monolithic | 拆开 | 只有 Monolithic 对 / 只有拆开对 | 差（拆开 − Monolithic，百分点，95% 区间） | 符号检验 p | Holm 校正后的 p（四组一起） |")
w("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
for seed, pairs, adj in (("原", oa, o_holm), ("新", na, n_holm)):
    for k in sorted(pairs):
        v = pairs[k]
        w(f"| {seed} | {k} | {v['n']}（{v['excluded']}） | {pct(v['mono'])} | {pct(v['split'])} | {v['only_mono']} / {v['only_split']} | "
          f"{pp(v['diff'])}（{pp(v['lo'])} 到 {pp(v['hi'])}） | {v['p']:.3f} | {adj[k]:.3f} |")
w("\np > 0.05 只说明没有检出差别，不等于证明相同。区间是 Agresti–Min 配对区间（judge.py）。"
  "这里的 Holm 只校正这四组；report.md、accuracy-pairs.csv 里的 Holm 是 judge.py 把表里全部比较一起校正的，数值不同，判据以这里为准。\n")

w("## 4. 正确率水平和题型（只作描述）\n")
w(f"- 按原结果的分题型正确率（原 8 格合计）折算，新这 200 题的预期正确率是 {pct(expected)}（同样折算原来的 200 题是 {pct(check_old)}）。")
w(f"- 实际：新 8 格平均 {pct(mean_acc(new_cells, new_judged))}，原 8 格平均 {pct(mean_acc(old_cells, old_judged))}。\n")
w("| 题型 | 原：题数 | 原：正确率（8 格合计） | 新：题数 | 新：正确率（8 格合计） |")
w("| --- | --- | --- | --- | --- |")
for k in sorted(set(types_old) | set(types_new)):
    o, n = types_old.get(k), types_new.get(k)
    w(f"| {k} | {len(o['ids']) if o else 0} | {pct(o['right'] / o['n']) if o else '—'} | {len(n['ids']) if n else 0} | {pct(n['right'] / n['n']) if n else '—'} |")
w("")

w(f"## 5. 两边都有的 {len(shared)} 题（只作描述）\n")
w("同一批题，不受题型比例影响。\n")
w("| 并发 | 布局 | 原 r1 | 原 r2 | 新 r1 | 新 r2 |")
w("| --- | --- | --- | --- | --- | --- |")
for c, name, o, n in shared_rows:
    w(f"| {c} | {name} | {pct(o['r1'])} | {pct(o['r2'])} | {pct(n['r1'])} | {pct(n['r2'])} |")
w(f"\n同一设定（并发、布局、轮都相同）新减原：平均 {pp(avg(between))} 个百分点，绝对值平均 {avg([abs(x) for x in between]) * 100:.1f}。"
  f"同一种子两轮之间（并发、布局相同）差的绝对值平均：原 {avg(within_old) * 100:.1f}、新 {avg(within_new) * 100:.1f} 个百分点。\n")

w("## 6. 其他\n")
w("- 各布局发给模型的请求是否逐字相同、每格的模型请求数和费用：见两边的 report.md。")
w("- 新这一轮另记了每次调用的用时（calls.json.gz）和 rag 每题的完整报告（reports.jsonl.gz），原来那一轮没有。")

md = "\n".join(out) + "\n"
(NEW / "种子对照.md").write_text(md, encoding="utf-8")
summary = {
    "old": str(OLD), "new": str(NEW), "criterion1": {"met": c1, "all_rounds_split_faster": all_faster, "pooled_within_tolerance": pooled_close,
                                                    "tolerance": GAIN_TOL, "gains_old": og, "gains_new": ng},
    "criterion2": {"met": c2, "alpha": ALPHA, "holm_new": n_holm, "holm_old": o_holm, "pairs_new": na, "pairs_old": oa},
    "verdict_seed_does_not_change_conclusions": c1 and c2,
    "accuracy": {"expected_new_from_old_types": expected, "old_type_weighted_check": check_old,
                 "mean_new": mean_acc(new_cells, new_judged), "mean_old": mean_acc(old_cells, old_judged)},
    "shared_questions": len(shared), "shared_between_mean": avg(between), "shared_within_old_mean_abs": avg(within_old),
    "shared_within_new_mean_abs": avg(within_new),
}
(NEW / "种子对照.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print(md)
