"""
离线并发扫描的汇总和判断（docs\\34 第 3 节）。只读扫描结果目录里的东西；写 sweep-summary.json、curves.csv，Markdown 打到标准输出。

    python -I src/sweep_report.py <扫描结果目录>                       打印 Markdown（sweep.sh 存成 扫描结果.md）
    python -I src/sweep_report.py <扫描结果目录> --supplements <场景>    只打印要补测的布局（空格分开，换行只用 \\n；不用补就是空行），sweep.sh 用

读数直接取各格的原始结果（raw\\<格子>.json，不用舍入过的 cells.csv），只算通过验收的格子：有 raw\\<格子>.ok，而且标记里的摘要和结果文件一致。
计划（窗口、档位、布局、题数、假模型设定）取 sweep.sh 写的 sweep-plan.json，和各格记下的设定逐格核对。

吞吐有两种算法（docs\\34 第 3 节）：
- 整波平均（判断用这个）：题是成批（一波约“并发”道）做完的，只用窗口两头两个时刻算，读数取决于两头落在波的什么位置。
  所以起点取遍第 20 到第 19+c 次做完（c 是并发），每段 L = (最后一题派出时的完成次数) − 20 − c 次完成，各段用时取平均，吞吐 = L ÷ 平均用时。
  各并发下这些段的中心都在同一处（第 110 次做完前后），也都在最后一题派出之前（机器满负荷）。
- 两点法：run.ts 记的 第 20 次做完 → 最后一题派出 之间的吞吐（以前各轮的算法），照列，用来对照。

判据在跑之前写定，边界都含等号：
- 某布局在 36 到顶：48、64 两档的吞吐都不比 36 高出 3% 以上（正好高 3% 算到顶）。先要 36、48、64 三档的第 1、2 遍都在。
- 补测：只看前两遍。没到顶，但超出 3% 的部分不大于两遍之间的相对差（36 和超出最多的那一档；两档并列最多时都算，取最大）。
  要补的布局，36、48、64 三档的第 3、4 遍都在了才按四遍平均再判；不用补的布局，第 3、4 遍即使有也不算。
- 没在 36 到顶的布局：64 不比 48 高出 3% 以上就记 48，否则记“至少 64”（64 以上没测）。
- 正式实验的并发，推荐 36 要同时满足：两个场景的全部布局都能定案、用整波平均都在 36 到顶；两点法对“在 36 到顶”的判断和它一致；
  两个场景“各格请求、结果逐字相同”的核对都通过、核对的就是现在这批格子；判断用到的格子（36、48、64）没有读数断档。
  不满足就列出曲线和原因，另行决定。
- 读数断档：每格每 5 秒取一次资源读数，窗口里相邻两次相隔超过 15 秒（电脑睡眠、被别的程序卡住）就标出这一格。
- 低并发组：S4 Monolithic 编排器主线程忙的比例（照计划的遍数取平均）严格低于 70% 的最高一档。
- 新旧程序对照：旧 新 新 旧 四格都通过验收时，比编排器每题主线程忙多久（主线程忙的比例 ÷ 整波平均吞吐）；
  新版、旧版各两格平均相差在 ±3% 以内（含）就算看不出程序版本的影响。
"""
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", newline="\n")
sys.stderr.reconfigure(encoding="utf-8")
OUT = Path(sys.argv[1])
TOL, EPS, BUSY_LOW, GAP_S = 0.03, 1e-9, 0.70, 15
NAMES = {"mono": "Monolithic", "direct": "Direct", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT",
         "split-both": "两个都隔离", "split-memctx": "合并（MEMORY 和 CONTEXT 一个进程）", "split-interaction": "隔离 INTERACTION+RETRIEVAL"}
TITLES = {"rag": "rag-qa", "s4": "S4"}
PROCS = {"orchestrator": "编排器", "memory": "MEMORY 进程", "context": "CONTEXT 进程", "memctx": "合并进程", "interaction": "INTERACTION 进程"}
PASSES = ("p1", "p2", "p3", "p4")

plan_file = OUT / "sweep-plan.json"
if not plan_file.exists():
    raise SystemExit(f"{plan_file} 不在：先用 sweep.sh run 跑（它写计划）")
PLAN = json.loads(plan_file.read_text(encoding="utf-8"))
LOW, LEVELS, WARM = PLAN["low"], PLAN["levels"], PLAN["warmup"]
ALL_LEVELS = LOW + LEVELS


