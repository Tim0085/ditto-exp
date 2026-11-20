"""阶段三：端到端原始记录 → 每格一行的表、拆开与 Monolithic 的成对比较。docs\\16 第 4 节阶段三。

    python -I src/export.py <阶段三原始记录目录> <输出目录>

每格：
- 吞吐：稳态窗口内完成的题数 / 窗口时长（窗口 = 第 warmup 题完成 → 最后一题派发，run.ts 里算好）；
- 每题用时 p50/p95/p99/平均：窗口内完成的题；
- 失败题数（全部题）；
- 各进程：主线程忙的比例、容器用掉的核数、事件循环延迟 p99、容器内存峰值；
- 一致性：同组、同轮、同并发下，拆开的格子发给模型的每个请求体和 Monolithic 的是否逐字相同（SHA-256）。
成对比较：同组、同轮、同并发、同核数，拆开相对 Monolithic 的吞吐提高、p95 缩短（pairs.csv，不含 Direct）。
有 Direct（不经过 Ditto 运行时，docs\\31）时另写 pairs-direct.csv：Monolithic 和各种拆法相对同一轮 Direct 的吞吐变化、p95 变化、
请求体是否逐字相同；cells.csv 另加一列 requests_same_as_direct。
有合并对照（split-memctx）和两个都拆（split-both）时另写 pairs-memctx.csv：合并对两个都拆。
三张比较表里，同一组有两轮时另加一行“两轮合并”：两轮吞吐之和的比（顺序前后对称，两轮合起来时间上的漂移抵消；单轮里配对的两格
相隔 1–5 格，含方向相反的漂移，主数用两轮合并）。
cells.csv 另有（全量实验，docs\\31）：
- 每题主线程忙多久、每题用掉的 CPU（各进程合计）：窗口里的总数除以窗口里完成的题数。Monolithic 对 Direct 的差别很小，看这两列比看吞吐准；
- 整台虚拟机的 CPU 忙的比例（看有没有别的重活）；
- 按派题的序号分段的吞吐（500 题一格里记忆库越来越大，负载一直在变；第 20 题做完到第 199 题派出这一段可以和以前 200 题的结果比）。
"""
import csv, json, statistics, sys
from pathlib import Path

RAW, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


cells, fakes = [], {}
for path in sorted(RAW.glob("*.json")):
    d = json.loads(path.read_text(encoding="utf-8"))
    m, w = d["meta"], d.get("window")
    group, _, rnd = m["label"].partition("|")
    cores = path.stem.split("-k")[-1].split("-")[0] if "-k" in path.stem else ""
    steady = [t for t in d["tasks"] if w and w["from_ms"] <= t["end_ms"] <= w["to_ms"] and t["ok"]]
    lat = [t["end_ms"] - t["start_ms"] for t in steady]
    row = {"cell": path.stem, "group": group, "round": rnd, "scenario": m["scenario"], "layout": m["layout"], "cores": cores,
           "concurrency": m["concurrency"], "tasks": len(d["tasks"]), "failed": sum(1 for t in d["tasks"] if not t["ok"]),
           "first_error": next((t.get("error") for t in d["tasks"] if not t["ok"]), ""),
           "steady_completed": len(steady), "throughput_per_s": w["throughput_per_s"] if w else None,
           "latency_mean_ms": statistics.mean(lat) if lat else None, "latency_p50_ms": pct(lat, 50),
           "latency_p95_ms": pct(lat, 95), "latency_p99_ms": pct(lat, 99), "window_s": (w["to_ms"] - w["from_ms"]) / 1000 if w else None,
           "fake_peak_active": d["fake"]["peakActive"], "fake_errors": d["fake"]["errors"], "real_model_calls": d["fake"]["realModelCalls"]}
    # rag：失败里有多少是模型输出过不了官方检查（run.ts 的 failKind）。只有 rag 有这一列，S4 的表和以前一样。
    if m["scenario"] == "rag":
        row["failed_model_output"] = sum(1 for t in d["tasks"] if not t["ok"] and t.get("failKind") == "model-output")
    procs = {"orchestrator": w["orchestrator"]} if w else {}
    if w:
        procs.update(w["remote"])
    # memctx：MEMORY 和 CONTEXT 放进同一个进程（docs\\23）；interaction：INTERACTION + RETRIEVAL（docs\\24）。Direct 只有编排器一个进程。
    for name in ["orchestrator", "memory", "context", "memctx", "interaction"]:
        p = procs.get(name)
        row[f"{name}_main_busy_share"] = p["main_busy_share"] if p else None
        row[f"{name}_cores_used"] = p["container_cores_used"] if p else None
        row[f"{name}_loop_delay_p99_ms"] = p["loopDelay"]["p99_ms"] if p and p.get("loopDelay") else None
        row[f"{name}_mem_peak_mb"] = p["container_mem_peak_mb"] if p else None
    row["total_cores_used"] = sum(p["container_cores_used"] or 0 for p in procs.values()) if procs else None
    # 每题主线程忙多久、每题用掉的 CPU（毫秒）：窗口里的总数 ÷ 窗口里完成的题数
    done = w["completed"] if w else 0
    for name in ["orchestrator", "memory", "context", "memctx", "interaction"]:
        p = procs.get(name)
        row[f"{name}_busy_ms_per_task"] = p["main_busy_ms"] / done if p and done else None
    cpu = [p.get("container_cpu_ms") for p in procs.values()]
    row["cpu_ms_per_task"] = sum(cpu) / done if procs and done and all(c is not None for c in cpu) else None
    row["vm_cpu_busy_share"] = (w["orchestrator"].get("vm_cpu_busy_share") if w else None)
    # 按派题的序号分段的吞吐：第 20 题做完 → 第 199 题派出 → 第 349 题派出 → 最后一题派出（只在 400 题以上的格子里算）
    if w and len(d["tasks"]) >= 400:
        start = {t["i"]: t["start_ms"] for t in d["tasks"]}
        edges = [("20_199", w["from_ms"], start[199]), ("199_349", start[199], start[349]), ("349_end", start[349], w["to_ms"])]
        for label, lo, hi in edges:
            n = sum(1 for t in d["tasks"] if t["ok"] and lo <= t["end_ms"] <= hi)
            row[f"throughput_seg_{label}"] = n / ((hi - lo) / 1000) if hi > lo else None
    cells.append(row)
    # 真实模型的出口按复位以后收到的记：上一格晚到的请求会混进这一格，按 cell 排除（假模型的记录没有 cell，全算）。
    fakes[path.stem] = {(r["task"], r["kind"]): r["sha256"] for r in d["fake"]["rows"] if r.get("cell", path.stem) == path.stem}

