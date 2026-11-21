r"""11-21 汇报（汇报_20261121）的结果表格和图：全量实验（S4、rag-qa，各 500 题，并发 36）为主，附低负载对照（并发 2）和离线并发扫描。
每张表、每张图对应 阶段结果11-21.md 的一处。只读结果，不改。

    python -I build_report_1121.py <sec1-2.0 目录> <汇报目录>

写 <汇报目录>\结果表格11-21.xlsx 和 <汇报目录>\图\ 下的 3 张图（吞吐收益、S4分段、并发扫描），并打印正文要用的数。
图里不写图号和图名，图注写在正文里，删图、调顺序时不用重画。
读的结果：
- results\full-s4-500-s42、results\full-rag-500-s42：全量实验（种子 42）；结论.json 由 report\full_criteria.py 写；
- results\s4-real-low-s42：低负载对照（并发 2，前 200 题）；判据.json 由 report\low_criteria.py 写；
- results\conc-sweep-s20261119：离线并发扫描（模拟模型），curves.csv。
吞吐和收益用 cells.csv 里的数，和 结论.json、判据.json 同一来源，逐一核对。正确率配对用 src\judge.py 的 diff_ci、sign_test 从 judged.csv
逐题重算，和各次运行的 accuracy.json 逐一核对；Holm 校正后的 p 和 结论.json 核对。对不上就停。
分段按派题的序号：第 20 次完成 → 第 200 道题派出 → 第 350 道题派出 → 最后一道题派出，和 src\export.py 的 throughput_seg_* 相同；
各段主线程忙的时间由每 5 秒一次的读数线性插值得到。
"""
import csv
import gzip
import importlib.util
import json
import math
import re
import statistics
import sys
import unicodedata
import warnings
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
if len(sys.argv) != 3:
    raise SystemExit("用法：python -I build_report_1121.py <sec1-2.0 目录> <汇报目录>")
SEC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
R = SEC / "results"
S4, RAG, LOW, SWEEP = R / "full-s4-500-s42", R / "full-rag-500-s42", R / "s4-real-low-s42", R / "conc-sweep-s20261119"
_spec = importlib.util.spec_from_file_location("judge", SEC / "03_memctx_probe" / "src" / "judge.py")
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)  # 只用 diff_ci、sign_test

LAYOUT = {"mono": "Monolithic", "direct": "Direct", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT", "split-both": "两个都隔离",
          "split-memctx": "合并", "split-interaction": "隔离 INTERACTION+RETRIEVAL"}
PROC = {"orchestrator": "编排器", "memory": "MEMORY 进程", "context": "CONTEXT 进程", "memctx": "合并进程", "interaction": "INTERACTION 进程"}
TYPE = {"single-session-user": "单段对话：用户说的事实", "single-session-assistant": "单段对话：助手说的内容",
        "single-session-preference": "单段对话：按用户偏好作答", "multi-session": "跨多段对话汇总", "temporal-reasoning": "时间推理",
        "knowledge-update": "知识更新（以最新的为准）"}
S4_SPLITS = ("split-memory", "split-both", "split-context", "split-memctx")
ROUNDS = ("r1", "r2")
TWO, PHASE = "throughput_per_s", "throughput_phase_avg"
SEGS = ("throughput_seg_20_199", "throughput_seg_199_349", "throughput_seg_349_end")
SEG = ("第 1 段", "第 2 段", "第 3 段")
BJ = timezone(timedelta(hours=8))
WINDOW = "稳态窗口：第 20 次完成到最后一道题派出"
SEG_NOTE = "分段按派题序号：第 1 段为第 20 次完成至第 200 道题派出，第 2 段至第 350 道题派出，第 3 段至最后一道题派出"
CI_NOTE = "逐题配对；“仅 A 答对 / 仅 B 答对”为两边判分不同的题数，区间为 Agresti–Min 95% 区间，p 为双侧符号检验"

csv_rows = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig", newline="")))
num = lambda v: None if v in (None, "") else float(v)
# 四舍五入按十进制写法进位：round() 按二进制值舍入，cells.csv 里 1.3495 这类数的二进制略小于写出来的值，会被舍成 1.349
to_dec = lambda v: Decimal(repr(float(v)))
half_up = lambda d, n: d.quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)
rnd = lambda v, n=1: None if v is None else float(half_up(to_dec(v), n))
pc = lambda v, n=1: None if v is None else float(half_up(to_dec(v) * 100, n))


# ================================================================ 读数
def load(d):
    """{(布局, 轮): 格子}，按开跑的先后排。格子 = raw 里的原始记录 + cells.csv 的那一行；出口记录只取这一格自己的。"""
    by_stem = {c["cell"]: c for c in csv_rows(d / "cells.csv")}
    out = {}
    for p in sorted((d / "raw").glob("*.json")):
        x = json.loads(p.read_text(encoding="utf-8"))
        m = x["meta"]
        key = (m["layout"], m["label"].partition("|")[2])
        if key in out:
            raise SystemExit(f"{d.name}：{p.stem} 和另一格的布局、轮相同")
        out[key] = {"stem": p.stem, "meta": m, "tasks": x["tasks"], "window": x["window"], "samples": x.get("samples") or [],
                    "csv": by_stem[p.stem], "rows": [r for r in x["fake"]["rows"] if r.get("cell", p.stem) == p.stem],
                    "calls": p.with_name(p.stem + ".calls.json.gz")}
    return dict(sorted(out.items(), key=lambda kv: kv[1]["meta"]["started"]))


def started(cell):
    return datetime.fromisoformat(cell["meta"]["started"].replace("Z", "+00:00")).astimezone(BJ).strftime("%H:%M")


def tp(cells, layout, r, key=PHASE):
    v = num(cells[(layout, r)]["csv"][key])
    if v is None:
        raise SystemExit(f"{cells[(layout, r)]['stem']} 的 {key} 是空的")
    return v


def gain(cells, base, layout, key, rounds):
    """layout 对 base 的吞吐收益：rounds 里各轮吞吐之和的比 − 1（单轮就是那一轮的比）。"""
    return sum(tp(cells, layout, r, key) for r in rounds) / sum(tp(cells, base, r, key) for r in rounds) - 1


def gains(cells, base, layout):
    out = {r: {"two": gain(cells, base, layout, TWO, [r]), "phase": gain(cells, base, layout, PHASE, [r])} for r in ROUNDS}
    out["pooled"] = {"two": gain(cells, base, layout, TWO, ROUNDS), "phase": gain(cells, base, layout, PHASE, ROUNDS)}
    return out


def seg_bounds(cell):
    w = cell["window"]
    start = {t["i"]: t["start_ms"] for t in cell["tasks"]}
    return [w["from_ms"], start[199], start[349], w["to_ms"]]


def seg_done(cell, b):
    return [sum(1 for t in cell["tasks"] if t["ok"] and lo <= t["end_ms"] <= hi) for lo, hi in zip(b, b[1:])]


