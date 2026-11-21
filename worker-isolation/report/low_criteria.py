"""
真实模型低并发组（docs\\35）：按跑之前写定的判据（docs\\35 第 3 节）下结论。只读结果，不改数据。

    python -I report/low_criteria.py [结果目录]

默认 results\\s4-real-low-s42。读 run-info.json、cells.csv、pairs.csv、raw\\*.json、judge\\accuracy.json、judge\\accuracy-pairs.csv，
写 <结果目录>\\判据.md、判据.json，并把 Markdown 打印出来。

判据（docs\\35 第 3 节，跑之前写定）：
  3.1 负载：Monolithic 两格编排器主线程忙的比例（稳态窗口里忙的时间 ÷ 窗口时长）取平均，低于 70%。
  3.2 吞吐：各种拆法对 Monolithic 的两轮合并收益（整波平均，两轮吞吐之和的比），都不超过 +5%。
      主线程忙 < 70% 而且都 ≤ +5%：成立；忙 < 70% 而有一种 > +5%：不成立，照实报，不重跑、不改界；
      忙 ≥ 70%：半负载，收益照报，不下“负载低”的结论。变慢的照实报，算作没有收益。
      两点法照列；两种算法对 +5% 的结论不一致的，标出来。
  3.3 配套检查（DeepSeek 时快时慢）：稳态窗口里成功做完的题，每题模型用时 m = 检索词、回答两步在出口花的时间之和
      （出口记的 total_ms，含出口的重试；同一步发了第二次的，出口记成 answer#2 这样，也算进这一步），
      非模型用时 R = 每题用时 − m。扣除模型快慢以后的收益 = (Monolithic 的 R + M) ÷ (拆开的 R + M) − 1；
      M 是这组比较里各格平均 m 的平均（单轮两格，两轮合并四格；两轮合并时 R 取两轮平均）。
      它和整波平均对 +5% 的结论不一致的、两轮收益相差超过 5 个百分点的，标出来；Monolithic 对 Direct 的两轮合并差当噪声参照。
  3.4 正确率：judge\\accuracy-pairs.csv 里拆开对 Monolithic 的单轮比较（每种拆法 × 每轮），单独做 Holm 校正，校正后都 > 0.05 算成立。
      p 用表里“只有A对”“只有B对”两个整数按 judge.py 的符号检验重算（表里的 p 只留 4 位小数，压在 0.05 上时会判反），
      并和表里的 p 核对。同时列 95% 区间和“同一布局跑两次”的参照。
      report.md 里“Holm 校正后 p”那一列是表里全部比较一起校正的，不是判据。
吞吐用的是 cells.csv 里留 4 位小数的数，收益和不舍入的能差约 0.02 个百分点：离界不到 0.05 个百分点时另外标出来。
"""
import csv
import json
import sys
from math import comb
from pathlib import Path
from statistics import fmean

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")   # 报错（SystemExit）走 stderr
PROBE = Path(__file__).resolve().parents[1]
SEC = PROBE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else SEC / "results" / "s4-real-low-s42"
BUSY_MAX, GAIN_MAX, ROUND_GAP, ALPHA = 0.70, 0.05, 0.05, 0.05
BUSY_TXT, GAIN_TXT = f"{BUSY_MAX * 100:g}%", f"{GAIN_MAX * 100:+g}%"   # 结论里写的界：70%、+5%
NEAR = 0.0005   # 离界不到 0.05 个百分点算“贴着界”
TWO, PHASE = "throughput_per_s", "throughput_phase_avg"
NAME = {"mono": "Monolithic", "direct": "Direct", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT",
        "split-both": "两个都隔离", "split-memctx": "合并（MEMORY 和 CONTEXT 合进一个进程）"}
STEPS = ("keyword", "answer")   # S4 每题两步模型请求：检索词、回答