# 一致性和成对比较
key = lambda r: (r["group"], r["round"], r["concurrency"], r["cores"])
gain = lambda x, y: (x / y - 1) if x and y else None


def same_requests(a, b):
    """两格发给模型的请求逐字相同的个数。分母是两边的并集：只在一边出现的请求（例如 rag 只有一边重来了一次回答，
    记成 answer#2）单独列出。"""
    keys = set(a) | set(b)
    same = sum(1 for k in keys if k in a and k in b and a[k] == b[k])
    only = sum(1 for k in keys if (k in a) != (k in b))
    return f"{same}/{len(keys)}" + (f"（只在一边 {only} 个）" if only else "")


mono = {key(r): r for r in cells if r["layout"] == "mono"}
pairs = []
for r in cells:
    base = mono.get(key(r))
    if not base or r is base:
        r["requests_same_as_mono"] = "" if r["layout"] == "mono" else "无对照"
        continue
    r["requests_same_as_mono"] = same_requests(fakes[base["cell"]], fakes[r["cell"]])
    if r["layout"] == "direct":
        continue  # Direct 不是拆法，和 Monolithic 的比较在 pairs-direct.csv
    pairs.append({"group": r["group"], "round": r["round"], "concurrency": r["concurrency"], "cores": r["cores"], "layout": r["layout"],
                  "mono_throughput": base["throughput_per_s"], "split_throughput": r["throughput_per_s"],
                  "throughput_gain": gain(r["throughput_per_s"], base["throughput_per_s"]),
                  "mono_p95_ms": base["latency_p95_ms"], "split_p95_ms": r["latency_p95_ms"],
                  "p95_cut": (1 - r["latency_p95_ms"] / base["latency_p95_ms"]) if r["latency_p95_ms"] and base["latency_p95_ms"] else None,
                  "mono_mean_ms": base["latency_mean_ms"], "split_mean_ms": r["latency_mean_ms"],
                  "requests_same": r["requests_same_as_mono"], "failed_mono": base["failed"], "failed_split": r["failed"]})

# 对 Direct（docs\\31）：Monolithic 和各种拆法，相对同组、同轮、同并发、同核数的 Direct。throughput_gain 是 这一格 ÷ Direct − 1。
direct = {key(r): r for r in cells if r["layout"] == "direct"}
pairs_direct = []
for r in cells:
    base = direct.get(key(r))
    if not direct:
        break
    if not base or r is base:
        r["requests_same_as_direct"] = "" if r["layout"] == "direct" else "无对照"
        continue
    r["requests_same_as_direct"] = same_requests(fakes[base["cell"]], fakes[r["cell"]])
    pairs_direct.append({"group": r["group"], "round": r["round"], "concurrency": r["concurrency"], "cores": r["cores"], "layout": r["layout"],
                         "direct_throughput": base["throughput_per_s"], "layout_throughput": r["throughput_per_s"],
                         "throughput_gain": gain(r["throughput_per_s"], base["throughput_per_s"]),
                         "direct_p95_ms": base["latency_p95_ms"], "layout_p95_ms": r["latency_p95_ms"],
                         "p95_cut": (1 - r["latency_p95_ms"] / base["latency_p95_ms"]) if r["latency_p95_ms"] and base["latency_p95_ms"] else None,
                         "direct_mean_ms": base["latency_mean_ms"], "layout_mean_ms": r["latency_mean_ms"],
                         "requests_same": r["requests_same_as_direct"], "failed_direct": base["failed"], "failed_layout": r["failed"]})