def interp(xs, x):
    ts = [a for a, _ in xs]
    i = bisect_left(ts, x)
    if i <= 0:
        return xs[0][1]
    if i >= len(xs):
        return xs[-1][1]
    (a0, b0), (a1, b1) = xs[i - 1], xs[i]
    return b0 + (b1 - b0) * (x - a0) / (a1 - a0)


def busy_series(cell):
    """各进程主线程累计忙的毫秒（事件循环活跃时间）随时间的读数，时间换到题的时间轴上。"""
    w, ss = cell["window"], cell["samples"]
    off = w["from_unix_ms"] - w["from_ms"]
    procs = {"orchestrator": [(s["at_ms"] - off, s["local"].get("elu_active_ms")) for s in ss]}
    for k in sorted({k for s in ss for k in (s.get("remote") or {})}):
        procs[k] = [(s["at_ms"] - off, (((s.get("remote") or {}).get(k) or {}).get("snapshot") or {}).get("elu_active_ms")) for s in ss]
    return {k: [(a, v) for a, v in xs if v is not None] for k, xs in procs.items()}


def seg_busy(cell):
    """→ 分段边界、各段完成的题数、{进程: [(忙的比例, 每题忙的毫秒), ×3]}。另和整个窗口的读数核对（插值误差）。"""
    b = seg_bounds(cell)
    done = seg_done(cell, b)
    out, worst = {}, 0.0
    w = cell["window"]
    procs = {"orchestrator": w["orchestrator"], **w["remote"]}
    for k, xs in busy_series(cell).items():
        out[k] = [((interp(xs, hi) - interp(xs, lo)) / (hi - lo), (interp(xs, hi) - interp(xs, lo)) / n) for (lo, hi), n in zip(zip(b, b[1:]), done)]
        whole = interp(xs, w["to_ms"]) - interp(xs, w["from_ms"])
        worst = max(worst, abs(whole / procs[k]["main_busy_ms"] - 1))
    if worst > 0.03:
        raise SystemExit(f"{cell['stem']}：按读数插值的窗口主线程时间和窗口记录差 {worst:.1%}")
    return b, done, out, worst


def step_median(cell, kind):
    xs = [r["latency_ms"] for r in cell["rows"] if r.get("ok") and r.get("latency_ms") is not None and r["kind"].split("#")[0] == kind]
    return statistics.median(xs) / 1000 if xs else None


def same_requests(A, B, kind):
    """两格同一步（第一次发出的那次）发给模型的请求逐字相同的题数 / 两边都有的题数（比的是出口加防缓存编号之前的请求体）。"""
    a = {r["task"]: r["sha256"] for r in A["rows"] if r["kind"] == kind}
    b = {r["task"]: r["sha256"] for r in B["rows"] if r["kind"] == kind}
    common = set(a) & set(b)
    return sum(a[k] == b[k] for k in common), len(common)


def labels_of(d):
    """judged.csv → {(布局, 轮): {序号: 判对}}；rag 里模型输出不合格的题 judge.py 已记成判错。"""
    lab = defaultdict(dict)
    for r in csv_rows(d / "judge" / "judged.csv"):
        if r["判对"] in ("True", "False"):
            lab[(r["布局"], r["轮"])][int(r["序号"])] = r["判对"] == "True"
    return lab


def paired(specs):
    """specs：[(A 的 {序号: 判对}, B 的 {序号: 判对}), ...]，逐题配对后加起来。"""
    n = ra = rb = b = c = 0
    for A, B in specs:
        for i, x in A.items():
            y = B.get(i)
            if y is None:
                continue
            n += 1
            ra += x
            rb += y
            b += x and not y
            c += y and not x
    d, lo, hi = judge.diff_ci(b, c, n)
    return {"n": n, "a": ra / n, "b": rb / n, "only_a": b, "only_b": c, "diff": d, "lo": lo, "hi": hi, "p": judge.sign_test(b, c)}


def check_accuracy_json(d, lab):
    """每个单轮比较，和这次运行 judge.py 写的 accuracy.json 逐一核对。"""
    n = 0
    for p in json.loads((d / "judge" / "accuracy.json").read_text(encoding="utf-8"))["pairs"]:
        if "+" in p["A"] or not p["共同题数"]:
            continue
        (la, ra), (lb, rb) = p["A"].split(" "), p["B"].split(" ")
        got = paired([(lab[(la, ra)], lab[(lb, rb)])])
        want = {"n": p["共同题数"], "a": p["A正确率"], "b": p["B正确率"], "only_a": p["只有A对"], "only_b": p["只有B对"], "diff": p["正确率差B减A"],
                "lo": p["差的95%区间下限"], "hi": p["差的95%区间上限"], "p": p["符号检验p"]}
        bad = [k for k in want if abs(float(got[k]) - float(want[k])) > 1e-4]
        if bad:
            raise SystemExit(f"{d.name} {p['A']} 对 {p['B']}：重算的 {bad} 和 accuracy.json 对不上：{got} / {want}")
        n += 1
    return n


