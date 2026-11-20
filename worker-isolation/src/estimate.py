"""阶段二：由阶段一的单题测量推算拆开的收益。docs\\16 第 4 节阶段二。

    python -I src/estimate.py <阶段一原始记录目录> <输出目录>

每个配置先算每题的“需求”（毫秒）：
- 目标 Worker：MEMORY.*（S1）、CONTEXT.*（S2、S3）；S4 两个都算；
- 其余 Worker：留在编排器进程里的节点（INFER，以及 S1 里的 CONTEXT）；
- 编排器本身：一题的忙时间 − 各节点的忙时间（Ditto 的 graph/loop 调度、校验等）。
每题有几遍计时（同一容器的 3 遍，或 3 个新容器各 1 遍，文件名 <配置>.p1/.p2/.p3）：先挑“一题忙时间”居中的那一遍，
各项都取这一遍的值，几项之间才对得上。
需求分两种：忙的时间（主线程被占住的时间，含同步等磁盘，决定排队）和 CPU 时间（决定核数够不够）。

拆开以后多出的跨进程开销，用阶段一单独那一遍量到的序列化时间：
- 调用方（编排器）：入参 stringify + 返回 parse；
- 被拆的 Worker：入参 parse + 返回 stringify ×2（官方 handler 先 stringify 一次核对大小，再 stringify 一次发送）；
- 每次调用另加固定的 HTTP 开销 HTTP_MS（两端各一份，试 0.1、0.3 毫秒两档）。

推算用封闭排队网络的均值分析（MVA）：N 个任务（并发数）在“模型等待”（延迟站，平均 Z）和各进程主线程
（单服务台、先到先服务，需求 = 忙的时间）之间循环。Monolithic 只有一个主线程站；拆开时每个进程一个站。
再按 CPU 时间检查总 CPU 不超过核数，超过就按核数封顶。Z 按每题实际调模型的次数算：S1 写入不调模型，Z = 0。
判据（跑之前写定）：并发 8、16、37 中任一档，吞吐提高或平均用时缩短达到 10%。
"""
import json, math, re, statistics, sys, csv
from pathlib import Path

RAW, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

# 假模型延迟：理解问题中位 1 秒、回答中位 2 秒，对数正态 sigma 0.5（docs\16 阶段二）。均值 = 中位 × e^(σ²/2)。
SIGMA = 0.5
Z_BY_SCENARIO = {s: (1000 + 2000) * math.exp(SIGMA ** 2 / 2) for s in ["s1-qa", "s2", "s3", "s3-full", "s4"]}
Z_BY_SCENARIO["s1-write"] = 0.0
# rag（docs\\24）：官方 rag-qa 每题 4 次模型调用，理解问题、挑证据、核对各中位 1 秒，回答 2 秒（fake-model.ts）。
# 没有检索到块的题会少调 2 次（挑证据、回答、核对按官方流程跳过），这里按全部调用算，偏保守（模型等待长，收益偏小）。
Z_BY_SCENARIO["rag"] = (1000 * 3 + 2000) * math.exp(SIGMA ** 2 / 2)
CONCURRENCY = [8, 16, 37]
HTTP_MS = [0.1, 0.3]
TARGETS = {"s1-qa": ["MEMORY"], "s1-write": ["MEMORY"], "s2": ["CONTEXT"], "s3": ["CONTEXT"], "s3-full": ["CONTEXT"], "s4": ["MEMORY", "CONTEXT"],
           "rag": ["INTERACTION", "RETRIEVAL", "MEMORY"]}
# 每种布局拆出去几个进程、每个进程里有哪些 Worker。单目标场景是 split-<目标>。
LAYOUTS = {
    "s4": {"split-memory": [["MEMORY"]], "split-context": [["CONTEXT"]], "split-both": [["MEMORY"], ["CONTEXT"]],
           "split-memctx": [["MEMORY", "CONTEXT"]]},  # docs\\23：MEMORY 和 CONTEXT 放进同一个独立进程
    "rag": {"split-interaction": [["INTERACTION", "RETRIEVAL"]],  # docs\\24：官方 rag-qa 里两者共用一份题库
            "split-interaction-memory": [["INTERACTION", "RETRIEVAL"], ["MEMORY"]]},
}