def over(a, b):
    """a 比 b 高出 3% 以上（正好 3% 不算；浮点误差留 EPS）。"""
    return a / b - 1 > TOL + EPS


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def validated(raw):
    """通过验收的格子 {名字: 摘要}，和没验收（没有标记或摘要对不上）的结果文件。"""
    ok, bad = {}, []
    for p in sorted(raw.glob("*.json")):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        mark = p.with_suffix(".ok")
        try:
            good = mark.exists() and json.loads(mark.read_text(encoding="utf-8")).get("sha256") == h
        except ValueError:
            good = False
        if good:
            ok[p.stem] = h
        else:
            bad.append(p.stem)
    return ok, bad


def phase_tp(d):
    """整波平均的吞吐（题/秒）。最后一题在第 count − c 次做完时派出；段数不够（L < 1）就是 None。"""
    c, n = d["meta"]["concurrency"], d["meta"]["count"]
    end = n - c
    e = sorted(t["end_ms"] for t in d["tasks"] if t["ok"])
    L = end - WARM - c
    if L < 1 or len(e) < end:
        return None
    T = sum(e[s + L - 1] - e[s - 1] for s in range(WARM, WARM + c)) / c
    return L / (T / 1000)


def max_gap_s(d):
    """窗口里相邻两次资源读数（每 5 秒一次）最长隔了几秒；读数不全就是 None。"""
    w = d["window"]
    lo, hi = w.get("from_unix_ms"), w.get("to_unix_ms")
    at = sorted(s["at_ms"] for s in d.get("samples") or [] if lo is not None and lo - 6000 <= s["at_ms"] <= hi + 6000)
    return max((b - a for a, b in zip(at, at[1:])), default=None) / 1000 if len(at) >= 2 else None


def metrics(d):
    """一格的读数：两种吞吐、窗口里做完的题、窗口秒数、各进程主线程忙的比例、用掉的核、虚拟机 CPU、每题用时 p95、读数断档。"""
    w, m = d["window"], d["meta"]
    procs = {"orchestrator": w["orchestrator"], **w["remote"]}
    cores = [p.get("container_cores_used") for p in procs.values()]
    lat = [t["end_ms"] - t["start_ms"] for t in d["tasks"] if t["ok"] and w["from_ms"] <= t["end_ms"] <= w["to_ms"]]
    return {"throughput": phase_tp(d), "throughput_two": w["throughput_per_s"], "completed": w["completed"],
            "window_s": (w["to_ms"] - w["from_ms"]) / 1000, "busy": {k: p.get("main_busy_share") for k, p in procs.items()},
            "cores": sum(cores) if all(c is not None for c in cores) else None, "vm": w["orchestrator"].get("vm_cpu_busy_share"),
            "p95_ms": pct(lat, 95), "gap_s": max_gap_s(d), "count": m["count"], "started": m.get("started")}


def load(scen):
    """cells[(布局, 并发, 遍)] = 读数（只算通过验收的格子），另返回 {名字: 摘要} 和没验收的结果文件。"""
    raw = OUT / scen / "raw"
    ok, bad = validated(raw) if raw.exists() else ({}, [])
    cells = {}
    for name in ok:
        d = json.loads((raw / f"{name}.json").read_text(encoding="utf-8"))
        m = d["meta"]
        layout, conc = m["layout"], m["concurrency"]
        rest = name.removeprefix(f"sweep-{scen}-k2-c{conc}-{layout}-")
        if rest not in PASSES or m.get("label") != f"sweep-{scen}|c{conc}-{rest}" or m.get("tag") != name:
            raise SystemExit(f"{name}：格子名、轮名标签、记下的设定对不上（{m.get('label')}）")
        if m["count"] != PLAN["window_end"] + conc or m.get("warmup") != WARM or layout not in PLAN["scenarios"][scen]["layouts"]:
            raise SystemExit(f"{name}：题数 {m['count']}、热身 {m.get('warmup')}、布局 {layout} 和计划对不上")
        if (layout, conc, rest) in cells:
            raise SystemExit(f"{name}：同一（布局、并发、遍）有两格")
        x = metrics(d)
        if x["throughput"] is None:
            raise SystemExit(f"{name}：算不出整波平均的吞吐（题数不够）")
        cells[(layout, conc, rest)] = x
    return cells, ok, bad


def tp(cells, layout, c, passes, key="throughput"):
    """几遍吞吐的平均；有一遍缺就是 None。"""
    xs = [cells.get((layout, c, p)) for p in passes]
    return None if any(x is None for x in xs) else mean([x[key] for x in xs])


def spread(cells, layout, c, key="throughput"):
    """前两遍之间的相对差：|第 1 遍 − 第 2 遍| ÷ 两遍平均。"""
    a, b = (cells.get((layout, c, p)) for p in ("p1", "p2"))
    return None if a is None or b is None else abs(a[key] - b[key]) / ((a[key] + b[key]) / 2)