def holm(ps):
    m, running, adj = len(ps), 0.0, {}
    for i, (k, p) in enumerate(sorted(ps.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def accuracy(d, cells, splits):
    """族内（拆开对 Monolithic，每种拆法 × 每轮）配对和 Holm；另有两轮合并、对 Direct、同一布局两轮之间（只作描述）。"""
    lab = labels_of(d)
    checked = check_accuracy_json(d, lab)
    fam = {(s, r): paired([(lab[("mono", r)], lab[(s, r)])]) for s in splits for r in ROUNDS}
    adj = holm({k: v["p"] for k, v in fam.items()})
    for k in fam:
        fam[k]["p_holm"] = adj[k]
    concl = json.loads((d / "结论.json").read_text(encoding="utf-8"))["accuracy"]["rows"]
    for (s, r), v in fam.items():
        c = concl[f"{s} {r}"]
        if abs(c["p"] - v["p"]) > 1e-12 or abs(c["p_holm"] - v["p_holm"]) > 1e-12 or abs(c["diff"] - v["diff"]) > 1e-4:
            raise SystemExit(f"{d.name} {s} {r}：重算的正确率配对和 结论.json 对不上")
    layouts = [l for l, _ in cells]
    pooled = {s: paired([(lab[("mono", r)], lab[(s, r)]) for r in ROUNDS]) for s in splits}
    vs_direct = {(l, r): paired([(lab[("direct", r)], lab[(l, r)])]) for l in dict.fromkeys(layouts) if l != "direct" for r in ROUNDS} if "direct" in layouts else {}
    twice = {l: paired([(lab[(l, "r1")], lab[(l, "r2")])]) for l in dict.fromkeys(layouts)}
    return {"fam": fam, "pooled": pooled, "vs_direct": vs_direct, "twice": twice, "checked": checked, "lab": lab}


def per_type(d):
    """judged.csv → {布局: {题型: (判对, 题次)}}，两轮合起来；“无法回答”的题（题号带 _abs）单列。"""
    out = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    counts = Counter()
    first = None
    for r in csv_rows(d / "judge" / "judged.csv"):
        t = r["题型"] + ("（无法回答）" if r["题号"].endswith("_abs") else "")
        out[r["布局"]][t][0] += r["判对"] == "True"
        out[r["布局"]][t][1] += 1
        first = first or (r["布局"], r["轮"])
        if (r["布局"], r["轮"]) == first:
            counts[t] += 1
    return out, counts


def run_record(d):
    log = (d / "run.log").read_text(encoding="utf-8", errors="replace").splitlines()
    stamps = [m.group(1) for line in log if (m := re.match(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", line))]
    rep = (d / "report.md").read_text(encoding="utf-8")
    m = re.search(r"全部（含冒烟）：(\d+) 个模型请求，成功 (\d+).*?已结清 ([\d.]+) 元.*?折算约 ([\d.]+) 元", rep)
    a = json.loads((d / "judge" / "accuracy.json").read_text(encoding="utf-8"))
    u = a.get("judge_usage_this_run") or {}
    return {"from": stamps[0], "to": stamps[-1], "requests": int(m.group(1)), "ok": int(m.group(2)), "peak": float(m.group(3)), "actual": float(m.group(4)),
            "judge_calls": a.get("judge_calls_this_run"), "judge_failures": a.get("judge_failures"), "judge_model": a.get("judge_model_returned"),
            "tok_in": u.get("prompt_tokens"), "tok_out": u.get("completion_tokens"),
            "usd": (u.get("prompt_tokens") or 0) * 2.5e-6 + (u.get("completion_tokens") or 0) * 10e-6}


s4, rag, low = load(S4), load(RAG), load(LOW)
if len(s4) != 12 or len(rag) != 6 or len(low) != 8:
    raise SystemExit(f"格子数不对：S4 {len(s4)}，rag {len(rag)}，低负载 {len(low)}")
G = {s: gains(s4, "mono", s) for s in S4_SPLITS}
GD = {l: gains(s4, "direct", l) for l in ("mono", *S4_SPLITS)}
GM = gains(s4, "split-both", "split-memctx")
RG = gains(rag, "mono", "split-interaction")
RGD = {l: gains(rag, "direct", l) for l in ("mono", "split-interaction")}
for d, mine in ((S4, {**G}), (RAG, {"split-interaction": RG})):
    concl = json.loads((d / "结论.json").read_text(encoding="utf-8"))["gains_vs_mono"]
    for s, g in mine.items():
        for r in (*ROUNDS, "pooled"):
            for k in ("two", "phase"):
                if abs(concl[s][r][k] - g[r][k]) > 1e-12:
                    raise SystemExit(f"{d.name} {s} {r} {k}：这里算的收益和 结论.json 对不上")
A4, AR = accuracy(S4, s4, S4_SPLITS), accuracy(RAG, rag, ("split-interaction",))
SB4 = {k: seg_busy(c) for k, c in s4.items()}
SBR = {k: seg_busy(c) for k, c in rag.items()}

# 分段收益（两轮合并）和各布局最忙那条主线程每题忙的时间（两轮合起来：两轮忙的时间之和 ÷ 两轮完成的题数之和）
seg_gain = {s: [sum(num(s4[(s, r)]["csv"][k]) for r in ROUNDS) / sum(num(s4[("mono", r)]["csv"][k]) for r in ROUNDS) - 1 for k in SEGS] for s in S4_SPLITS}
seg_gain_direct = [sum(num(s4[("direct", r)]["csv"][k]) for r in ROUNDS) / sum(num(s4[("mono", r)]["csv"][k]) for r in ROUNDS) - 1 for k in SEGS]


def bottleneck(layout, cells=s4, sb=SB4):
    """每段最忙的进程，和它两轮合起来每题忙的毫秒。"""
    out = []
    for i in range(3):
        best = None
        for r in ROUNDS:
            _, done, procs, _ = sb[(layout, r)]
            k = max(procs, key=lambda p: procs[p][i][0])
            best = best or k
            if k != best:
                raise SystemExit(f"{layout} 第 {i + 1} 段两轮最忙的进程不同：{best}、{k}")
        busy_ms = sum(sb[(layout, r)][2][best][i][1] * sb[(layout, r)][1][i] for r in ROUNDS)
        out.append((best, busy_ms / sum(sb[(layout, r)][1][i] for r in ROUNDS)))
    return out


BOTT = {l: bottleneck(l) for l in ("mono", *S4_SPLITS)}

# rag：每段各节点每题调用几次、每次平均多久（只看在本进程里执行的调用）
NODES = ("RETRIEVAL.SEARCH", "INTERACTION.ACT.TOOL", "INFER.REASONING.SAMPLE")


def rag_nodes(cell):
    b = seg_bounds(cell)
    done = seg_done(cell, b)
    off = cell["window"]["from_unix_ms"] - cell["window"]["from_ms"]
    ub = [x + off for x in b]
    calls = json.loads(gzip.decompress(cell["calls"].read_bytes()))
    agg = {n: [[0, 0.0] for _ in range(3)] for n in NODES}
    for pr in (calls["orchestrator"], *calls["remote"].values()):
        for node, start, ms, _task, kind, *_rest in pr["rows"]:
            name = pr["nodes"][node]
            if kind != 0 or name not in agg:
                continue
            for i, (lo, hi) in enumerate(zip(ub, ub[1:])):
                if lo <= start < hi:
                    agg[name][i][0] += 1
                    agg[name][i][1] += ms
                    break
    return {n: [(c / done[i], (s / c) if c else None) for i, (c, s) in enumerate(v)] for n, v in agg.items()}


RN = {k: rag_nodes(c) for k, c in rag.items()}

# 低负载对照（并发 2）：判据.json
LJ = json.loads((LOW / "判据.json").read_text(encoding="utf-8"))
check_accuracy_json(LOW, labels_of(LOW))

# 离线并发扫描：curves.csv，各遍平均之比
sw = defaultdict(list)
for r in csv_rows(SWEEP / "curves.csv"):
    sw[(r["scenario"], r["layout"], int(r["concurrency"]))].append(float(r["throughput_phase_avg"]))
SW_CONC = sorted({c for _, _, c in sw})
SWG = {(sc, l): {c: statistics.fmean(sw[(sc, l, c)]) / statistics.fmean(sw[(sc, "mono", c)]) - 1 for c in SW_CONC if (sc, l, c) in sw}
       for sc, l, _ in sw if l != "mono"}

# ================================================================ 表格
wb = Workbook()
wb.remove(wb.active)
HEAD = PatternFill("solid", fgColor="E3EDF6")


# 显示宽度：中文、全角符号和宋体里画成全角的符号（÷、−、– 等）算 2，其余算 1；Excel 列宽的单位约是一个半角数字
dw = lambda s: sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in str(s))


def sheet(name, note, header, rows):
    """第 1 行注释合并到整张表的宽度，自动换行、竖直居中，行高按字数算（Excel 不会给合并的格子自动调行高）；
    第 2 行表头最多折成两行；数值一行放下。"""
    ws = wb.create_sheet(name)
    ws.append([note])
    ws.append(header)
    for r in rows:
        ws.append(r)
    widths = []
    for i, h in enumerate(header, 1):
        longest = max([math.ceil(dw(h) / 2)] + [dw(r[i - 1]) for r in rows if i - 1 < len(r) and r[i - 1] is not None])
        widths.append(min(40, max(8, longest + 2)))
        ws.column_dimensions[get_column_letter(i)].width = widths[-1]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(header))
    ws["A1"].font = Font(italic=True, color="5A6577")
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 15 * math.ceil(dw(note) / (sum(widths) * 0.9))
    for c in ws[2]:
        c.font = Font(bold=True)
        c.fill = HEAD
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 15 * max(2, *[math.ceil(dw(h) / (w - 1)) for h, w in zip(header, widths)])
    ws.freeze_panes = "A3"


# 表 1 题型
s4_type, type_counts = per_type(S4)
rag_type, rag_counts = per_type(RAG)
if type_counts != rag_counts or sum(type_counts.values()) != 500:
    raise SystemExit(f"两个场景的题型分布不同或不是 500 题：{type_counts} / {rag_counts}")
TYPE_ORDER = [t for t in ("single-session-user", "single-session-assistant", "single-session-preference", "multi-session", "temporal-reasoning", "knowledge-update")]
rows = []
for t in TYPE_ORDER:
    rows.append([t, TYPE[t], type_counts[t] + type_counts[t + "（无法回答）"], type_counts[t + "（无法回答）"]])
rows.append(["合计", "", sum(type_counts.values()), sum(v for k, v in type_counts.items() if k.endswith("（无法回答）"))])
sheet("表1 题型", "LongMemEval_S（cleaned）全部 500 题。“无法回答”题：历史对话中没有答案，正确的回答是指出无法回答。",
      ["题型", "说明", "题数", "其中“无法回答”题"], rows)


def cell_rows(cells, sb, procs, extra):
    rows = []
    for n, ((l, r), c) in enumerate(cells.items(), 1):
        w, x = c["window"], c["csv"]
        acc = {a["格子"]: a for a in csv_rows(c["calls"].parent.parent / "judge" / "accuracy.csv")}[c["stem"]]
        busy = {"orchestrator": w["orchestrator"], **w["remote"]}
        cols = []
        for p in procs:
            v = busy.get(p)
            cols += [rnd(v["main_busy_ms"] / w["completed"], 0) if v else None, pc(v["main_busy_share"], 0) if v else None]
        failed = int(float(x["failed"]))
        rows.append([n, started(c), LAYOUT[l], r, f"{int(float(x['tasks'])) - failed} / {failed}", rnd(num(x[TWO]), 3), rnd(num(x[PHASE]), 3),
                     *[rnd(num(x[k]), 3) for k in SEGS], rnd(num(x["latency_p50_ms"]) / 1000), rnd(num(x["latency_p95_ms"]) / 1000), *cols,
                     rnd(num(x["cpu_ms_per_task"]), 0), *extra(c), pc(float(acc["正确率"]))])
    return rows


proc_head = lambda procs: [h for p in procs for h in (f"{PROC[p]}：每题主线程忙（毫秒）", f"{PROC[p]}：忙的比例（%）")]
S4_PROCS = ("orchestrator", "memory", "context", "memctx")
sheet("表2 S4_每格", f"2 核，并发 36，每格 500 题。{WINDOW}，分段见表 4；每题主线程忙 = 窗口内主线程忙的时间 ÷ 窗口内完成的题数。",
      ["开跑顺序", "开始（北京时间）", "布局", "轮", "跑完 / 失败", "吞吐：两点法（题/秒）", "吞吐：整波平均（题/秒）", *[f"{s}吞吐（题/秒）" for s in SEG],
       "每题用时 p50（秒）", "每题用时 p95（秒）", *proc_head(S4_PROCS), "每题 CPU，各进程合计（毫秒）",
       "模型用时中位：检索词一步（秒）", "模型用时中位：回答一步（秒）", "正确率（%）"],
      cell_rows(s4, SB4, S4_PROCS, lambda c: [rnd(step_median(c, "keyword"), 2), rnd(step_median(c, "answer"), 2)]))


def gain_rows(groups):
    rows = []
    for kind, base, results, cells in groups:
        for l, g in results.items():
            for r in (*ROUNDS, "pooled"):
                b = sum(tp(cells, base, x) for x in (ROUNDS if r == "pooled" else [r])) / (2 if r == "pooled" else 1)
                v = sum(tp(cells, l, x) for x in (ROUNDS if r == "pooled" else [r])) / (2 if r == "pooled" else 1)
                rows.append([kind, LAYOUT[l], "两轮合并" if r == "pooled" else r, rnd(b, 3), rnd(v, 3), pc(g[r]["phase"], 2), pc(g[r]["two"], 2)])
    return rows


sheet("表3 S4_吞吐收益", "收益 = 这一布局的吞吐 ÷ 基准布局同一轮的吞吐 − 1；两轮合并为两轮吞吐之和的比，吞吐列为两轮平均。主数为整波平均的两轮合并。",
      ["比较", "布局", "轮", "基准吞吐：整波平均（题/秒）", "这一布局：整波平均（题/秒）", "收益：整波平均（%）", "收益：两点法（%）"],
      gain_rows([("对 Monolithic", "mono", G, s4), ("对 Direct", "direct", GD, s4), ("合并对两个都隔离", "split-both", {"split-memctx": GM}, s4)]))

rows = []
for l in ("direct", *S4_SPLITS):
    gs = seg_gain_direct if l == "direct" else seg_gain[l]
    for i, k in enumerate(SEGS):
        rows.append([LAYOUT[l], SEG[i], rnd(num(s4[(l, "r1")]["csv"][k]), 3), rnd(num(s4[(l, "r2")]["csv"][k]), 3),
                     rnd(num(s4[("mono", "r1")]["csv"][k]), 3), rnd(num(s4[("mono", "r2")]["csv"][k]), 3), pc(gs[i], 1)])
sheet("表4 S4_分段收益", f"{SEG_NOTE}。分段吞吐 = 段内完成的题数 ÷ 段长；收益按两轮合并。",
      ["布局", "段", "这一布局 r1（题/秒）", "这一布局 r2（题/秒）", "Monolithic r1（题/秒）", "Monolithic r2（题/秒）", "对 Monolithic 的收益（%）"], rows)

rows = []
worst_interp = 0.0
for (l, r), (b, done, procs, worst) in SB4.items():
    worst_interp = max(worst_interp, worst)
    for p, segs in procs.items():
        rows.append([LAYOUT[l], r, PROC[p], *[pc(s[0], 0) for s in segs], *[rnd(s[1], 0) for s in segs], *done])
for l in ("mono", *S4_SPLITS):
    rows.append([LAYOUT[l], "两轮合起来", "最忙的进程：" + "、".join(dict.fromkeys(PROC[p] for p, _ in BOTT[l])), None, None, None, *[rnd(ms, 0) for _, ms in BOTT[l]], None, None, None])
sheet("表5 S4_分段主线程", "分段同表 4。忙的比例 = 段内主线程忙的时间 ÷ 段长，每题忙 = 段内主线程忙的时间 ÷ 段内完成的题数，由每 5 秒的读数插值得到。"
      "最后几行为各布局每段最忙的进程，两轮合计。",
      ["布局", "轮", "进程", *[f"{s}：忙的比例（%）" for s in SEG], *[f"{s}：每题忙（毫秒）" for s in SEG], *[f"{s}：完成的题" for s in SEG]], rows)


def acc_rows(A, splits, scen_layouts):
    rows = []
    for (s, r), v in A["fam"].items():
        rows.append(["族内：拆开对 Monolithic", f"Monolithic {r}", f"{LAYOUT[s]} {r}", v["n"], pc(v["a"]), pc(v["b"]), v["only_a"], v["only_b"],
                     pc(v["diff"]), pc(v["lo"]), pc(v["hi"]), rnd(v["p"], 4), rnd(v["p_holm"], 4)])
    for s, v in A["pooled"].items():
        rows.append(["描述：两轮合并（同一题算两次）", "Monolithic r1 + r2", f"{LAYOUT[s]} r1 + r2", v["n"], pc(v["a"]), pc(v["b"]), v["only_a"], v["only_b"],
                     pc(v["diff"]), pc(v["lo"]), pc(v["hi"]), rnd(v["p"], 4), None])
    for (l, r), v in A["vs_direct"].items():
        rows.append(["描述：对 Direct", f"Direct {r}", f"{LAYOUT[l]} {r}", v["n"], pc(v["a"]), pc(v["b"]), v["only_a"], v["only_b"],
                     pc(v["diff"]), pc(v["lo"]), pc(v["hi"]), rnd(v["p"], 4), None])
    for l, v in A["twice"].items():
        rows.append(["参照：同一布局两轮之间", f"{LAYOUT[l]} r1", f"{LAYOUT[l]} r2", v["n"], pc(v["a"]), pc(v["b"]), v["only_a"], v["only_b"],
                     pc(v["diff"]), pc(v["lo"]), pc(v["hi"]), rnd(v["p"], 4), None])
    return rows


ACC_HEAD = ["比较", "A", "B", "共同题数", "A 正确率（%）", "B 正确率（%）", "仅 A 答对", "仅 B 答对", "差 B − A（百分点）", "95% 区间下限", "95% 区间上限",
            "符号检验 p", "Holm 校正后 p（族内）"]
sheet("表6 S4_正确率配对", f"{CI_NOTE}。族内 8 组一起做 Holm 校正，其余行只作描述。",
      ACC_HEAD, acc_rows(A4, S4_SPLITS, s4))

rows = []
for s in S4_SPLITS:
    for r in ROUNDS:
        k1, n1 = same_requests(s4[("mono", r)], s4[(s, r)], "keyword")
        k2, n2 = same_requests(s4[("mono", r)], s4[(s, r)], "answer")
        rows.append(["拆开对 Monolithic", f"{LAYOUT[s]} {r}", f"{k1} / {n1}", f"{k2} / {n2}"])
for r in ROUNDS:
    k1, n1 = same_requests(s4[("direct", r)], s4[("mono", r)], "keyword")
    k2, n2 = same_requests(s4[("direct", r)], s4[("mono", r)], "answer")
    rows.append(["Monolithic 对 Direct", r, f"{k1} / {n1}", f"{k2} / {n2}"])
for l in ("mono", "direct", *S4_SPLITS):
    k1, n1 = same_requests(s4[(l, "r1")], s4[(l, "r2")], "keyword")
    k2, n2 = same_requests(s4[(l, "r1")], s4[(l, "r2")], "answer")
    rows.append(["同一布局两轮之间", LAYOUT[l], f"{k1} / {n1}", f"{k2} / {n2}"])
S4_SAME = rows
sheet("表7 S4_模型输入", "比较 Ditto 发给模型的请求体（SHA-256，不含防缓存编号）。检索词一步只由题目决定；回答一步还取决于上一步模型给出的检索词。",
      ["比较", "格子", "检索词一步：相同 / 共同题数", "回答一步：相同 / 共同题数"], rows)

# rag
RAG_PROCS = ("orchestrator", "interaction")


def rag_extra(c):
    st = Counter(t.get("ragStatus") for t in c["tasks"] if t["ok"])
    return [*[rnd(step_median(c, k), 2) for k in ("plan", "select", "answer", "verify")],
            st["answered"], st["insufficient-evidence"], st["conflicting-evidence"], st["needs-clarification"],
            sum(1 for t in c["tasks"] if t.get("failKind") == "model-output")]


sheet("表8 rag_每格", "2 核，并发 36，每格 500 题；窗口和分段同表 2、表 4。未通过官方输出检查的题（模型输出不合格）算失败，不计入吞吐，判分按答错。",
      ["开跑顺序", "开始（北京时间）", "布局", "轮", "跑完 / 失败", "吞吐：两点法（题/秒）", "吞吐：整波平均（题/秒）", *[f"{s}吞吐（题/秒）" for s in SEG],
       "每题用时 p50（秒）", "每题用时 p95（秒）", *proc_head(RAG_PROCS), "每题 CPU，各进程合计（毫秒）",
       "模型用时中位：理解问题（秒）", "挑证据（秒）", "回答（秒）", "核对（秒）", "状态：有结论", "证据不足", "证据冲突", "要澄清", "模型输出不合格", "正确率（%）"],
      cell_rows(rag, SBR, RAG_PROCS, rag_extra))
sheet("表9 rag_吞吐收益", "同表 3。", ["比较", "布局", "轮", "基准吞吐：整波平均（题/秒）", "这一布局：整波平均（题/秒）", "收益：整波平均（%）", "收益：两点法（%）"],
      gain_rows([("对 Monolithic", "mono", {"split-interaction": RG}, rag), ("对 Direct", "direct", RGD, rag)]))
sheet("表10 rag_正确率配对", "比法同表 6。族内 2 组一起做 Holm 校正；模型输出不合格的题按答错计。", ACC_HEAD,
      acc_rows(AR, ("split-interaction",), rag))

rows = []
for (l, r), c in rag.items():
    b, done, procs, _ = SBR[(l, r)]
    bott = "interaction" if "interaction" in procs else "orchestrator"
    for i in range(3):
        rows.append([LAYOUT[l], r, SEG[i], rnd(num(c["csv"][SEGS[i]]), 3), pc(procs["orchestrator"][i][0], 0),
                     pc(procs["interaction"][i][0], 0) if "interaction" in procs else None, PROC[bott], rnd(procs[bott][i][1], 0),
                     *[x for n in NODES for x in (rnd(RN[(l, r)][n][i][0], 2), rnd(RN[(l, r)][n][i][1], 0))]])
RAG_DIAG = rows
sheet("表11 rag_分段", "分段同表 4。各节点只计在本进程执行的调用（拆开时 RETRIEVAL.SEARCH、INTERACTION.ACT.TOOL 在 INTERACTION 进程执行）；"
      "INTERACTION.ACT.TOOL 的用时含排队等主线程的时间。",
      ["布局", "轮", "段", "段吞吐（题/秒）", "编排器忙（%）", "INTERACTION 进程忙（%）", "最忙的进程", "它每题忙（毫秒）",
       "RETRIEVAL.SEARCH：每题次数", "每次（毫秒）", "INTERACTION.ACT.TOOL：每题次数", "每次（毫秒）", "模型调用：每题次数", "每次（毫秒）"], rows)

rows = []
for r in ROUNDS:
    for kind in ("plan", "select", "answer", "verify"):
        k, n = same_requests(rag[("mono", r)], rag[("split-interaction", r)], kind)
        rows.append(["拆开对 Monolithic", r, kind, f"{k} / {n}"])
for kind in ("plan", "select", "answer", "verify"):
    k, n = same_requests(rag[("mono", "r1")], rag[("mono", "r2")], kind)
    rows.append(["Monolithic 两轮之间", "", kind, f"{k} / {n}"])
RAG_SAME = rows
sheet("表12 rag_模型输入", "每题 4 步模型调用：理解问题（plan）、挑证据（select）、回答（answer）、核对（verify）。比法同表 7；只比每一步第一次发出的请求。",
      ["比较", "轮", "步骤", "相同 / 共同题数"], rows)

# 低负载对照
rows = []
for k, v in LJ["cells"].items():
    l, r = k.split(" ")
    c = low[(l, r)]["csv"]
    rows.append([LAYOUT[l], r, pc(v["busy"], 1), rnd(num(c[TWO]), 3), rnd(num(c[PHASE]), 3), v["n"], rnd(v["T_ms"], 0), rnd(v["m_ms"], 0), rnd(v["R_ms"], 0)])
sheet("表13 低负载_每格", "S4，并发 2，每格为全量实验的前 200 题。m = 每题等模型的时间（两步模型调用之和），R = 每题用时 − m。",
      ["布局", "轮", "编排器主线程忙（%）", "吞吐：两点法（题/秒）", "吞吐：整波平均（题/秒）", "窗口里完成的题", "每题用时（毫秒）", "m（毫秒）", "R（毫秒）"],
      sorted(rows, key=lambda x: (list(LAYOUT.values()).index(x[0]), x[1])))
rows = []
for s, g in LJ["gains_vs_mono"].items():
    for r in (*ROUNDS, "pooled"):
        rows.append(["对 Monolithic", LAYOUT[s], "两轮合并" if r == "pooled" else r, pc(g[r]["phase"], 2), pc(g[r]["two"], 2), pc(g[r]["adjusted"], 2)])
for r in (*ROUNDS, "pooled"):
    g = LJ["mono_vs_direct"][r]
    rows.append(["Monolithic 对 Direct", "Monolithic", "两轮合并" if r == "pooled" else r, pc(g["phase"], 2), pc(g["two"], 2), pc(g["adjusted"], 2)])
for k, v in LJ["accuracy"]["rows"].items():
    s, r = k.split(" ")
    rows.append(["正确率：拆开对 Monolithic", LAYOUT[s], r, None, None, None, pc(v["diff"]), pc(v["lo"]), pc(v["hi"]), rnd(v["p"], 4), rnd(v["p_holm"], 4)])
sheet("表14 低负载_收益与正确率", "收益的主数为整波平均的两轮合并；扣除模型用时波动后的收益 = (Monolithic 的 R + M) ÷ (拆开的 R + M) − 1，M 为几格 m 的平均（m、R 见表 13）。"
      "正确率比法同表 6，4 组一起做 Holm 校正。",
      ["项目", "布局", "轮", "收益：整波平均（%）", "收益：两点法（%）", "收益：扣除模型用时波动以后（%）", "正确率差（百分点）", "95% 区间下限", "95% 区间上限",
       "符号检验 p", "Holm 校正后 p"], rows)

spread = lambda v: pc(max(v) / min(v) - 1, 1) if len(v) > 1 else None
rows = []
for sc in ("s4", "rag"):
    rows.append([("S4" if sc == "s4" else "rag-qa"), "Monolithic 吞吐（题/秒，整波平均）", *[rnd(statistics.fmean(sw[(sc, "mono", c)]), 3) if (sc, "mono", c) in sw else None for c in SW_CONC]])
    for l in ("direct", *S4_SPLITS, "split-interaction"):
        if (sc, l) in SWG:
            rows.append([("S4" if sc == "s4" else "rag-qa"), f"{LAYOUT[l]} 对 Monolithic（%）", *[pc(SWG[(sc, l)].get(c), 1) for c in SW_CONC]])
    # 同一布局、同一并发重复运行的波动：各遍吞吐的最大值 ÷ 最小值 − 1；只有一遍的档留空。用 get，不往 defaultdict 里加空键
    for l in ("mono", "direct", *S4_SPLITS, "split-interaction"):
        if any(len(sw.get((sc, l, c), [])) > 1 for c in SW_CONC):
            rows.append([("S4" if sc == "s4" else "rag-qa"), f"{LAYOUT[l]}：各遍最大差（%）", *[spread(sw.get((sc, l, c), [])) for c in SW_CONC]])
SW_SPREAD = [r for r in rows if "各遍最大差" in r[1]]
sheet("表15 离线并发扫描", "模拟模型（每题每步的延迟按题固定），2 核，原种子 20261119 的题，每格 200 + 并发 道题；吞吐为整波平均，收益为各遍平均之比。"
      "并发 8 起每档两遍，rag-qa 的 Monolithic、Direct 在并发 36、48、64 为四遍。各遍最大差 = 各遍吞吐的最大值 ÷ 最小值 − 1。",
      ["场景", "项目", *[f"并发 {c}" for c in SW_CONC]], rows)

rows = []
for sc, data, layouts in (("S4", s4_type, ("mono", "direct", *S4_SPLITS)), ("rag-qa", rag_type, ("mono", "direct", "split-interaction"))):
    for t in [*TYPE_ORDER, *[x + "（无法回答）" for x in TYPE_ORDER]]:
        if type_counts[t] == 0:
            continue
        rows.append([sc, t, type_counts[t], *[pc(data[l][t][0] / data[l][t][1]) for l in layouts]] + [None] * (6 - len(layouts)))
sheet("表16 分题型正确率", "两轮合计（题次 = 题数 × 2）。S4 的 6 列依次为 Monolithic、Direct、隔离 MEMORY、两个都隔离、隔离 CONTEXT、合并；"
      "rag-qa 的前 3 列依次为 Monolithic、Direct、隔离 INTERACTION+RETRIEVAL。",
      ["场景", "题型", "题数", "布局 1（%）", "布局 2（%）", "布局 3（%）", "布局 4（%）", "布局 5（%）", "布局 6（%）"], rows)

REC = {"S4 全量": run_record(S4), "rag-qa 全量": run_record(RAG), "S4 低负载对照": run_record(LOW)}
rows = [[k, v["from"], v["to"], v["requests"], v["ok"], v["peak"], v["actual"], v["judge_calls"], v["judge_failures"], "、".join(v["judge_model"]),
         v["tok_in"], v["tok_out"], rnd(v["usd"], 2)] for k, v in REC.items()]
sheet("表17 运行记录", "DeepSeek 费用为出口按 token 记的账，按高峰价，另按发出时刻折算（11-21 为周六，全天半价），未与账单核对。"
      "GPT-4o 按输入 2.5、输出 10 美元每百万 token 估算。",
      ["运行", "开始", "结束", "模型请求（含冒烟）", "成功", "DeepSeek：按高峰价（元）", "DeepSeek：按发出时刻（元）", "判分请求", "判分失败", "判分返回的模型",
       "判分输入 token", "判分输出 token", "GPT-4o 估算（美元）"], rows)


# 表 18 rag-qa 两轮的差别：各格瓶颈进程在稳态窗口里的 CPU 和内存
def proc_mem(cell, proc):
    """窗口里每 5 秒的读数：堆的中位和最大、RSS 的中位、相邻两次读数堆下降 50 MB 以上的次数（大回收的粗略计数）。"""
    w = cell["window"]
    snaps = [s["local"] if proc == "orchestrator" else ((s.get("remote") or {}).get(proc) or {}).get("snapshot") for s in cell["samples"]]
    snaps = [x for x in snaps if x and w["from_unix_ms"] <= x["at_ms"] <= w["to_unix_ms"]]
    heap = [x["heap_mb"] for x in snaps]
    return statistics.median(heap), max(heap), statistics.median(x["rss_mb"] for x in snaps), sum(1 for a, b in zip(heap, heap[1:]) if a - b > 50)


rows = []
for (l, r), c in rag.items():
    w = c["window"]
    p = "interaction" if "interaction" in (w.get("remote") or {}) else "orchestrator"
    v = w["orchestrator"] if p == "orchestrator" else w["remote"][p]
    n = w["completed"]
    hmed, hmax, rmed, drops = proc_mem(c, p)
    rows.append([LAYOUT[l], r, rnd(num(c["csv"][PHASE]), 3), PROC[p], rnd(v["main_busy_ms"] / n, 0), rnd(v["main_thread_cpu_ms"] / n, 0),
                 rnd((v["process_cpu_ms"] - v["main_thread_cpu_ms"]) / n, 0), rnd(hmed, 0), rnd(hmax, 0), rnd(rmed, 0), drops, rnd(v["loopDelay"]["max_ms"], 0)])
RAG_VAR = rows
sheet("表18 rag_两轮的差别", "各格瓶颈进程在稳态窗口内的读数。每题 = 窗口内的总量 ÷ 窗口内完成的题数；其他线程 CPU = 进程 CPU − 主线程 CPU，主要是垃圾回收和编译线程；"
      "堆、RSS 取每 5 秒的读数，堆回落 = 相邻两次读数下降 50 MB 以上的次数。",
      ["布局", "轮", "吞吐：整波平均（题/秒）", "瓶颈进程", "每题主线程忙（毫秒）", "每题主线程 CPU（毫秒）", "每题其他线程 CPU（毫秒）",
       "堆：中位（MB）", "堆：最大（MB）", "RSS：中位（MB）", "堆回落 50 MB 以上（次）", "事件循环最长卡顿（毫秒）"], rows)

OUT.mkdir(parents=True, exist_ok=True)
wb.save(OUT / "结果表格11-21.xlsx")

# ================================================================ 图
INK, INK2, MUTED, GRID, AXIS, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
COLOR = {"split-memory": "#2a78d6", "split-both": "#eb6834", "split-context": "#1baf7a", "split-memctx": "#eda100", "split-interaction": "#e87ba4"}
font_manager.fontManager.addfont(r"C:\Windows\Fonts\msyh.ttc")
plt.rcParams.update({"font.family": "Microsoft YaHei", "font.size": 9, "axes.unicode_minus": False, "figure.facecolor": SURF, "axes.facecolor": SURF,
                     "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "xtick.major.size": 0, "ytick.major.size": 0, "text.color": INK, "legend.frameon": False, "savefig.dpi": 200})
minus = lambda s: s.replace("-", "−")
signed = lambda v, n=1: minus(f"{half_up(to_dec(v), n):+f}%")
FIG = OUT / "图"
FIG.mkdir(exist_ok=True)


def style(ax, xgrid=False, ygrid=False):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    if xgrid:
        ax.grid(axis="x", color=GRID, lw=0.6)
    if ygrid:
        ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def save(fig, name):
    fig.savefig(FIG / name, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    # 吞吐收益（点图）
    rows = [(f"S4：{LAYOUT[s]}", s, G[s]) for s in S4_SPLITS] + [("rag-qa：隔离 INTERACTION+RETRIEVAL", "split-interaction", RG)]
    ys = [6, 5, 4, 3, 1.4]
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    ax.axvline(0, color=AXIS, lw=0.9, zorder=1)
    ax.axhline(2.2, color=GRID, lw=0.6, zorder=1)
    for y, (lab, s, g) in zip(ys, rows):
        a, b, p = g["r1"]["phase"] * 100, g["r2"]["phase"] * 100, g["pooled"]["phase"] * 100
        ax.plot([a, b], [y, y], color=MUTED, lw=1.2, zorder=2, solid_capstyle="butt")
        ax.plot([a, b], [y, y], ls="none", marker="|", ms=11, mec=MUTED, mew=1.6, zorder=3)
        ax.plot([p], [y], ls="none", marker="o", ms=10, mfc=COLOR[s], mec=SURF, mew=1.6, zorder=4)
        ax.text(max(a, b, p) + 1.6, y, signed(p), va="center", ha="left", color=INK, fontsize=9)
    ax.set_yticks(ys, [lab for lab, _, _ in rows])
    ax.set_ylim(0.6, 6.7)
    ax.set_xlim(-15, 47)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: minus(f"{v:+.0f}%") if v else "0"))
    style(ax, xgrid=True)
    ax.set_xlabel("吞吐收益（相对同一轮的 Monolithic，整波平均）")
    ax.legend(handles=[Line2D([0], [0], ls="none", marker="o", ms=9, mfc=INK2, mec=SURF, label="两轮合并（主数）"),
                       Line2D([0], [0], color=MUTED, lw=1.2, marker="|", ms=10, mew=1.6, label="两轮各自的值（r1、r2）")],
              loc="upper left", fontsize=8.5)
    fig.tight_layout()
    save(fig, "吞吐收益.png")

    # S4 分段收益和最忙那条主线程每题忙的时间
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8.2, 3.5))
    x = [1, 2, 3]
    for s in S4_SPLITS:
        a1.plot(x, [v * 100 for v in seg_gain[s]], color=COLOR[s], lw=1.8, marker="o", ms=6.5, mec=SURF, mew=1.3, zorder=3)
    a1.axhline(0, color=AXIS, lw=0.9)
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: minus(f"{v:+.0f}%") if v else "0"))
    a1.set_title("拆开对 Monolithic 的吞吐收益", loc="left", fontsize=9.5, color=INK)
    a2.plot(x, [ms for _, ms in BOTT["mono"]], color=INK2, lw=1.8, marker="o", ms=6.5, mec=SURF, mew=1.3, zorder=3)
    for s in S4_SPLITS:
        a2.plot(x, [ms for _, ms in BOTT[s]], color=COLOR[s], lw=1.8, marker="o", ms=6.5, mec=SURF, mew=1.3, zorder=3)
    a2.set_ylim(0, None)
    a2.set_title("最忙那条主线程每题忙的时间（毫秒）", loc="left", fontsize=9.5, color=INK)
    for ax in (a1, a2):
        ax.set_xticks(x, ["第 1 段", "第 2 段", "第 3 段"])
        ax.set_xlim(0.7, 3.3)
        style(ax, ygrid=True)
    handles = [Line2D([0], [0], color=INK2, lw=1.8, marker="o", ms=6, mec=SURF, label="Monolithic（编排器）")]
    handles += [Line2D([0], [0], color=COLOR[s], lw=1.8, marker="o", ms=6, mec=SURF,
                       label=f"{LAYOUT[s]}（{'、'.join(dict.fromkeys(PROC[p] for p, _ in BOTT[s]))}）") for s in S4_SPLITS]
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=8.3, bbox_to_anchor=(0.5, -0.13))
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    save(fig, "S4分段.png")

    # 离线并发扫描
    conc = [c for c in SW_CONC if c != 37]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    for sc, s in [("s4", l) for l in S4_SPLITS] + [("rag", "split-interaction")]:
        ax.plot(conc, [SWG[(sc, s)][c] * 100 for c in conc], color=COLOR[s], lw=1.8, marker="o" if sc == "s4" else "s", ms=6, mec=SURF, mew=1.2, zorder=3,
                label=("S4：" if sc == "s4" else "rag-qa：") + LAYOUT[s])
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(conc))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.axhline(0, color=AXIS, lw=0.9)
    ax.axvline(36, color=MUTED, lw=0.8, zorder=1)
    ax.set_ylim(-20, 60)
    ax.text(36 * 1.04, 58, "正式实验取并发 36", color=INK2, fontsize=8.3, va="top")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: minus(f"{v:+.0f}%") if v else "0"))
    style(ax, ygrid=True)
    ax.set_xlabel("并发（同时在做的题数）")
    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8.3)
    fig.tight_layout()
    save(fig, "并发扫描.png")