def pooled(rows, base_key, layout_key):
    """同一组、同一并发、同一核数、同一布局有两轮以上时，加一行“两轮合并”：吞吐用各轮平均（即各轮吞吐之和的比），p95、平均用时也取各轮平均。"""
    out = []
    groups = {}
    for r in rows:
        groups.setdefault((r["group"], r["concurrency"], r["cores"], r["layout"]), []).append(r)
    for (group, conc, cores, layout), rs in groups.items():
        if len(rs) < 2 or any(r[f"{base_key}_throughput"] is None or r[f"{layout_key}_throughput"] is None for r in rs):
            continue
        mean = lambda k: statistics.mean(r[k] for r in rs) if all(r[k] is not None for r in rs) else None
        b, l = mean(f"{base_key}_throughput"), mean(f"{layout_key}_throughput")
        bp, lp = mean(f"{base_key}_p95_ms"), mean(f"{layout_key}_p95_ms")
        out.append({"group": group, "round": "两轮合并（" + "+".join(r["round"] for r in rs) + "）", "concurrency": conc, "cores": cores, "layout": layout,
                    f"{base_key}_throughput": b, f"{layout_key}_throughput": l, "throughput_gain": gain(l, b),
                    f"{base_key}_p95_ms": bp, f"{layout_key}_p95_ms": lp, "p95_cut": (1 - lp / bp) if lp and bp else None,
                    f"{base_key}_mean_ms": mean(f"{base_key}_mean_ms"), f"{layout_key}_mean_ms": mean(f"{layout_key}_mean_ms"),
                    "requests_same": "", f"failed_{base_key}": sum(r[f"failed_{base_key}"] for r in rs), f"failed_{layout_key}": sum(r[f"failed_{layout_key}"] for r in rs)})
    return out


pairs += pooled(pairs, "mono", "split")
pairs_direct += pooled(pairs_direct, "direct", "layout")

# 合并对照（docs\\31）：MEMORY 和 CONTEXT 合进一个进程（split-memctx）对各占一个进程（split-both），同一轮。
# 两者跨进程的调用完全一样，只差两个重活共用一条主线程还是各用一条。
both = {key(r): r for r in cells if r["layout"] == "split-both"}
pairs_memctx = []
for r in cells:
    base = both.get(key(r))
    if r["layout"] != "split-memctx" or not base:
        continue
    pairs_memctx.append({"group": r["group"], "round": r["round"], "concurrency": r["concurrency"], "cores": r["cores"], "layout": r["layout"],
                         "both_throughput": base["throughput_per_s"], "memctx_throughput": r["throughput_per_s"],
                         "throughput_gain": gain(r["throughput_per_s"], base["throughput_per_s"]),
                         "both_p95_ms": base["latency_p95_ms"], "memctx_p95_ms": r["latency_p95_ms"],
                         "p95_cut": (1 - r["latency_p95_ms"] / base["latency_p95_ms"]) if r["latency_p95_ms"] and base["latency_p95_ms"] else None,
                         "both_mean_ms": base["latency_mean_ms"], "memctx_mean_ms": r["latency_mean_ms"],
                         "requests_same": same_requests(fakes[base["cell"]], fakes[r["cell"]]), "failed_both": base["failed"], "failed_memctx": r["failed"]})
pairs_memctx += pooled(pairs_memctx, "both", "memctx")


def write(name, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with (OUT / name).open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, keys)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


write("cells.csv", cells)
write("pairs.csv", pairs)
write("pairs-direct.csv", pairs_direct)
write("pairs-memctx.csv", pairs_memctx)
summary = {"cells": len(cells), "failedTasks": sum(r["failed"] for r in cells), "realModelCalls": sum(r["real_model_calls"] for r in cells),
           "pairs": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()} for p in pairs],
           **({"pairsDirect": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()} for p in pairs_direct]} if pairs_direct else {}),
           **({"pairsMemctx": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()} for p in pairs_memctx]} if pairs_memctx else {})}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"cells": len(cells), "failed": summary["failedTasks"], "pairs": len(pairs), "pairsDirect": len(pairs_direct)}, ensure_ascii=False))