def judge36(t36, t48, t64):
    return {"t36": t36, "t48": t48, "t64": t64, "g48": t48 / t36 - 1, "g64": t64 / t36 - 1,
            "plateau36": not over(t48, t36) and not over(t64, t36),
            "level": None if not (over(t48, t36) or over(t64, t36)) else (48 if not over(t64, t48) else 64)}


def decide(cells, layout, key="throughput"):
    """一个布局的判断。status：基础遍缺失 / 待补 / 补测未齐 / 可定案。"""
    out = {"layout": layout}
    base_missing = [f"c{c}-{p}" for c in (36, 48, 64) for p in ("p1", "p2") if (layout, c, p) not in cells]
    if base_missing:
        return {**out, "status": "基础遍缺失", "missing": base_missing}
    base = judge36(*(tp(cells, layout, c, ("p1", "p2"), key) for c in (36, 48, 64)))
    per_pass = {p: judge36(*(cells[(layout, c, p)][key] for c in (36, 48, 64)))["plateau36"] for p in ("p1", "p2")}
    out.update(base=base, per_pass_plateau36=per_pass, sensitive=per_pass["p1"] != per_pass["p2"])
    need = False
    if not base["plateau36"]:
        gmax = max(base["g48"], base["g64"])
        tied = [c for c, g in ((48, base["g48"]), (64, base["g64"])) if abs(g - gmax) <= EPS]
        spreads = [spread(cells, layout, c, key) for c in [36, *tied]]
        out["spread_used"] = max(spreads)
        need = gmax - TOL <= max(spreads) + EPS
    out["needs_supplement"] = need
    extra = [f"c{c}-{p}" for c in (36, 48, 64) for p in ("p3", "p4") if (layout, c, p) in cells]
    if not need:
        if extra:
            out["ignored_supplements"] = extra
        return {**out, "status": "可定案", "final": base, "passes": 2}
    sup_missing = [f"c{c}-{p}" for c in (36, 48, 64) for p in ("p3", "p4") if (layout, c, p) not in cells]
    if sup_missing:
        return {**out, "status": "待补" if not extra else "补测未齐", "missing": sup_missing}
    return {**out, "status": "可定案", "final": judge36(*(tp(cells, layout, c, PASSES, key) for c in (36, 48, 64))), "passes": 4}


def check_status(scen, ok):
    """“各格请求、结果逐字相同”的核对（cell_check.py sweep 写的 sweep-check.json）：通过，而且核对的就是现在这批格子。"""
    f = OUT / scen / "sweep-check.json"
    if not f.exists():
        return "没核对"
    c = json.loads(f.read_text(encoding="utf-8"))
    if c.get("cells") != ok:
        return "过期（核对以后格子变了）"
    return "通过" if c.get("status") == "pass" else "没通过"


# ---------------------------------------------------------------- 补测名单（sweep.sh 用）
if len(sys.argv) > 3 and sys.argv[2] == "--supplements":
    scen = sys.argv[3]
    if scen not in PLAN["scenarios"]:
        raise SystemExit(f"计划里没有场景 {scen}")
    cells = load(scen)[0]
    print(" ".join(l for l in PLAN["scenarios"][scen]["layouts"] if decide(cells, l).get("needs_supplement")))
    sys.exit(0)

pctx = lambda x, nd=1: "—" if x is None else f"{x * 100:+.{nd}f}%"
share = lambda x: "—" if x is None else f"{x * 100:.1f}%"
f3 = lambda x: "—" if x is None else f"{x:.3f}"
ms0 = lambda x: "—" if x is None else f"{x:.0f}"
out, summary, curves = [], {"plan": PLAN, "scenarios": {}}, []
w = out.append

# ---------------------------------------------------------------- 新旧程序对照
pc = PLAN.get("prog_check") or {}
prog_dir = OUT / "prog-check" / "raw"
prog_ok, prog_bad = validated(prog_dir) if prog_dir.exists() else ({}, [])
prog = []
for spec in pc.get("order", []):
    name = f"progcheck-rag-k2-c37-mono-{spec}"
    if name not in prog_ok:
        prog.append({"cell": name, "spec": spec, "valid": False})
        continue
    d = json.loads((prog_dir / f"{name}.json").read_text(encoding="utf-8"))
    win, m = d["window"], d["meta"]
    o = win["orchestrator"]
    if m["count"] != pc["count"] or m["concurrency"] != 37 or m["layout"] != "mono" or m.get("label") != f"progcheck|{spec}":
        raise SystemExit(f"{name}：设定和计划对不上")
    t = phase_tp(d)
    prog.append({"cell": name, "spec": spec, "valid": t is not None, "throughput": t, "throughput_two": win["throughput_per_s"],
                 "busy_share": o["main_busy_share"], "busy_ms_per_task": o["main_busy_share"] * 1000 / t if t else None,
                 "busy_ms_per_task_two": o["main_busy_ms"] / win["completed"],
                 "thread_cpu_ms_per_task": o["main_thread_cpu_ms"] / (o["wall_ms"] / 1000 * t) if t else None,
                 "started": m.get("started")})