glyph = [str(w.message) for w in caught if "Glyph" in str(w.message) or "missing" in str(w.message)]
if glyph:
    raise SystemExit(f"字体缺字：{glyph[:3]}")

# ================================================================ 打印正文要用的数
print(json.dumps({n: wb[n].max_row - 2 for n in wb.sheetnames}, ensure_ascii=False))
print("正确率配对和 accuracy.json 核对的单轮比较：S4", A4["checked"], "组，rag", AR["checked"], "组；分段插值和窗口记录最多差", f"{worst_interp:.2%}")
# 收益印三位小数、p 印五位：比正文多两位，免得把 16.248 先看成 16.25、再舍入成 16.3
print("S4 收益（整波平均）：", {LAYOUT[s]: [signed(G[s][r]["phase"] * 100, 3) for r in (*ROUNDS, "pooled")] for s in S4_SPLITS},
      "两点法合并：", {LAYOUT[s]: signed(G[s]["pooled"]["two"] * 100, 3) for s in S4_SPLITS})
print("合并对两个都隔离：", [signed(GM[r]["phase"] * 100, 3) for r in (*ROUNDS, "pooled")], "Monolithic 对 Direct：", [signed(GD["mono"][r]["phase"] * 100, 3) for r in (*ROUNDS, "pooled")])
print("S4 分段收益：", {LAYOUT[s]: [signed(v * 100, 3) for v in seg_gain[s]] for s in S4_SPLITS}, "Direct：", [signed(v * 100, 3) for v in seg_gain_direct])
print("S4 最忙主线程每题（毫秒）：", {LAYOUT[l]: [(PROC[p], int(rnd(ms, 0))) for p, ms in BOTT[l]] for l in BOTT})
for (l, r), (b, done, procs, _) in SB4.items():
    if l != "direct":
        print("  ", LAYOUT[l], r, "完成", done, {PROC[p]: [f"{pc(s[0], 0):.0f}%" for s in v] for p, v in procs.items()})
