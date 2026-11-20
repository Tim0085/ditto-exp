"""把阶段三的 cells.csv、pairs.csv 排成 docs\\18 里的 Markdown 表。只换格式，不做计算。

    python -I src/report_tables.py <阶段三结果目录>   → 打印到标准输出（UTF-8）
"""
import csv, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(sys.argv[1])
cells = list(csv.DictReader(open(OUT / "cells.csv", encoding="utf-8-sig")))
pairs = list(csv.DictReader(open(OUT / "pairs.csv", encoding="utf-8-sig")))
NAME = {"mono": "Monolithic", "split-memory": "拆 MEMORY", "split-context": "拆 CONTEXT", "split-both": "两个都拆"}
f = lambda r, k: float(r[k]) if r.get(k) not in (None, "") else None


def pct(v):
    if v is None:
        return "—"
    s = f"{v * 100:+.0f}%"
    return "0%" if s in ("+0%", "-0%") else s


def main_table(group, cores, layouts):
    print(f"\n**{group}，{cores} 核**：吞吐（题/秒）和每题用时 p95（秒），括号里是相对同轮 Monolithic 的变化\n")
    print("| 并发 | 轮 | " + " | ".join(NAME[l] for l in layouts) + " |")
    print("| --- | --- | " + " | ".join("---" for _ in layouts) + " |")
    rows = sorted({(int(c["concurrency"]), c["round"]) for c in cells if c["group"] == group and c["cores"] == str(cores)})
    for conc, rnd in rows:
        out = []
        for l in layouts:
            c = next((c for c in cells if c["group"] == group and c["cores"] == str(cores) and c["concurrency"] == str(conc) and c["round"] == rnd and c["layout"] == l), None)
            p = next((p for p in pairs if p["group"] == group and p["cores"] == str(cores) and p["concurrency"] == str(conc) and p["round"] == rnd and p["layout"] == l), None)
            if not c:
                out.append("—")
                continue
            thr, p95 = f(c, "throughput_per_s"), f(c, "latency_p95_ms")
            extra = f"（{pct(f(p, 'throughput_gain'))}，p95 {pct(f(p, 'p95_cut') and -f(p, 'p95_cut'))}）" if p else ""
            out.append(f"{thr:.2f} / {p95 / 1000:.1f}s{extra}")
        print(f"| {conc} | {rnd} | " + " | ".join(out) + " |")


def resource_table(group, cores, conc):
    print(f"\n**{group}，{cores} 核，并发 {conc}（第 1 轮）各进程资源**\n")
    print("| 布局 | 合计用核 | 编排器 主线程忙 / 用核 / 事件循环延迟 p99 | MEMORY 主线程忙 / 用核 / p99 | CONTEXT 主线程忙 / 用核 / p99 |")
    print("| --- | --- | --- | --- | --- |")
    for c in cells:
        if c["group"] != group or c["cores"] != str(cores) or c["concurrency"] != str(conc) or c["round"] != "r1":
            continue
        def proc(p):
            b, k, d = f(c, f"{p}_main_busy_share"), f(c, f"{p}_cores_used"), f(c, f"{p}_loop_delay_p99_ms")
            return "—" if b is None else f"{b * 100:.0f}% / {k:.2f} / {d:,.0f} ms"
        print(f"| {NAME[c['layout']]} | {f(c, 'total_cores_used'):.2f} | {proc('orchestrator')} | {proc('memory')} | {proc('context')} |")


def check_table():
    print("\n**全部格子的一致性和稳定性**\n")
    print("| 组 | 格子数 | 失败题数 | 真实模型调用 | 请求体与 Monolithic 相同 |")
    print("| --- | --- | --- | --- | --- |")
    for g in sorted({c["group"] for c in cells}):
        cs = [c for c in cells if c["group"] == g]
        same = [c["requests_same_as_mono"] for c in cs if c["layout"] != "mono"]
        # 没有对照、条数不同（“（拆开 N 个）”）都算不通过。
        ok = all(s and s != "无对照" and "（" not in s and s.split("/")[0] == s.split("/")[1] for s in same)
        print(f"| {g} | {len(cs)} | {sum(int(c['failed']) for c in cs)} | {sum(int(c['real_model_calls']) for c in cs)} | {'全部逐字相同' if ok else '有不同：' + '、'.join(same)}（{len(same)} 格） |")


groups = sorted({(c["group"], c["cores"]) for c in cells})
for g, k in groups:
    layouts = [l for l in NAME if any(c["group"] == g and c["cores"] == k and c["layout"] == l for c in cells)]
    main_table(g, int(k), layouts)
for g, k in groups:
    concs = sorted({int(c["concurrency"]) for c in cells if c["group"] == g and c["cores"] == k})
    resource_table(g, int(k), max(concs))
check_table()