prog_result = None
if prog and all(r["valid"] for r in prog):
    by = {r["spec"]: r for r in prog}
    mo, mn = (mean([by[s]["busy_ms_per_task"] for s in pc["order"] if s.startswith(v)]) for v in ("old", "new"))
    to, tn = (mean([by[s]["throughput"] for s in pc["order"] if s.startswith(v)]) for v in ("old", "new"))
    starts = [by[s]["started"] for s in pc["order"]]
    ch = mn / mo - 1
    prog_result = {"old_busy_ms": mo, "new_busy_ms": mn, "busy_change": ch, "throughput_change": tn / to - 1,
                   "pairs": {"new-p1 对 old-p1": by["new-p1"]["busy_ms_per_task"] / by["old-p1"]["busy_ms_per_task"] - 1,
                             "new-p2 对 old-p2": by["new-p2"]["busy_ms_per_task"] / by["old-p2"]["busy_ms_per_task"] - 1},
                   "in_planned_order": starts == sorted(starts),
                   "verdict": "增加" if ch > TOL + EPS else "减少" if ch < -TOL - EPS else "界内"}
summary["prog_check"] = {"cells": prog, "unvalidated": prog_bad, "result": prog_result}

# ---------------------------------------------------------------- 各场景
for scen, conf in PLAN["scenarios"].items():
    cells, ok, bad = load(scen)
    layouts = conf["layouts"]
    dec = {l: decide(cells, l) for l in layouts}

    def two_point_plateau(l):
        """两点法“在 36 到顶”的判断，和整波平均用同一批遍次（不走它自己的补测逻辑）；整波平均没定案就是 None。"""
        d = dec[l]
        if d["status"] != "可定案":
            return None
        ps = PASSES if d["passes"] == 4 else ("p1", "p2")
        return judge36(*(tp(cells, l, c, ps, "throughput_two") for c in (36, 48, 64)))["plateau36"]

    two = {l: two_point_plateau(l) for l in layouts}
    s = summary["scenarios"][scen] = {"check": check_status(scen, ok) if ok else "没有格子", "unvalidated": bad, "layouts": dec,
                                      "two_point_plateau36": two}
    s["all_decided_plateau36"] = all(v["status"] == "可定案" and v["final"]["plateau36"] for v in dec.values())
    s["two_point_disagrees"] = [l for l in layouts if dec[l]["status"] == "可定案" and two[l] != dec[l]["final"]["plateau36"]]
    # 接近门槛（只作提醒，不改判断）：48 或 64 对 36 的超出落在 1.5% 到 4.5% 之间。两遍是同一批题、同样等法的重复，
    # “两遍的差”量不到“这批题恰好这样排”的误差，贴近 3% 的结论不够硬，要看曲线
    s["near_threshold"] = [l for l in layouts if dec[l]["status"] == "可定案"
                           and any(abs(dec[l]["final"][g] - TOL) <= 0.015 + EPS for g in ("g48", "g64"))]
    s["gaps"] = {f"{l}-c{c}-{p}": x["gap_s"] for (l, c, p), x in cells.items() if x["gap_s"] is not None and x["gap_s"] > GAP_S}
    s["gaps_in_decision"] = [k for k in s["gaps"] if any(f"-c{c}-" in k for c in (36, 48, 64))]
    for (l, c, p), x in sorted(cells.items(), key=lambda kv: (layouts.index(kv[0][0]), kv[0][1], kv[0][2])):
        curves.append({"scenario": scen, "layout": l, "concurrency": c, "pass": p, "throughput_phase_avg": x["throughput"],
                       "throughput_two_point": x["throughput_two"], "window_completed": x["completed"], "window_s": x["window_s"],
                       "p95_ms": x["p95_ms"], **{f"{k}_busy_share": v for k, v in x["busy"].items()}, "total_cores_used": x["cores"],
                       "vm_cpu_busy_share": x["vm"], "max_sample_gap_s": x["gap_s"], "started": x["started"]})
    if scen == "s4":
        busy = {}
        for c in ALL_LEVELS:
            need = ("p1",) if c in LOW else ("p1", "p2")
            xs = [cells.get(("mono", c, p)) for p in need]
            busy[c] = None if any(x is None for x in xs) else mean([x["busy"]["orchestrator"] for x in xs])
        under = [c for c in ALL_LEVELS if busy[c] is not None and busy[c] < BUSY_LOW]
        chosen = max(under) if under else None
        s["low_concurrency"] = {"mono_orchestrator_busy": busy, "chosen": chosen,
                                "missing_above": [c for c in ALL_LEVELS if busy[c] is None and (chosen is None or c > chosen)]}
    s["_cells"] = cells