def load(paths):
    """一个配置的全部文件 → 每题一行（中位的那一遍）。"""
    passes, serde, meta = {}, {}, None
    for k, path in enumerate(sorted(paths)):
        data = json.loads(path.read_text(encoding="utf-8"))
        meta, reps = data["meta"], data["meta"]["reps"]
        if any(not t["ok"] for t in data["tasks"]):
            raise SystemExit(f"{path.name} 有失败的题，不推算")
        for t in data["tasks"]:
            if t["rep"] < reps:
                passes[(t["i"], k, t["rep"])] = {"busy": t["busy_ms"] - t["measure_ms"], "cpu": t["thread_cpu_ms"],
                                                  "wall": t["end_ms"] - t["start_ms"], "w_busy": {}, "w_cpu": {}, "calls": {}}
        for n in data["nodes"]:
            w = n["node"].split(".")[0]
            if n["rep"] < reps:
                p = passes[(n["job"], k, n["rep"])]
                p["w_busy"][w] = p["w_busy"].get(w, 0) + n["busy_ms"]
                p["w_cpu"][w] = p["w_cpu"].get(w, 0) + n["thread_cpu_ms"]
                p["calls"][w] = p["calls"].get(w, 0) + 1
            elif "in_bytes" in n and k == 0:  # 字节和序列化只取第一个文件的那一遍
                s = serde.setdefault(n["job"], {}).setdefault(w, {"caller": 0.0, "remote": 0.0, "bytes": 0})
                s["caller"] += n["in_stringify_ms"] + n["out_parse_ms"]
                s["remote"] += n["in_parse_ms"] + 2 * n["out_stringify_ms"]
                s["bytes"] += n["in_bytes"] + n["out_bytes"]
    by_task = {}
    for (i, _, _), p in passes.items():
        by_task.setdefault(i, []).append(p)
    tasks = []
    for i, ps in sorted(by_task.items()):
        ps = sorted(ps, key=lambda p: p["busy"])
        p = ps[(len(ps) - 1) // 2]  # 中位的那一遍（偶数遍取偏小的一个）
        tasks.append({"i": i, **p, "passes": [round(q["busy"], 1) for q in ps], "serde": serde.get(i, {})})
    return meta, tasks


def mva(demands, n, z):
    """单服务台 FCFS 站点的精确均值分析。demands：各站每题需求（毫秒）。返回吞吐（题/毫秒）和平均用时（毫秒，不含 Z）。"""
    q = [0.0] * len(demands)
    x = r_total = 0.0
    for k in range(1, n + 1):
        r = [d * (1 + qi) for d, qi in zip(demands, q)]
        r_total = sum(r)
        x = k / (z + r_total)
        q = [x * ri for ri in r]
    return x, r_total


def capped(busy, cpu, n, z, cores):
    x, r = mva(busy, n, z)
    if x * sum(cpu) > cores:  # 按 CPU 时间算总核数，超过就封顶
        x = cores / sum(cpu)
        r = n / x - z
    return x, r


mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
groups = {}
for path in sorted(RAW.glob("*.json")):
    m = re.match(r"^(.*?)(?:\.p\d)?$", path.stem)
    groups.setdefault(m.group(1), []).append(path)

results, demand_rows = [], []
for name, paths in groups.items():
    meta, tasks = load(paths)
    scenario, z = meta["scenario"], Z_BY_SCENARIO[meta["scenario"]]
    targets = TARGETS[scenario]
    T = lambda key, w: mean([t[key].get(w, 0) for t in tasks])
    S = lambda key, w: mean([t["serde"].get(w, {}).get(key, 0) for t in tasks])
    orch_busy = mean([max(0.0, t["busy"] - sum(t["w_busy"].values())) for t in tasks])
    orch_cpu = mean([max(0.0, t["cpu"] - sum(t["w_cpu"].values())) for t in tasks])
    others_busy = mean([sum(v for w, v in t["w_busy"].items() if w not in targets) for t in tasks])
    others_cpu = mean([sum(v for w, v in t["w_cpu"].items() if w not in targets) for t in tasks])
    tb = {w: T("w_busy", w) for w in targets}
    tc = {w: T("w_cpu", w) for w in targets}
    calls = {w: T("calls", w) for w in targets}
    caller, remote = {w: S("caller", w) for w in targets}, {w: S("remote", w) for w in targets}
    mono_busy = orch_busy + others_busy + sum(tb.values())
    mono_cpu = orch_cpu + others_cpu + sum(tc.values())
    spread = [max(t["passes"]) / min(t["passes"]) for t in tasks if min(t["passes"]) > 0]
    demand_rows.append({"config": name, "scenario": scenario, "files": len(paths), "tasks": len(tasks),
                        "passes_per_task": len(tasks[0]["passes"]), "pass_spread_median": statistics.median(spread) if spread else None,
                        "task_wall_ms": mean([t["wall"] for t in tasks]), "task_busy_ms": mono_busy, "task_cpu_ms": mono_cpu,
                        "orchestrator_ms": orch_busy, "other_workers_ms": others_busy,
                        **{f"{w}_ms": tb[w] for w in targets}, **{f"{w}_cpu_ms": tc[w] for w in targets}, **{f"{w}_calls": calls[w] for w in targets},
                        **{f"{w}_transfer_kb": S("bytes", w) / 1024 for w in targets},
                        **{f"{w}_caller_serde_ms": caller[w] for w in targets}, **{f"{w}_worker_serde_ms": remote[w] for w in targets},
                        "target_share": sum(tb.values()) / mono_busy if mono_busy else 0, "model_wait_ms": z})
    layouts = LAYOUTS.get(scenario) or {"split-" + targets[0].lower(): [[targets[0]]]}
    for http in HTTP_MS:
        for layout, groups in layouts.items():
            split = [w for g in groups for w in g]
            stay = [w for w in targets if w not in split]
            extra = sum(caller[w] + http * calls[w] for w in split)
            o_busy = orch_busy + others_busy + sum(tb[w] for w in stay) + extra
            o_cpu = orch_cpu + others_cpu + sum(tc[w] for w in stay) + extra
            # 同一个进程里的几个 Worker 共用一条主线程：需求相加。
            s_busy = [o_busy] + [sum(tb[w] + remote[w] + http * calls[w] for w in g) for g in groups]
            s_cpu = [o_cpu] + [sum(tc[w] + remote[w] + http * calls[w] for w in g) for g in groups]
            for cores in ([2, 3] if len(groups) == 2 else [2]):
                for n in CONCURRENCY:
                    xm, rm = capped([mono_busy], [mono_cpu], n, z, cores)
                    xs, rs = capped(s_busy, s_cpu, n, z, cores)
                    results.append({"config": name, "scenario": scenario, "layout": layout, "cores": cores, "http_ms": http, "concurrency": n,
                                    "mono_demand_ms": mono_busy, "split_demands_ms": "/".join(f"{d:.1f}" for d in s_busy),
                                    "mono_tps": xm * 1000, "split_tps": xs * 1000, "throughput_gain": xs / xm - 1,
                                    "mono_latency_ms": rm + z, "split_latency_ms": rs + z, "latency_cut": 1 - (rs + z) / (rm + z),
                                    "upper_bound_gain": mono_busy / max(s_busy) - 1})


def write(name, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with (OUT / name).open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


write("demands.csv", demand_rows)
write("estimate.csv", results)
best = {}
for r in results:
    if r["http_ms"] != 0.3:  # 判据用保守的一档
        continue
    k = f'{r["config"]}|{r["layout"]}|{r["cores"]}核'
    gain = max(r["throughput_gain"], r["latency_cut"])
    if gain > best.get(k, {"gain": -9})["gain"]:
        best[k] = {"gain": round(gain, 4), "at_concurrency": r["concurrency"], "throughput_gain": round(r["throughput_gain"], 4),
                   "latency_cut": round(r["latency_cut"], 4), "passes": gain >= 0.10}
(OUT / "estimate-summary.json").write_text(json.dumps({"model_wait_ms": Z_BY_SCENARIO, "criterion": 0.10, "http_ms_for_verdict": 0.3,
    "best_by_config": dict(sorted(best.items(), key=lambda kv: -kv[1]["gain"]))}, ensure_ascii=False, indent=1), encoding="utf-8")
for k, v in sorted(best.items(), key=lambda kv: -kv[1]["gain"]):
    print(f'{k:40s} {v["gain"]:+.3f} @c{v["at_concurrency"]} {"达到" if v["passes"] else "未达到"}')