print("rag 收益：", [signed(RG[r]["phase"] * 100, 3) for r in (*ROUNDS, "pooled")], "两点法", signed(RG["pooled"]["two"] * 100, 3),
      "Monolithic 对 Direct：", [signed(RGD["mono"][r]["phase"] * 100, 3) for r in (*ROUNDS, "pooled")])
for row in RAG_DIAG:
    print("  rag", row)
print("S4 正确率族内：", {f"{LAYOUT[s]} {r}": (pc(v["diff"]), rnd(v["p"], 5), rnd(v["p_holm"], 5)) for (s, r), v in A4["fam"].items()})
print("S4 两轮之间：", {LAYOUT[l]: pc(v["diff"]) for l, v in A4["twice"].items()}, "合并：", {LAYOUT[s]: (pc(v["diff"]), pc(v["lo"]), pc(v["hi"])) for s, v in A4["pooled"].items()})
print("rag 正确率族内：", {r: (pc(v["diff"]), rnd(v["p"], 5), rnd(v["p_holm"], 5)) for (s, r), v in AR["fam"].items()},
      "两轮之间：", {LAYOUT[l]: (pc(v["diff"]), rnd(v["p"], 5)) for l, v in AR["twice"].items()})
print("S4 模型输入：", S4_SAME)
print("rag 模型输入：", RAG_SAME)
print("题型：", dict(type_counts))
print("分题型（S4 Monolithic）：", {t: rnd(s4_type["mono"][t][0] / s4_type["mono"][t][1] * 100) for t in s4_type["mono"]})
print("分题型（rag Monolithic）：", {t: rnd(rag_type["mono"][t][0] / rag_type["mono"][t][1] * 100) for t in rag_type["mono"]})
print("离线扫描（S4、rag 对 Monolithic，%）：", {f"{sc} {LAYOUT[l]}": {c: pc(v) for c, v in g.items()} for (sc, l), g in SWG.items()})
print("运行记录：", REC)
print("rag 两轮的差别（表 18）：", RAG_VAR)
print("离线扫描各遍最大差（表 15）：", SW_SPREAD)