reasons = []
if set(summary["scenarios"]) != set(PLAN["scenarios"]):
    reasons.append("有场景没有结果")
for scen, s in summary["scenarios"].items():
    if not s["all_decided_plateau36"]:
        reasons.append(f"{TITLES[scen]} 不是全部布局都能定案并在 36 到顶")
    if s["two_point_disagrees"]:
        reasons.append(f"{TITLES[scen]} 的 {'、'.join(NAMES[l] for l in s['two_point_disagrees'])} 两种算法判断不一致")
    if s["check"] != "通过":
        reasons.append(f"{TITLES[scen]} 的逐字相同核对{s['check']}")
    if s["gaps_in_decision"]:
        reasons.append(f"{TITLES[scen]} 判断用到的格子有读数断档：{'、'.join(s['gaps_in_decision'])}")
summary["recommended_concurrency"] = None if reasons else 36
summary["not_recommended_because"] = reasons

# ---------------------------------------------------------------- 结论
w("# 离线并发扫描结果（原种子 20261119，假模型，2 核）\n")
w(f"由 `src/sweep_report.py` 从各格的原始结果直接生成（只算通过验收的格子），判据在跑之前写在 docs\\34 第 3 节，边界都含等号。"
  f"每格跑“{PLAN['window_end']} + 并发”道题、热身 {WARM} 题，最后一题在第 {PLAN['window_end']} 次做完时派出。"
  f"**吞吐用整波平均判断**：起点取遍第 {WARM} 到第 {WARM - 1} + 并发 次做完，每段 {PLAN['window_end'] - WARM} − 并发 次完成，各段用时平均；"
  "以前各轮的两点法（第 20 次做完 → 最后一题派出）照列对照。假模型接在出口后面，提示词、请求和真实运行相同，延迟按题和步骤固定：" +
  "；".join(f"{TITLES[k]} {v['fake']}" for k, v in PLAN["scenarios"].items()) +
  "。结论都是“在这套假模型设定下”的；吞吐的绝对值不能和真实运行直接比，看的是随并发怎么变。\n")
w("## 结论\n")
if prog_result:
    v = prog_result["verdict"]
    w(f"- **新旧程序对照**（{pc['desc']}）：编排器每题主线程忙，旧版 {prog_result['old_busy_ms']:.0f} 毫秒，新版 {prog_result['new_busy_ms']:.0f} 毫秒"
      f"（{pctx(prog_result['busy_change'])}；相邻两对分别 " + "、".join(pctx(x) for x in prog_result["pairs"].values()) + "）。"
      + {"增加": "新版每题多花的超出 ±3%：新版程序本身更费编排器的主线程，docs/33 的吞吐变低至少有一部分来自程序版本。",
         "减少": "新版每题反而少花，超出 ±3%：在这项筛查里（只测了 rag Monolithic 并发 37，两版各两格），docs/33 的吞吐变低看不出是新版程序造成的。",
         "界内": "差别在 ±3% 以内，看不出程序版本的影响（两版各两格，只是筛查，不能证明完全相同）；docs/33 的吞吐变低多半来自机器状态等别的原因。"}[v]
      + ("" if prog_result["in_planned_order"] else "注意：四格实际跑的先后不是计划的 旧 新 新 旧。"))
else:
    w(f"- **新旧程序对照**：四格没有都通过验收（见第 1 节），不下结论。")
for scen, s in summary["scenarios"].items():
    parts = []
    for l, d in s["layouts"].items():
        if d["status"] != "可定案":
            parts.append(f"{NAMES[l]}：{d['status']}（缺 {'、'.join(d['missing'])}）")
        elif not d["final"]["plateau36"]:
            parts.append(f"{NAMES[l]}：在 36 没到顶，" + ("按同一判据到顶在 48" if d["final"]["level"] == 48 else "到 64 还在涨（至少 64，64 以上没测）"))
    head = (f"{len(s['layouts'])} 种布局都在并发 36 到顶" if s["all_decided_plateau36"] else "不是全部布局都能在 36 定案并到顶")
    w(f"- **{TITLES[scen]}**（整波平均）：{head}" + ("；" + "；".join(parts) if parts else "") + "。"
      + (f"两点法判断不一致的：{'、'.join(NAMES[l] for l in s['two_point_disagrees'])}。" if s["two_point_disagrees"] else "两点法的判断一致。")
      + (f"**接近门槛**（48 或 64 对 36 的超出在 1.5%–4.5% 之间，结论不够硬，要看曲线）：{'、'.join(NAMES[l] for l in s['near_threshold'])}。" if s["near_threshold"] else "")
      + f"各格请求、结果逐字相同的核对：{s['check']}。"
      + (f"读数断档的格子：{'、'.join(s['gaps'])}。" if s["gaps"] else "")
      + (f"另有没验收的结果文件 {len(s['unvalidated'])} 个，没算进来。" if s["unvalidated"] else ""))