def name(layout):
    return NAME.get(layout, layout)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def holm(ps):
    """Holm 校正：p 从小到大第 i 个（从 0 数）乘 (m − i)，再取到它为止的最大值，不超过 1。"""
    m, running, adj = len(ps), 0.0, {}
    for i, (k, p) in enumerate(sorted(ps.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def sign_test(b, c):
    """双侧符号检验，和 judge.py 的 sign_test 相同：只有一方答对的题里，两边各占几题。"""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def pct(x):
    return "—" if x is None else f"{x * 100:+.2f}%"


def share(x):
    return f"{x * 100:.1f}%"


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
# 计划：整种布局缺了、多了，只有和计划比才查得出来，所以 run-info.json 必须在
if not (OUT / "run-info.json").exists():
    raise SystemExit(f"{OUT} 里没有 run-info.json：没法和计划核对格子，不下结论")
plan = json.loads((OUT / "run-info.json").read_text(encoding="utf-8"))["plan"]
want = sorted(tuple(s.split(":")[:2]) for s in plan["formal"].split())
if sorted(cells) != want:
    raise SystemExit(f"cells.csv 的格子和 run-info.json 的计划对不上：{sorted(cells)} 对 {want}")
if plan.get("conc") not in (None, "", CONC):
    raise SystemExit(f"run-info.json 记的并发是 {plan['conc']}，cells.csv 里是 {CONC}")
plan_note = f"格子和 run-info.json 的计划一致（{len(want)} 格）"


def tp(layout, rnd, key):
    v = cells[(layout, rnd)][key]
    if v in ("", None):
        raise SystemExit(f"{cells[(layout, rnd)]['cell']} 的 {key} 是空的（窗口无效？）")
    return float(v)


# ---------------------------------------------------------------- 每格：窗口里每题的用时、模型用时（raw\*.json）
def window_stats(cell):
    """稳态窗口里成功做完的题（和 real_detail.py 的 A 节相同）：每题用时 T、模型用时 m、非模型用时 R 的平均。"""
    p = OUT / "raw" / f"{cell}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    w = d.get("window")
    if not w:
        raise SystemExit(f"{cell} 的窗口无效（raw 里没有 window）")
    rows = [x for x in d["fake"]["rows"] if x.get("cell", p.stem) == p.stem]   # 上一格晚到的请求不算
    # 同一格、同一题、同一步的第二次请求，出口记成 answer#2 这样（egress.ts）：按“#”前面的步骤名归到同一步
    per = {}
    for x in rows:
        per.setdefault((x["task"], x["kind"].split("#")[0]), []).append(x)
    inw = [t for t in d["tasks"] if t["ok"] and w["from_ms"] <= t["end_ms"] <= w["to_ms"]]
    if not inw:
        raise SystemExit(f"{cell} 的窗口里没有做完的题")
    T, M, dup = [], [], 0
    for t in inw:
        k = f"{t['i']}:{t['id']}"
        got = {s: per.get((k, s)) for s in STEPS}
        lack = [s for s, g in got.items() if not g]
        if lack:
            raise SystemExit(f"{cell} 第 {k} 题在出口记录里缺 {lack}")
        if any(x.get("total_ms") is None for g in got.values() for x in g):
            raise SystemExit(f"{cell} 第 {k} 题的出口记录没有 total_ms")
        dup += sum(len(g) - 1 for g in got.values())   # 同一步发了不止一次：几次的用时都算进 m
        T.append(t["end_ms"] - t["start_ms"])
        M.append(sum(x["total_ms"] for g in got.values() for x in g))
    return {"cell": cell, "n": len(inw), "T_ms": fmean(T), "m_ms": fmean(M), "R_ms": fmean(T) - fmean(M),
            "busy": w["orchestrator"]["main_busy_share"], "egress_retried": sum(1 for x in rows if (x.get("attempts") or 1) > 1),
            "steps_sent_twice": dup, "other_kinds": sorted({x["kind"].split("#")[0] for x in rows} - set(STEPS))}


st = {key: window_stats(r["cell"]) for key, r in cells.items()}
for key, s in st.items():   # cells.csv 和 raw 是同一批结果
    if abs(s["busy"] - float(cells[key]["orchestrator_main_busy_share"])) > 1e-3:
        raise SystemExit(f"{s['cell']}：raw 里的主线程忙比例和 cells.csv 对不上")
    if str(s["n"]) != cells[key]["steady_completed"]:
        raise SystemExit(f"{s['cell']}：窗口里的题 {s['n']} 和 cells.csv 的 steady_completed {cells[key]['steady_completed']} 对不上")


def gain(base, layout, key, rs):
    """layout 对 base 的吞吐收益：rs 里各轮吞吐之和的比 − 1（单轮就是那一轮的比）。"""
    return sum(tp(layout, r, key) for r in rs) / sum(tp(base, r, key) for r in rs) - 1


def adj_gain(base, layout, rs):
    """扣除模型快慢以后的收益：两边换成同一个模型用时 M。"""
    m = fmean(st[(l, r)]["m_ms"] for l in (base, layout) for r in rs)
    rb, rl = fmean(st[(base, r)]["R_ms"] for r in rs), fmean(st[(layout, r)]["R_ms"] for r in rs)
    return (rb + m) / (rl + m) - 1


def gains(base, layout):
    out = {r: {"two": gain(base, layout, TWO, [r]), "phase": gain(base, layout, PHASE, [r]), "adjusted": adj_gain(base, layout, [r])}
           for r in rounds}
    out["pooled"] = {"two": gain(base, layout, TWO, rounds), "phase": gain(base, layout, PHASE, rounds), "adjusted": adj_gain(base, layout, rounds)}
    return out


G = {s: gains("mono", s) for s in splits}
# 和 export.py 写的 pairs.csv 对一遍：每种拆法两轮加两轮合并、两点法和整波平均两列，一个都不能少。
# 表里的吞吐只留 4 位小数，差在 5e-4 以内算一致。
matched = 0
for r in read_csv(OUT / "pairs.csv"):
    s = r["layout"]
    if s not in G:
        continue
    rnd = "pooled" if r["round"].startswith("两轮合并") else r["round"]
    if rnd not in G[s]:
        raise SystemExit(f"pairs.csv 里有不认识的轮：{s} {r['round']}")
    for key, col in (("two", "throughput_gain"), ("phase", "throughput_gain_phase")):
        if r.get(col) in ("", None):
            raise SystemExit(f"pairs.csv 里 {s} {r['round']} 的 {col} 是空的")
        if abs(G[s][rnd][key] - float(r[col])) > 5e-4:
            raise SystemExit(f"{s} {r['round']} 的 {col}：这里算出 {G[s][rnd][key]:.4f}，pairs.csv 是 {r[col]}")
        matched += 1
if matched != 6 * len(splits):
    raise SystemExit(f"pairs.csv 里对上的只有 {matched} 个数，应是 {6 * len(splits)} 个（每种拆法两轮加两轮合并、两列）")
D = gains("direct", "mono") if "direct" in layouts else None   # Monolithic 相对 Direct：噪声参照

# ---------------------------------------------------------------- 3.1、3.2、3.3
busy = fmean(st[("mono", r)]["busy"] for r in rounds)
low = busy < BUSY_MAX
over = {s: G[s]["pooled"]["phase"] for s in splits if G[s]["pooled"]["phase"] > GAIN_MAX}
if not low:
    verdict = f"半负载：Monolithic 主线程忙 {share(busy)}，不低于 {BUSY_TXT}。收益照报，不下“负载低”的结论"
elif not over:
    listed = "、".join(f"{name(s)} {pct(G[s]['pooled']['phase'])}" for s in splits)
    verdict = f"成立：负载低时拆开没有收益（{listed}，都不超过 {GAIN_TXT}）"
else:
    verdict = "不成立：" + "；".join(f"并发 {CONC} 时，{name(s)} 已有 {pct(g)} 的收益" for s, g in over.items()) + "（不重跑，不改界）"
inside = lambda g: g <= GAIN_MAX
flags = {"3.1": [], "3.2": [], "3.3": []}
if abs(busy - BUSY_MAX) < NEAR:
    flags["3.1"].append(f"主线程忙 {busy * 100:.3f}%，贴着 {BUSY_TXT} 的界")
disagree = False   # 有没有“对 +5% 的结论不一致”
for s in splits:
    p = G[s]["pooled"]
    if p["phase"] < 0:
        flags["3.2"].append(f"{name(s)}：拆开以后慢了 {-p['phase'] * 100:.2f}%，算作没有收益")
    if abs(p["phase"] - GAIN_MAX) < NEAR:
        flags["3.2"].append(f"{name(s)}：两轮合并 {p['phase'] * 100:+.3f}%，贴着 {GAIN_TXT} 的界（吞吐表里只有 4 位小数，结论可能随舍入变）")
    if inside(p["two"]) != inside(p["phase"]):
        disagree = True
        flags["3.2"].append(f"{name(s)}：两点法 {pct(p['two'])}、整波平均 {pct(p['phase'])}，对 {GAIN_TXT} 的结论不一致，要人来看")
    if inside(p["adjusted"]) != inside(p["phase"]):
        disagree = True
        flags["3.3"].append(f"{name(s)}：扣除模型快慢以后 {pct(p['adjusted'])}、整波平均 {pct(p['phase'])}，对 {GAIN_TXT} 的结论不一致，要人来看")
    r0, r1 = (G[s][r]["phase"] for r in rounds)
    if abs(r0 - r1) > ROUND_GAP:
        flags["3.3"].append(f"{name(s)}：两轮不一致，{rounds[0]} {pct(r0)}、{rounds[1]} {pct(r1)}，相差 {abs(r0 - r1) * 100:.2f} 个百分点"
                            "（结论仍按两轮合并）")
if disagree:
    verdict += f"；有对 {GAIN_TXT} 的结论不一致的，见下面“要标出的”"
adjusted_text = "、".join(f"{name(s)} {pct(G[s]['pooled']['adjusted'])}" for s in splits)
noise_text = (f"噪声参照（Monolithic 相对 Direct，两轮合并）：整波平均 {pct(D['pooled']['phase'])}，扣除模型快慢以后 {pct(D['pooled']['adjusted'])}"
              if D else "没有 Direct，没有噪声参照")
check_text = f"扣除模型快慢以后，两轮合并：{adjusted_text}。{noise_text}。" + ("见下面“要标出的”" if flags["3.3"] else "没有要标出的")
odd = [f"{s['cell']}：出口重试 {s['egress_retried']} 次、同一步发了不止一次 {s['steps_sent_twice']} 次、别的请求种类 {s['other_kinds']}"
       for s in st.values() if s["egress_retried"] or s["steps_sent_twice"] or s["other_kinds"]]

# ---------------------------------------------------------------- 3.4 正确率
acc = {"complete": False}
acc_file = OUT / "judge" / "accuracy.json"
if acc_file.exists() and json.loads(acc_file.read_text(encoding="utf-8")).get("complete") is True:
    rows = read_csv(OUT / "judge" / "accuracy-pairs.csv")

    def pair(x):
        """一行比较：p 按两个整数重算，并和表里的（留 4 位小数）核对。"""
        if x["符号检验p"] in ("", None):
            return None
        b, c = int(x["只有A对"]), int(x["只有B对"])
        p = sign_test(b, c)
        if abs(p - float(x["符号检验p"])) > 6e-5:
            raise SystemExit(f"{x['比较']}（{x['A']} 对 {x['B']}）：按 {b}/{c} 重算的 p 是 {p:.6f}，表里是 {x['符号检验p']}")
        return {"n": int(x["共同题数"]), "excluded": int(x["排除的题（没跑完或没判成）"]), "mono": float(x["A正确率"]),
                "split": float(x["B正确率"]), "only_mono": b, "only_split": c, "diff": float(x["正确率差B减A"]),
                "lo": float(x["差的95%区间下限"]), "hi": float(x["差的95%区间上限"]), "p": p}

    fam = {}
    for s in splits:
        for r in rounds:
            hit = [x for x in rows if x["比较"] == f"拆开对合并：{s}" and x["A"] == f"mono {r}" and x["B"] == f"{s} {r}"]
            if len(hit) != 1:
                raise SystemExit(f"accuracy-pairs.csv 里“拆开对合并：{s}”（mono {r} 对 {s} {r}）应有一行，实际 {len(hit)} 行")
            v = pair(hit[0])
            if v is None:
                raise SystemExit(f"{s} {r}：没有两边都判过的题，比不了")
            fam[f"{s} {r}"] = v
    adj = holm({k: v["p"] for k, v in fam.items()})
    for k in fam:
        fam[k]["p_holm"] = adj[k]
    detected = [k for k, p in adj.items() if p <= ALPHA]
    refs = [{"比较": x["比较"], "A": x["A"], "B": x["B"], **(pair(x) or {})} for x in rows if x["比较"].startswith("参照：")]
    acc = {"complete": True, "family": len(fam), "detected": detected, "rows": fam, "refs": refs}
    if detected:
        acc_verdict = "有检出差别：" + "、".join(f"{k}（校正后 p = {adj[k]:.3f}，差 {fam[k]['diff'] * 100:+.1f} 个百分点）" for k in detected)
    else:
        acc_verdict = f"成立：{len(fam)} 组校正后 p 都 > {ALPHA:g}，没有检出差别"
else:
    acc_verdict = "判分没做完（judge\\accuracy.json 不在，或 complete 不是 true），不下结论"

# ---------------------------------------------------------------- 输出
busy_rounds = "、".join(f"{r} {share(st[('mono', r)]['busy'])}" for r in rounds)
L = ["# 低并发组的判据（docs/35 第 3 节）", "",
     f"结果目录 `{OUT.name}`；{plan_note}；并发 {CONC}。只读结果算出，主数是整波平均的两轮合并收益。", "",
     "## 结论", "", "| 判据 | 结果 |", "| --- | --- |",
     f"| 3.1 负载 | Monolithic 主线程忙 {share(busy)}（{busy_rounds}）：{'低于' if low else '不低于'} {BUSY_TXT} |",
     f"| 3.2 吞吐 | {verdict} |",
     f"| 3.3 配套检查 | {check_text} |",
     f"| 3.4 正确率 | {acc_verdict} |", ""]
PART = {"3.1": "3.1 负载", "3.2": "3.2 吞吐", "3.3": "3.3 配套检查"}
if any(flags.values()):
    L += ["**要标出的**：", ""]
    for part, xs in flags.items():
        if xs:
            L += [f"- {PART[part]}："] + [f"  - {x}" for x in xs]
    L.append("")
L += ["## 每格（稳态窗口里成功做完的题）", "",
      "| 格子 | 主线程忙 | 吞吐：两点法 / 整波平均（题/秒） | 窗口里的题 | 每题用时（毫秒） | 模型用时 m | 非模型用时 R | m 占每题用时 |",
      "| --- | --- | --- | --- | --- | --- | --- | --- |"]
for (l, r) in sorted(cells, key=lambda k: cells[k]["cell"]):
    s = st[(l, r)]
    L.append(f"| {name(l)} {r} | {share(s['busy'])} | {tp(l, r, TWO):.3f} / {tp(l, r, PHASE):.3f} | {s['n']} | {s['T_ms']:.0f} | "
             f"{s['m_ms']:.0f} | {s['R_ms']:.0f} | {s['m_ms'] / s['T_ms'] * 100:.0f}% |")
L += ["", "## 吞吐收益：拆开对 Monolithic", "",
      "| 拆法 | 轮 | 两点法 | 整波平均（主数） | 扣除模型快慢以后 |", "| --- | --- | --- | --- | --- |"]
for s in splits:
    for r in [*rounds, "pooled"]:
        g = G[s][r]
        L.append(f"| {name(s)} | {'两轮合并' if r == 'pooled' else r} | {pct(g['two'])} | {pct(g['phase'])} | {pct(g['adjusted'])} |")
if D:
    L += ["", "## 噪声参照：Monolithic 相对 Direct", "",
          "Ditto 运行时本身的代价预计很小（每题主线程多几毫秒），这里的差多半是噪声。", "",
          "| 轮 | 两点法 | 整波平均 | 扣除模型快慢以后 |", "| --- | --- | --- | --- |"]
    for r in [*rounds, "pooled"]:
        g = D[r]
        L.append(f"| {'两轮合并' if r == 'pooled' else r} | {pct(g['two'])} | {pct(g['phase'])} | {pct(g['adjusted'])} |")
if odd:
    L += ["", "**出口记录里不寻常的**（重试、同一步发了不止一次的用时都已算进 m）：", ""] + [f"- {x}" for x in odd]
L += ["", f"## 正确率：{acc.get('family', len(splits) * 2)} 组单轮比较单独做 Holm", ""]
if acc["complete"]:
    L += ["| 比较 | 共同题数（排除的题） | Monolithic | 拆开 | 只有 Monolithic 对 / 只有拆开对 | 差（拆开 − Monolithic，百分点，95% 区间） | 符号检验 p | Holm 校正后 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, v in acc["rows"].items():
        s, r = k.split(" ")
        L.append(f"| {name(s)} {r} | {v['n']}（{v['excluded']}） | {v['mono'] * 100:.1f}% | {v['split'] * 100:.1f}% | {v['only_mono']} / {v['only_split']} | "
                 f"{v['diff'] * 100:+.1f}（{v['lo'] * 100:+.1f} 到 {v['hi'] * 100:+.1f}） | {v['p']:.4f} | {v['p_holm']:.4f} |")
    L += ["", "参照：同一布局跑两次（temperature 0 也不保证每次回答一样）：", "",
          "| 比较 | 共同题数（排除的题） | 差（后一轮 − 前一轮，百分点，95% 区间） | 符号检验 p |", "| --- | --- | --- | --- |"]
    for x in acc["refs"]:
        if "p" in x:
            L.append(f"| {x['比较']} | {x['n']}（{x['excluded']}） | {x['diff'] * 100:+.1f}（{x['lo'] * 100:+.1f} 到 {x['hi'] * 100:+.1f}） | {x['p']:.4f} |")
        else:
            L.append(f"| {x['比较']} | — | —（没有两边都判过的题） | — |")
    L += ["", "没检出差别，不等于没有差别。report.md 里“Holm 校正后 p”那一列是全部比较一起校正的，不是判据。"]
else:
    L.append(acc_verdict)
md = "\n".join(L) + "\n"
summary = {"out": str(OUT), "plan_check": plan_note, "concurrency": CONC, "busy_mono": busy, "busy_low": low, "throughput_verdict": verdict,
           "check_text": check_text, "flags": flags, "cells": {f"{l} {r}": s for (l, r), s in st.items()}, "gains_vs_mono": G,
           "mono_vs_direct": D, "accuracy_verdict": acc_verdict, "accuracy": acc, "unusual_egress": odd}
(OUT / "判据.md").write_text(md, encoding="utf-8")
(OUT / "判据.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(md, end="")
