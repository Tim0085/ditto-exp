"""S4 真实模型验证的补充核对（docs\\20 第 3.4、5.1 节用的数）。只读结果，不改数据。

    python -I src/real_check.py <真实运行结果目录> <假模型端到端结果目录>

1. 两种布局交给模型的内容是否一样：检索词相同的题，回答一步发给出口的请求是否逐字相同；检索词不同的题有多少。
   同一布局跑两次也算一遍，作参照。上一格晚到的请求会混进下一格的记录（出口按复位以后收到的记），按 cell 字段排除。
2. 每题主线程忙多久（稳态窗口里主线程忙的总时间 ÷ 完成题数），真实模型对假模型（2 核、并发 37）。
3. MEMORY 每题调用几次、每次多久；稳态窗口按时间分四段的吞吐。
格子从结果目录里认（布局-轮），按开跑时间排。
"""
import glob
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
REAL, FAKE = Path(sys.argv[1]), Path(sys.argv[2])
LAYOUT = {"mono": "Monolithic", "split-memory": "拆 MEMORY", "split-context": "拆 CONTEXT", "split-both": "两个都拆"}

raw, STEM = {}, {}
for p in (REAL / "raw").glob("*.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    c = f"{d['meta']['layout']}-{d['meta']['label'].partition('|')[2]}"
    raw[c], STEM[c] = d, p.stem
ORDER = sorted(raw, key=lambda c: raw[c]["meta"]["started"])
ROUNDS = sorted({c.rsplit("-", 1)[1] for c in ORDER})
SPLITS = sorted({raw[c]["meta"]["layout"] for c in ORDER} - {"mono"})


def load(c):
    d = raw[c]
    tasks = {f"{t['i']}:{t['id']}": t for t in d["tasks"] if t["ok"]}
    rows = {(r["task"], r["kind"]): r for r in d["fake"]["rows"] if r.get("cell", STEM[c]) == STEM[c]}
    late = sum(1 for r in d["fake"]["rows"] if r.get("cell", STEM[c]) != STEM[c])
    return tasks, rows, late


print("## 1. 两种布局交给模型的内容\n")
cells = {c: load(c) for c in ORDER}
for c, (_, _, late) in cells.items():
    if late:
        print(f"{c}：记录里有上一格晚到的请求 {late} 个（已排除）")
pairs = [(f"mono-{r}", f"{s}-{r}") for s in SPLITS for r in ROUNDS]
pairs += [(f"{l}-{ROUNDS[0]}", f"{l}-{ROUNDS[1]}") for l in ["mono", *SPLITS]] if len(ROUNDS) > 1 else []
norm = lambda t: [q.lower() for q in t["queries"]]
text = lambda t: (t.get("answer") or "").strip()
for a, b in pairs:
    if a not in cells or b not in cells:
        continue
    ta, ra, _ = cells[a]
    tb, rb, _ = cells[b]
    common = sorted(set(ta) & set(tb))
    same_req = lambda k: ra.get((k, "answer"), {}).get("sha256") is not None and ra[(k, "answer")]["sha256"] == rb.get((k, "answer"), {}).get("sha256")  # 两边都缺不算相同
    same_q = [k for k in common if norm(ta[k]) == norm(tb[k])]
    diff_q = [k for k in common if k not in same_q]
    req = [k for k in common if same_req(k)]
    print(f"{a} 对 {b}：共同 {len(common)} 题；检索词相同 {len(same_q)} 题，其中回答请求逐字相同 {sum(map(same_req, same_q))}；"
          f"检索词不同 {len(diff_q)} 题，其中回答请求仍相同 {sum(map(same_req, diff_q))}；"
          f"回答请求相同的 {len(req)} 题里，模型回答文字完全相同 {sum(1 for k in req if text(ta[k]) == text(tb[k]))} 题")


def busy(d):
    w = d["window"]
    n = w["completed"]
    procs = {"编排器": w["orchestrator"], **{k.upper() + " 进程": v for k, v in w["remote"].items()}}
    parts = [f"{k} {v['main_busy_ms'] / n:.0f} ms（忙 {v['main_busy_share']:.0%}，用 {v['container_cores_used']:.2f} 核）" for k, v in procs.items()]
    return f"吞吐 {w['throughput_per_s']:.3f}；每题主线程忙：" + "；".join(parts)


def memory_and_quarters(d):
    """MEMORY 每题调用几次、每次多久（本进程是执行时间，拆出去是往返）；稳态窗口按时间分四段的吞吐。"""
    w, walls = d["window"], d["nodeWalls"]
    lo, hi = w["from_ms"], w["to_ms"]
    seg = (hi - lo) / 4
    ends = [t["end_ms"] for t in d["tasks"] if t["ok"] and lo <= t["end_ms"] <= hi]
    quarters = [sum(1 for e in ends if lo + q * seg <= e < lo + (q + 1) * seg + (1e-6 if q == 3 else 0)) / (seg / 1000) for q in range(4)]
    mem = "；".join(f"{n} 每题 {walls[n]['calls'] / w['completed']:.1f} 次、平均 {walls[n]['mean_ms']:.0f} ms" for n in ("MEMORY.SEARCH", "MEMORY.WRITE") if n in walls)
    return f"{mem}；窗口四段吞吐 {' / '.join(f'{x:.2f}' for x in quarters)}"


fakes = {Path(p).stem: json.loads(Path(p).read_text(encoding="utf-8")) for p in sorted(glob.glob(str(FAKE / "raw" / "s4-u100-k2-c37-*.json")))}
print("\n## 2. 每题主线程忙多久（2 核、并发 37）\n")
for c in ORDER:
    print(f"真实  {c:<17} {busy(raw[c])}")
for name, d in fakes.items():
    print(f"假模型 {name:<30} {busy(d)}")
print("\n## 3. MEMORY 的调用和窗口内吞吐的变化（2 核、并发 37）\n")
for c in ORDER:
    print(f"真实  {c:<17} {memory_and_quarters(raw[c])}")
for name, d in fakes.items():
    print(f"假模型 {name:<30} {memory_and_quarters(d)}")