w("- **正式实验的并发**：" + ("两个场景的全部布局都在 36 到顶，两种算法一致，核对都通过，没有读数断档，按方案用 36。" if not reasons else
    "不推荐，原因：" + "；".join(reasons) + "。按方案列出曲线，另行决定。"))
low = summary["scenarios"].get("s4", {}).get("low_concurrency")
if low:
    w(f"- **低并发组**：" + (f"S4 Monolithic 编排器主线程忙的比例低于 70% 的最高一档是 {low['chosen']}（忙 {share(low['mono_orchestrator_busy'][low['chosen']])}）"
                             if low["chosen"] else "S4 Monolithic 测过的各档，编排器主线程忙的比例都不低于 70%")
      + (f"；更高的 {'、'.join(map(str, low['missing_above']))} 缺读数，这只是候选" if low["missing_above"] else "")
      + "。假模型用的是以前最慢的延迟，真实模型更快时主线程会更忙，到时用低并发组的第一格核实。")
w("")

# ---------------------------------------------------------------- 第 1 节：新旧程序对照
w(f"## 1. 新旧程序对照（{pc.get('desc', '')}）\n")
w("旧版是 11-20 凌晨 rag 那一轮（results\\rag-real-a）用的程序（docs\\31_附录\\程序备份），新版是现在的程序；同一套数据、库、假模型和出口，"
  "前面先跑一格不计入的热身，再按 旧 新 新 旧 的顺序跑。“每题主线程忙”= 窗口里编排器主线程忙的比例 × 1000 ÷ 整波平均吞吐（毫秒）；"
  "两点法的算法是窗口里主线程忙的毫秒 ÷ 窗口里做完的题，照列。\n")
w("| 格子 | 有效 | 开始（UTC） | 吞吐：整波平均 / 两点法（题/秒） | 主线程忙的比例 | 每题主线程忙：整波 / 两点（毫秒） | 每题主线程 CPU（毫秒） |")
w("| --- | --- | --- | --- | --- | --- | --- |")
for r in prog:
    if "busy_share" not in r:
        w(f"| {r['cell']} | **否**（没有通过验收的结果） | — | — | — | — | — |")
        continue
    w(f"| {r['cell']} | {'是' if r['valid'] else '**否**（算不出整波平均）'} | {(r['started'] or '')[11:19]} | {f3(r['throughput'])} / {f3(r['throughput_two'])} | "
      f"{share(r['busy_share'])} | {ms0(r['busy_ms_per_task'])} / {ms0(r['busy_ms_per_task_two'])} | {ms0(r['thread_cpu_ms_per_task'])} |")
if prog_bad:
    w(f"\n没验收的结果文件：{'、'.join(prog_bad)}（没算进来）。")
w("")

# ---------------------------------------------------------------- 各场景
sec = 2
for scen, s in summary["scenarios"].items():
    cells, layouts = s.pop("_cells"), PLAN["scenarios"][scen]["layouts"]
    title = TITLES[scen]
    w(f"## {sec}. {title}：吞吐随并发（题/秒，整波平均，计划内各遍的平均；括号里是前两遍的相对差）\n")
    w("| 布局 | " + " | ".join(f"并发 {c}" for c in ALL_LEVELS) + " |")
    w("| --- | " + " | ".join("---" for _ in ALL_LEVELS) + " |")
    for l in layouts:
        cols = []
        for c in ALL_LEVELS:
            have = [p for p in PASSES if (l, c, p) in cells]
            t = mean([cells[(l, c, p)]["throughput"] for p in have])
            sp = spread(cells, l, c)
            cols.append("—" if t is None else f"{t:.3f}" + (f"（{sp * 100:.1f}%）" if sp is not None else "")
                        + (f" ×{len(have)}遍" if len(have) != (1 if c in LOW else 2) else ""))
        w(f"| {NAMES[l]} | " + " | ".join(cols) + " |")
    w(f"\n{'、'.join(map(str, LOW))} 只跑一遍；{LEVELS[0]} 起各两遍（第 1 遍并发从低到高，第 2 遍从高到低）；遍数和计划不同的标了遍数。"
      "两点法的读数见 curves.csv 和 2.2 节。\n")
    w(f"### {sec}.1 在 36 到顶了吗（整波平均；48、64 对 36 高出 3% 以上才算没到顶）\n")
    w("| 布局 | 状态 | 前两遍：36 / 48（对 36）/ 64（对 36） | 两遍各自判 | 补测 | 最终（遍数）：48 对 36 / 64 对 36 | 判断 | 两点法的判断 | 36 对 8–24 里最高 | 36 时最忙的主线程 | 36 时用掉的核（共 2 个） |")
    w("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for l in layouts:
        d, t2 = s["layouts"][l], s["two_point_plateau36"][l]
        rows36 = [cells[(l, 36, p)] for p in PASSES if (l, 36, p) in cells]
        procs = {k: mean([r["busy"].get(k) for r in rows36]) for k in PROCS}
        who = max(((k, v) for k, v in procs.items() if v is not None), key=lambda x: x[1], default=(None, None))
        cores = mean([r["cores"] for r in rows36])
        lower = [tp(cells, l, c, ("p1", "p2")) for c in LEVELS if c < 36]
        lower = [x for x in lower if x is not None]
        t36 = tp(cells, l, 36, ("p1", "p2"))
        vs_lower = pctx(t36 / max(lower) - 1) if lower and t36 else "—"
        two = "—" if t2 is None else ("在 36 到顶" if t2 else "没到顶")
        if d["status"] == "基础遍缺失":
            w(f"| {NAMES[l]} | 基础遍缺失：{'、'.join(d['missing'])} | — | — | — | — | — | {two} | {vs_lower} | {PROCS.get(who[0], '—')} {share(who[1])} | {f3(cores)} |")
            continue
        b = d["base"]
        pp = "、".join(f"{p} {'到顶' if v else '没到顶'}" for p, v in d["per_pass_plateau36"].items()) + ("（**对遍次敏感**）" if d["sensitive"] else "")
        sup = ("要补，" + ("已补齐" if d["status"] == "可定案" else f"{d['status']}，缺 {'、'.join(d['missing'])}")) if d["needs_supplement"] else (
            "不用" + (f"（{'、'.join(d['ignored_supplements'])} 没触发补测，不算）" if d.get("ignored_supplements") else ""))
        if d["status"] == "可定案":
            fnl = d["final"]
            final = f"（{d['passes']} 遍）{pctx(fnl['g48'])} / {pctx(fnl['g64'])}"
            judge = ("在 36 到顶" if fnl["plateau36"] else ("没到顶，到顶在 48" if fnl["level"] == 48 else "没到顶，至少 64")) + (
                "（**接近门槛**）" if l in s["near_threshold"] else "")
        else:
            final, judge = "—", "待定"
        w(f"| {NAMES[l]} | {d['status']} | {f3(b['t36'])} / {f3(b['t48'])}（{pctx(b['g48'])}）/ {f3(b['t64'])}（{pctx(b['g64'])}） | {pp} | {sup} | {final} | "
          f"{judge} | {two} | {vs_lower} | {PROCS.get(who[0], '—')} {share(who[1])} | {f3(cores)} |")
    w("\n“36 对 8–24 里最高”只作描述：负数说明 36 比更低的某档还慢，正式实验用 36 会亏待这个布局。\n")
    w(f"### {sec}.2 每遍的读数（36、37、48、64，题/秒；整波平均 / 两点法）\n")
    w("| 布局 | 遍 | 36 | 37 | 48 | 64 | 48 对 36 | 64 对 36 | 64 对 48 |")
    w("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for l in layouts:
        for p in PASSES:
            v = {c: cells.get((l, c, p)) for c in (36, 37, 48, 64)}
            if not any(v.values()):
                continue
            t = {c: (x["throughput"] if x else None) for c, x in v.items()}
            t2 = {c: (x["throughput_two"] if x else None) for c, x in v.items()}
            r = lambda a, b: pctx(t[a] / t[b] - 1) if t[a] and t[b] else "—"
            both = lambda c: "—" if t[c] is None else f"{t[c]:.3f} / {t2[c]:.3f}"
            w(f"| {NAMES[l]} | {p} | {both(36)} | {both(37)} | {both(48)} | {both(64)} | {r(48, 36)} | {r(64, 36)} | {r(64, 48)} |")
    w("")
    w(f"### {sec}.3 36 对 37（以前各轮用 37；只用两档都有的第 1、2 遍）\n")
    w("| 布局 | 36 | 37 | 37 对 36（整波平均） | 37 对 36（两点法） |")
    w("| --- | --- | --- | --- | --- |")
    for l in layouts:
        t36, t37 = tp(cells, l, 36, ("p1", "p2")), tp(cells, l, 37, ("p1", "p2"))
        u36, u37 = tp(cells, l, 36, ("p1", "p2"), "throughput_two"), tp(cells, l, 37, ("p1", "p2"), "throughput_two")
        w(f"| {NAMES[l]} | {f3(t36)} | {f3(t37)} | {pctx(t37 / t36 - 1) if t36 and t37 else '—'} | {pctx(u37 / u36 - 1) if u36 and u37 else '—'} |")
    w("")
    w(f"### {sec}.4 最忙的主线程忙的比例、用掉的核（各遍平均；先对每个进程取平均再挑最忙的）\n")
    w("| 布局 | " + " | ".join(f"并发 {c}" for c in ALL_LEVELS) + " |")
    w("| --- | " + " | ".join("---" for _ in ALL_LEVELS) + " |")
    for l in layouts:
        cols = []
        for c in ALL_LEVELS:
            rows = [cells[(l, c, p)] for p in PASSES if (l, c, p) in cells]
            if not rows:
                cols.append("—")
                continue
            procs = {k: mean([r["busy"].get(k) for r in rows]) for k in PROCS}
            who = max(((k, v) for k, v in procs.items() if v is not None), key=lambda x: x[1])
            cols.append(f"{PROCS[who[0]]} {share(who[1])}，{f3(mean([r['cores'] for r in rows]))} 核")
        w(f"| {NAMES[l]} | " + " | ".join(cols) + " |")
    vms = [x["vm"] for x in cells.values() if x["vm"] is not None]
    comp = [x["completed"] for x in cells.values()]
    gaps = [x["gap_s"] for x in cells.values() if x["gap_s"] is not None]
    w(f"\n整台虚拟机 CPU 忙的比例：各格平均 {share(mean(vms))}、最高 {share(max(vms) if vms else None)}（看有没有别的重活）。"
      f"两点法窗口里做完的题：{min(comp) if comp else '—'} 到 {max(comp) if comp else '—'} 题。"
      f"相邻两次资源读数最长相隔 {f3(max(gaps) if gaps else None)} 秒（超过 {GAP_S} 秒的格子标为读数断档）。\n")
    if scen == "s4":
        lc = s["low_concurrency"]
        w(f"### {sec}.5 低并发组用哪一档（S4 Monolithic 编排器主线程忙的比例严格低于 70% 的最高一档）\n")
        w("| 并发 | " + " | ".join(str(c) for c in ALL_LEVELS) + " |")
        w("| --- | " + " | ".join("---" for _ in ALL_LEVELS) + " |")
        w("| 编排器主线程忙 | " + " | ".join(share(lc["mono_orchestrator_busy"][c]) for c in ALL_LEVELS) + " |")
        w(f"\n选出的一档：{lc['chosen'] if lc['chosen'] else '没有'}" + (f"（更高的 {'、'.join(map(str, lc['missing_above']))} 缺读数）" if lc["missing_above"] else "") + "。\n")
    w(f"各格请求、结果逐字相同的核对：{s['check']}（{scen}\\sweep-check.txt）。" + (f"没验收的结果文件：{'、'.join(s['unvalidated'])}（没算进来）。" if s["unvalidated"] else "") + "\n")
    sec += 1

failed = OUT / "failed-cells.txt"
w(f"## {sec}. 作废的格子\n")
w(("作废过的格子（挪进 failed\\；重跑过的以重跑的为准）：\n\n```\n" + failed.read_text(encoding="utf-8").strip() + "\n```\n") if failed.exists() else "没有。\n")
w(f"## {sec + 1}. 说明\n")
w("- 每格的表（各场景的 cells.csv、扫描结果.xlsx）里的吞吐是两点法；“真实模型调用”一列在扫描里是出口转发给假模型的请求数，不是真实模型；费用是按假模型的字数模拟记账的。")
w("- curves.csv 是每格每遍的原始读数：两种吞吐、窗口里做完的题数、窗口秒数、各进程主线程忙的比例、用掉的核、虚拟机 CPU、读数间隔。")
w("- S4 每题的活随记忆库变大而变多。并发越高，同一完成进度下在途的题越多、已经写进记忆库的也越多（并发 64 比 36 约多十几题，每题约多 20 毫秒，"
  "约 2%–3%）。这是真实运行里同样会有的代价；全量 500 题后面的题活更重、更不缺并发，所以用这段的结论定全量的并发是偏保守的。")

summary["curves"] = curves
(OUT / "sweep-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
if curves:
    keys = list(dict.fromkeys(k for r in curves for k in r))
    with (OUT / "curves.csv").open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, keys)
        wr.writeheader()
        for r in curves:
            wr.writerow(r)
print("\n".join(out))
