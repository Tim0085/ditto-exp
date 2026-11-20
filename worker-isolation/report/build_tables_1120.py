r"""11-20 汇报的结果表格（汇报_20261120\结果表格11-20.xlsx）：只放 11-20 的新结果，每张表对应 阶段结果11-20.md 的一处。只读结果，不改。

    python -I build_tables_1120.py <sec1-2.0 目录> <输出 xlsx>

读的结果：
- S4 拆 CONTEXT：results\memctx-real-s4-ctx2（11-20 凌晨，第二次）、results\memctx-real-s4-ctx（11-19 晚，第一次，作对照）；
  汇总表另读 results\memctx-real-s4（11-19 中午，拆 MEMORY）。
- rag-qa：results\rag-real-a（真实模型）、results\rag-a-probe-20261120（单题测量）、results\rag-a-e2e-20261120（假模型端到端）；
  results\rag-probe-20261119（11-19 的单题测量，作对照）；bench_data\longmemeval-s\derived\rag-a-first200.*（数据）。
吞吐变化用各格原始记录里的吞吐（不用 pairs.csv 里保留 4 位小数的比值，免得取整时差 1）。
正确率配对用 src\judge.py 的 diff_ci、sign_test；每轮的配对和各次运行 judge\accuracy.json 里的逐一核对，对不上就报错。
"""
import csv
import importlib.util
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.stdout.reconfigure(encoding="utf-8")
SEC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
R = SEC / "results"
_spec = importlib.util.spec_from_file_location("judge", SEC / "03_memctx_probe" / "src" / "judge.py")
judge = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(judge)  # 只用 diff_ci、sign_test

MEM, CTX, CTX2 = R / "memctx-real-s4", R / "memctx-real-s4-ctx", R / "memctx-real-s4-ctx2"
RUN = {MEM: "11-19 中午", CTX: "11-19 晚", CTX2: "11-20 凌晨"}
RAG, RAG_PROBE, RAG_E2E, RAG_PROBE_B = R / "rag-real-a", R / "rag-a-probe-20261120", R / "rag-a-e2e-20261120", R / "rag-probe-20261119"
DERIVED = SEC / "bench_data" / "longmemeval-s" / "derived"
LAYOUT = {"mono": "Monolithic", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT", "split-both": "两个都隔离（各一个进程）",
          "split-interaction": "隔离 INTERACTION + RETRIEVAL", "split-interaction-memory": "隔离 INTERACTION + RETRIEVAL，再隔离 MEMORY"}
PROC = {"orchestrator": "编排器", "memory": "MEMORY 进程", "context": "CONTEXT 进程", "interaction": "INTERACTION 进程（含 RETRIEVAL）"}
# 表注里反复用到的说明
WINDOW = "稳态窗口（第 20 题做完到最后一题派出之间）"
SHA_NOTE = ("逐字比请求体的 SHA-256。转发给 DeepSeek 时每格开头另加一行不同的编号，免得命中别的格子留下的缓存；比的是加这一行之前、Ditto 发出的请求本身。")
CI_NOTE = "区间是配对比例差的 95% 区间（Agresti–Min 方法）"
ROUND = lambda r: "第 " + r[-1] + " 轮"  # r1、c16-r1 → 第 1 轮

csv_rows = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig")))
f = lambda v, n=3: None if v in (None, "") else round(float(v), n)
pct = lambda v: None if v in (None, "") else round(float(v) * 100, 1)
sec = lambda v: None if v in (None, "") else round(float(v) / 1000, 1)
wb = Workbook()
wb.remove(wb.active)
HEAD = PatternFill("solid", fgColor="E3EDF6")


def sheet(name, note, header, rows):
    ws = wb.create_sheet(name)
    ws.append([note])
    ws["A1"].font = Font(italic=True, color="5A6577")
    ws.append(header)
    for c in ws[2]:
        c.font = Font(bold=True); c.fill = HEAD; c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 30
    for r in rows:
        ws.append(r)
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(48, max(9, width * 1.6))
    ws.freeze_panes = "A3"


def cells_of(d):
    """{(布局, 轮, 并发): {meta, tasks{序号: 题}, rows{(题, 步骤): 请求}, window, csv}}。
    请求只取这一格自己的（按 cell 排除上一格晚到的）；csv 是 export.py 写的 cells.csv 那一行（每题用时 p50、p95）。"""
    by_stem = {c["cell"]: c for c in csv_rows(d / "cells.csv")}
    out = {}
    for p in sorted((d / "raw").glob("*.json")):
        x = json.loads(p.read_text(encoding="utf-8"))
        m = x["meta"]
        key = (m["layout"], m["label"].partition("|")[2], m["concurrency"])
        if key in out:
            raise SystemExit(f"{d.name}：{p.stem} 和另一格的布局、轮、并发相同")
        out[key] = {"meta": m, "tasks": {t["i"]: t for t in x["tasks"]}, "window": x.get("window"), "csv": by_stem[p.stem],
                    "rows": {(r["task"], r["kind"]): r for r in x["fake"]["rows"] if r.get("cell", p.stem) == p.stem}}
    return out


def labels_of(d):
    """judged.csv → {(布局, 轮): {序号: 判对}}。没判成的不算；rag 里模型输出不合格的题 judge.py 已记成判错。"""
    lab = defaultdict(dict)
    for r in csv_rows(d / "judge" / "judged.csv"):
        if r["判对"] in ("True", "False"):
            lab[(r["布局"], r["轮"])][int(r["序号"])] = r["判对"] == "True"
    return lab


def paired(specs):
    """specs：[(A 的 {序号: 判对}, B 的 {序号: 判对}), ...]，逐题配对后加起来 → 共同题数、A、B 正确率、只有 A 对、只有 B 对、差、区间、p。"""
    n = ra = rb = b = c = 0
    for A, B in specs:
        for i, x in A.items():
            y = B.get(i)
            if y is None:
                continue
            n += 1; ra += x; rb += y; b += x and not y; c += y and not x
    d, lo, hi = judge.diff_ci(b, c, n)
    return [n, pct(ra / n), pct(rb / n), b, c, pct(d), pct(lo), pct(hi), f(judge.sign_test(b, c))]


def check_against_accuracy_json(d, lab):
    """每轮“拆开对 Monolithic”的配对，和这次运行 judge.py 写的 accuracy.json 逐一核对。"""
    for p in json.loads((d / "judge" / "accuracy.json").read_text(encoding="utf-8"))["pairs"]:
        if "合并" in p["比较"] or p["比较"].startswith("参照") or not p["共同题数"]:
            continue
        (la, ra), (lb, rb) = p["A"].split(" "), p["B"].split(" ")
        got = paired([(lab[(la, ra)], lab[(lb, rb)])])
        want = [p["共同题数"], pct(p["A正确率"]), pct(p["B正确率"]), p["只有A对"], p["只有B对"], pct(p["正确率差B减A"]),
                pct(p["差的95%区间下限"]), pct(p["差的95%区间上限"]), f(p["符号检验p"])]
        if got != want:
            raise SystemExit(f"{d.name} {p['A']} vs {p['B']}：重算 {got}，accuracy.json {want}")


def busy(cell):
    """一格里各进程每题主线程忙多久 → {进程: (毫秒, 忙的比例, 用掉的核)}。"""
    w = cell["window"]
    return {k: (v["main_busy_ms"] / w["completed"], v["main_busy_share"], v["container_cores_used"])
            for k, v in {"orchestrator": w["orchestrator"], **w["remote"]}.items()}


def busy_rows(cells, model=None):
    rows = []
    for (layout, rnd, conc), c in sorted(cells.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0] != "mono", kv[0][0])):
        for k, (ms, share, cores) in busy(c).items():
            rows.append(([model] if model else []) + [LAYOUT[layout], conc, ROUND(rnd), PROC[k], round(ms), round(share * 100), round(cores, 2)])
    return rows


def comparisons(cells):
    """每个隔离的格子和同一轮、同一并发的 Monolithic：[(并发, 轮, 布局, Monolithic 吞吐, 隔离后吞吐, 变化, Monolithic p95 毫秒, 隔离后 p95 毫秒)]。"""
    out = []
    for (layout, rnd, conc), c in cells.items():
        if layout == "mono":
            continue
        mono = cells[("mono", rnd, conc)]
        a, b = mono["window"]["throughput_per_s"], c["window"]["throughput_per_s"]
        out.append((conc, rnd, layout, a, b, b / a - 1, float(mono["csv"]["latency_p95_ms"]), float(c["csv"]["latency_p95_ms"])))
    return sorted(out, key=lambda x: (x[0], x[2] != "split-context", x[2], x[1]))


ctx, ctx2, mem, rag, e2e = cells_of(CTX), cells_of(CTX2), cells_of(MEM), cells_of(RAG), cells_of(RAG_E2E)
lab_ctx, lab_ctx2, lab_mem, lab_rag = labels_of(CTX), labels_of(CTX2), labels_of(MEM), labels_of(RAG)
for d, lab in ((CTX, lab_ctx), (CTX2, lab_ctx2), (MEM, lab_mem), (RAG, lab_rag)):
    check_against_accuracy_json(d, lab)
C, L = {CTX: ctx, CTX2: ctx2, MEM: mem}, {CTX: lab_ctx, CTX2: lab_ctx2, MEM: lab_mem}
S4C = 37  # S4 只跑并发 37

# ================================================================ 二、S4 隔离 CONTEXT 再跑一次（表 1–5）
rows = []
for d in (CTX2, CTX):
    for conc, rnd, layout, a, b, g, pa, pb in comparisons(C[d]):
        rows.append([RUN[d], LAYOUT[layout], ROUND(rnd), round(a, 3), round(b, 3), pct(g), sec(pa), sec(pb), pct(pb / pa - 1)])
sheet("表1 拆CONTEXT_吞吐与用时", "S4，DeepSeek deepseek-flash（关闭思考），2 核，并发 37，每格 200 题。每行是同一次运行的同一轮：百分比 = 隔离后 ÷ 同一轮 Monolithic − 1。"
      "11-19 晚那次是 11-19 汇报里的数，放在这里对照。",
      ["哪次运行", "隔离方式", "轮", "Monolithic 吞吐（题/秒）", "隔离后吞吐（题/秒）", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "p95 变化（%）"], rows)

sheet("表2 拆CONTEXT_主线程", f"11-20 凌晨那次，并发 37。每题主线程忙 = {WINDOW}里主线程忙的总时间 ÷ 完成题数；忙的比例 = 主线程忙着的时间占窗口的比例。"
      "吞吐约等于 1 ÷ 最忙那条主线程每题忙多久。",
      ["布局", "并发", "轮", "进程", "每题主线程忙（毫秒）", "主线程忙的比例（%）", "用掉的核"], busy_rows(ctx2))

rows = []
acc = {c["格子"]: c for c in json.loads((CTX2 / "judge" / "accuracy.json").read_text(encoding="utf-8"))["cells"]}
for (layout, rnd, conc), c in sorted(ctx2.items(), key=lambda kv: (kv[0][1], kv[0][0] != "mono", kv[0][0])):
    a, row = acc[c["csv"]["cell"]], c["csv"]
    rows.append([LAYOUT[layout], ROUND(rnd), int(row["tasks"]) - int(row["failed"]), int(row["failed"]), round(c["window"]["throughput_per_s"], 3),
                 sec(row["latency_p50_ms"]), sec(row["latency_p95_ms"]), a["判对"], pct(a["正确率"])])
sheet("表3 拆CONTEXT_每格", "11-20 凌晨那次，并发 37，每格 200 题，都跑完、都判过。正确率：GPT-4o（gpt-4o-2024-08-06）按 LongMemEval 官方口径判。",
      ["布局", "轮", "跑完", "失败", "吞吐（题/秒）", "每题用时 p50（秒）", "每题用时 p95（秒）", "判对题数", "正确率（%）"], rows)

rows = []
for d in (CTX2, CTX):
    for s in ("split-context", "split-both"):
        for r in ("r1", "r2"):
            rows.append(["每轮：隔离对 Monolithic", RUN[d], f"Monolithic {ROUND(r)}", f"{LAYOUT[s]} {ROUND(r)}", *paired([(L[d][("mono", r)], L[d][(s, r)])])])
for d in (CTX2, CTX):
    for s in ("split-context", "split-both"):
        rows.append(["一次运行两轮合并", RUN[d], "Monolithic 两轮", f"{LAYOUT[s]} 两轮", *paired([(L[d][("mono", r)], L[d][(s, r)]) for r in ("r1", "r2")])])
merged = {}
for s in ("split-context", "split-both"):
    merged[s] = paired([(L[d][("mono", r)], L[d][(s, r)]) for d in (CTX, CTX2) for r in ("r1", "r2")])
    rows.append(["两次运行四轮合并", "11-19 晚 + 11-20 凌晨", "Monolithic 四轮", f"{LAYOUT[s]} 四轮", *merged[s]])
monos = [(d, r) for d in (CTX, CTX2) for r in ("r1", "r2")]
for i in range(len(monos)):
    for j in range(i + 1, len(monos)):
        (da, ra), (db, rb) = monos[i], monos[j]
        rows.append(["参照：Monolithic 自己重复跑", "两次运行", f"{RUN[da]} {ROUND(ra)}", f"{RUN[db]} {ROUND(rb)}", *paired([(L[da][("mono", ra)], L[db][("mono", rb)])])])
for d in (CTX2, CTX):
    for s in ("split-context", "split-both"):
        rows.append(["参照：同一隔离布局跑两轮", RUN[d], f"{LAYOUT[s]} 第 1 轮", f"{LAYOUT[s]} 第 2 轮", *paired([(L[d][(s, "r1")], L[d][(s, "r2")])])])
sheet("表4 拆CONTEXT_正确率配对", f"逐题配对，只用两边都跑完、都判过的题。{CI_NOTE}。合并的几行同一题出现了 2 次或 4 次，区间偏窄，只作描述。",
      ["比较", "哪次运行", "A", "B", "共同题数", "A 正确率（%）", "B 正确率（%）", "只有 A 对", "只有 B 对", "差 B−A（百分点）", "95% 区间下限", "95% 区间上限", "符号检验 p"], rows)


def s4_inputs(A, B, la, lb):
    common = sorted(set(A["tasks"]) & set(B["tasks"]))
    key = lambda i: f"{i}:{A['tasks'][i]['id']}"
    req = lambda X, i: (X["rows"].get((key(i), "answer")) or {}).get("sha256")
    words = lambda X, i: [q.lower() for q in X["tasks"][i].get("queries") or []]
    text = lambda X, i: (X["tasks"][i].get("answer") or "").strip()
    same_q = [i for i in common if words(A, i) == words(B, i)]
    same = {i for i in common if req(A, i) and req(A, i) == req(B, i)}
    disc = [i for i in common if i in la and i in lb and la[i] != lb[i]]
    return [len(common), len(same_q), sum(1 for i in same_q if i in same), len(same), sum(1 for i in same if text(A, i) == text(B, i)),
            len(disc), sum(1 for i in disc if i in same)]


rows, s4_between, s4_within = [], [0] * 7, [0] * 7
for d in (CTX2, CTX):
    for s in ("split-context", "split-both"):
        for r in ("r1", "r2"):
            x = s4_inputs(C[d][("mono", r, S4C)], C[d][(s, r, S4C)], L[d][("mono", r)], L[d][(s, r)])
            s4_between = [t + v for t, v in zip(s4_between, x)]
            rows.append(["隔离对 Monolithic", RUN[d], f"Monolithic {ROUND(r)}", f"{LAYOUT[s]} {ROUND(r)}", *x])
rows.append(["隔离对 Monolithic：8 组合计", "两次运行", "", "", *s4_between])
for d in (CTX2, CTX):
    for l in ("mono", "split-context", "split-both"):
        x = s4_inputs(C[d][(l, "r1", S4C)], C[d][(l, "r2", S4C)], L[d][(l, "r1")], L[d][(l, "r2")])
        s4_within = [t + v for t, v in zip(s4_within, x)]
        rows.append(["参照：同一布局跑两轮", RUN[d], f"{LAYOUT[l]} 第 1 轮", f"{LAYOUT[l]} 第 2 轮", *x])
rows.append(["参照：6 组合计", "两次运行", "", "", *s4_within])
sheet("表5 拆CONTEXT_模型输入", f"S4 每题模型调用两步：先给检索词，再回答。回答请求 = 回答一步发给模型的请求，{SHA_NOTE}"
      "检索词相同时，检索到的记忆和整理出的上下文也相同，回答请求就逐字相同。",
      ["比较", "哪次运行", "A", "B", "共同题数", "检索词相同的题", "其中回答请求逐字相同", "回答请求逐字相同的题（合计）", "其中模型回答文字完全相同",
       "只有一方答对的题", "其中回答请求逐字相同"], rows)

# ================================================================ 三、rag-qa（表 6–14）
m = json.loads((DERIVED / "rag-a-first200.manifest.json").read_text(encoding="utf-8"))
m_b = json.loads((DERIVED / "rag-lme-first200.manifest.json").read_text(encoding="utf-8"))
ev = [t["evidence"] for c in rag.values() for t in c["tasks"].values() if t["ok"] and t.get("evidence") is not None]
sheet("表6 rag-qa_上限与数据", "一段对话一个来源，来源标题是这段对话的日期。官方示例（Ditto 上游 examples/patterns/rag-qa）的上限只放开前两项，其余照官方。数据是 LongMemEval_S 前 200 题。",
      ["项", "官方示例的上限", "这次", "前 200 题的实际情况"],
      [["一次请求的来源数", 20, 100, f"每题 {m['sources_per_task']['min']}–{m['sources_per_task']['max']} 个（中位 {m['sources_per_task']['median']}），共 {m['knowledge_records']:,} 个"],
       ["每个来源的块数", 200, 400, f"中位 {m['chunks_per_source']['median']}，最多 {m['chunks_per_source']['max']}；超过 200 块的 {m['sources_over_official_200_chunks']} 个"],
       ["每个来源的字数", "100,000", "不变", f"中位 {m['chars_per_source']['median']:,}，p95 {m['chars_per_source']['p95']:,}，最多 {m['chars_per_source']['max']:,}"],
       ["每行字数", "1,600", "不变", f"{m['wrapped_lines']} 行超过（{m['tasks_with_wrapped_lines']} 题），准备数据时折行，只插入换行、不改文字"],
       ["问题字数", "1,200", "不变", f"最多 {m['question_chars_max']}（含提问日期）"],
       ["检索词个数", "1–3", "不变", "—"],
       ["检索池长度（检索结果转成 JSON 后的字数，含元数据）", "45,000", "不变", "—"],
       ["挑证据最多留几块", 8, "不变", f"实际中位 {statistics.median(ev):g} 块"],
       ["证据字数", "10,000", "不变", "—"],
       ["每题的块数", "—", "—", f"中位 {m['chunks_per_task']['median']:,}"]])

rows = []
for label, p, man in (("11-20：一段对话一个来源", RAG_PROBE, m), ("11-19：相邻对话合并成不超过 20 个来源", RAG_PROBE_B, m_b)):
    d = csv_rows(p / "demands.csv")[0]
    t = float(d["task_busy_ms"])
    rows.append([label, round(t), round(float(d["INTERACTION_ms"])), round(float(d["RETRIEVAL_ms"])), round(float(d["MEMORY_ms"])),
                 round(float(d["orchestrator_ms"]) + float(d["other_workers_ms"])), round(float(d["INTERACTION_ms"]) / t * 100),
                 man["sources_per_task"]["median"], man["chars_per_source"]["median"]])
sheet("表7 rag-qa_活的分布", "单题测量：Monolithic，1 核，并发 1，模型延迟 0，前 50 题，3 个新容器各 1 遍；每题主线程忙多久（毫秒）。11-19 那一行是 11-19 汇报表 10 的数，放在这里对照。",
      ["来源怎么切", "每题合计（毫秒）", "INTERACTION", "RETRIEVAL", "MEMORY", "编排器和 CONTEXT、INFER", "INTERACTION 占比（%）", "每题来源数（中位）", "每个来源字数（中位）"], rows)

rows = [[conc, ROUND(rnd), round(a, 3), round(b, 3), pct(g), sec(pa), sec(pb), pct(pb / pa - 1)] for conc, rnd, layout, a, b, g, pa, pb in comparisons(rag)]
sheet("表8 rag-qa真实_吞吐与用时", "DeepSeek deepseek-flash（关闭思考），2 核，每格 200 题；每个并发按 Monolithic → 隔离 → 隔离 → Monolithic 跑两轮。"
      "每行是同一并发、同一轮：百分比 = 隔离 INTERACTION + RETRIEVAL 后 ÷ 同一轮 Monolithic − 1。",
      ["并发", "轮", "Monolithic 吞吐（题/秒）", "隔离后吞吐（题/秒）", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "p95 变化（%）"], rows)

sheet("表9 rag-qa_主线程", f"每题主线程忙 = {WINDOW}里主线程忙的总时间 ÷ 完成题数；忙的比例 = 主线程忙着的时间占窗口的比例。真实模型 8 格（表 8）和假模型 15 格（表 10）。",
      ["模型", "布局", "并发", "轮", "进程", "每题主线程忙（毫秒）", "主线程忙的比例（%）", "用掉的核"], busy_rows(rag, "真实模型") + busy_rows(e2e, "假模型"))

same_e2e = {(p["layout"], p["round"], int(p["concurrency"])): p["requests_same"] for p in csv_rows(RAG_E2E / "pairs.csv")}
rows = [[conc, ROUND(rnd), LAYOUT[layout], round(a, 3), round(b, 3), pct(g), sec(pa), sec(pb), same_e2e[(layout, rnd, conc)]]
        for conc, rnd, layout, a, b, g, pa, pb in comparisons(e2e)]
sheet("表10 rag-qa假模型_端到端", "假模型：理解问题、挑证据、核对一步中位 1 秒，回答一步中位 2 秒，带随机波动，各布局同一题同一步等一样久。2 核，每格 200 题；"
      "并发 8 按 Monolithic → 隔离 → 再隔离 MEMORY 跑一轮，并发 16、37 按 Monolithic → 隔离 → 再隔离 MEMORY → 再隔离 MEMORY → 隔离 → Monolithic 跑两轮。"
      "百分比 = 隔离后 ÷ 同一并发、同一轮的 Monolithic − 1。最后一列：假模型的回复固定，200 题 × 4 步的请求逐字比 SHA-256。",
      ["并发", "轮", "隔离方式", "Monolithic 吞吐（题/秒）", "隔离后吞吐（题/秒）", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "发给模型的请求和 Monolithic 相同"], rows)

rows = []
acc = {c["格子"]: c for c in json.loads((RAG / "judge" / "accuracy.json").read_text(encoding="utf-8"))["cells"]}
for (layout, rnd, conc), c in sorted(rag.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0] != "mono")):
    a, row, ts = acc[c["csv"]["cell"]], c["csv"], c["tasks"].values()
    status = lambda s: sum(1 for t in ts if t["ok"] and t.get("ragStatus") == s)
    model_fail = sum(1 for t in ts if not t["ok"] and t.get("failKind") == "model-output")
    rows.append([LAYOUT[layout], conc, ROUND(rnd), status("answered"), status("insufficient-evidence"), status("conflicting-evidence"),
                 status("needs-clarification"), model_fail, int(row["failed"]) - model_fail, round(c["window"]["throughput_per_s"], 3),
                 sec(row["latency_p50_ms"]), sec(row["latency_p95_ms"]), a["判对"], pct(a["正确率"]), pct(a["正确率_只算判过的"])])
sheet("表11 rag-qa真实_每格", "官方回答状态：answered 有结论；insufficient-evidence 证据不足；conflicting-evidence 证据冲突；needs-clarification 要澄清。"
      "模型输出过不了官方检查的题照官方算答不出，判分算错。正确率：GPT-4o 按 LongMemEval 官方口径。",
      ["布局", "并发", "轮", "answered", "insufficient-evidence", "conflicting-evidence", "needs-clarification", "模型输出不合格", "程序或网络出错",
       "吞吐（题/秒）", "每题用时 p50（秒）", "每题用时 p95（秒）", "判对题数", "正确率（全部 200 题，%）", "正确率（只算 GPT 判过的，%）"], rows)

RR = ("c16-r1", "c16-r2", "c37-r1", "c37-r2")
conc_name = lambda r: "并发 " + r.partition("-")[0][1:]
rows = [["每轮：隔离对 Monolithic", conc_name(r), f"Monolithic {ROUND(r)}", f"隔离 {ROUND(r)}", *paired([(lab_rag[("mono", r)], lab_rag[("split-interaction", r)])])]
        for r in RR]
for c in ("c16", "c37"):
    rows.append(["一个并发两轮合并", conc_name(c), "Monolithic 两轮", "隔离两轮", *paired([(lab_rag[("mono", r)], lab_rag[("split-interaction", r)]) for r in RR if r.startswith(c)])])
rag_merged = paired([(lab_rag[("mono", r)], lab_rag[("split-interaction", r)]) for r in RR])
rows.append(["四轮合并", "并发 16、37", "Monolithic 四轮", "隔离四轮", *rag_merged])
for l in ("mono", "split-interaction"):
    for c in ("c16", "c37"):
        rows.append(["参照：同一布局跑两轮", conc_name(c), f"{LAYOUT[l]} 第 1 轮", f"{LAYOUT[l]} 第 2 轮", *paired([(lab_rag[(l, f"{c}-r1")], lab_rag[(l, f"{c}-r2")])])])
sheet("表12 rag-qa真实_正确率配对", f"逐题配对，模型输出不合格的题按答错计（200 题都算）。{CI_NOTE}；合并的几行同一题出现了 2 次或 4 次，区间偏窄，只作描述。",
      ["比较", "并发", "A", "B", "共同题数", "A 正确率（%）", "B 正确率（%）", "只有 A 对", "只有 B 对", "差 B−A（百分点）", "95% 区间下限", "95% 区间上限", "符号检验 p"], rows)

STEPS = ("plan", "select", "answer", "verify")


def rag_inputs(A, B):
    """各步：两边都发过、请求逐字相同的 / 两边合起来发过的（同一题同一步的第二次记成 answer#2，按“题、步骤、第几次”比）。"""
    a = {k: v["sha256"] for k, v in A["rows"].items()}
    b = {k: v["sha256"] for k, v in B["rows"].items()}
    out = []
    for k in STEPS:
        keys = [x for x in set(a) | set(b) if x[1].partition("#")[0] == k]
        out.append(f"{sum(1 for x in keys if x in a and x in b and a[x] == b[x])}/{len(keys)}")
    return out


cell = lambda layout, r: rag[(layout, r, int(r[1:3]))]
rows = [["隔离对 Monolithic", conc_name(r), f"Monolithic {ROUND(r)}", f"隔离 {ROUND(r)}", *rag_inputs(cell("mono", r), cell("split-interaction", r))] for r in RR]
rows += [["参照：同一布局跑两轮", conc_name(c), f"{LAYOUT[l]} 第 1 轮", f"{LAYOUT[l]} 第 2 轮", *rag_inputs(cell(l, f"{c}-r1"), cell(l, f"{c}-r2"))]
         for l in ("mono", "split-interaction") for c in ("c16", "c37")]
sheet("表13 rag-qa真实_模型输入", f"每一步发给模型的请求：相同的个数 / 两边合起来的个数（官方回答、核对不合格时会重来一次，重来的那次单独算）。{SHA_NOTE}"
      "理解问题一步的请求只取决于题目；之后几步的请求里带着模型前面几步的输出，模型的输出不同，请求就不同。",
      ["比较", "并发", "A", "B", "理解问题一步", "挑证据一步", "回答一步", "核对一步"], rows)

# 有结论（answered）的题：两种布局之间、同一布局两轮之间，只在一边是“有结论”的题数
answered = lambda t: t["ok"] and t.get("ragStatus") == "answered"
ans_between = [0, 0]
for r in RR:
    A, B = cell("mono", r)["tasks"], cell("split-interaction", r)["tasks"]
    ans_between[0] += sum(1 for i in A if answered(A[i]) and not answered(B[i])); ans_between[1] += sum(1 for i in A if answered(B[i]) and not answered(A[i]))
ans_within = [0, 0]
for l in ("mono", "split-interaction"):
    for c in ("c16", "c37"):
        A, B = cell(l, f"{c}-r1")["tasks"], cell(l, f"{c}-r2")["tasks"]
        ans_within[0] += sum(1 for i in A if answered(A[i]) and not answered(B[i])); ans_within[1] += sum(1 for i in A if answered(B[i]) and not answered(A[i]))

refs = {e["question_id"]: e["question_type"] for e in json.load(open(SEC / "bench_data" / "longmemeval-s" / "longmemeval_s_cleaned.json", encoding="utf-8"))}
qtype = lambda qid: refs[qid] + ("（无法回答）" if "_abs" in qid else "")
by_status, by_type, s4_type = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
STATUS = {"answered": "answered（有结论）", "insufficient-evidence": "insufficient-evidence（证据不足）", "conflicting-evidence": "conflicting-evidence（证据冲突）",
          "needs-clarification": "needs-clarification（要澄清）"}
for (layout, rnd, conc), c in rag.items():
    for i, t in c["tasks"].items():
        ok = lab_rag[(layout, rnd)].get(i)
        s = STATUS[t["ragStatus"]] if t["ok"] else ("模型输出不合格" if t.get("failKind") == "model-output" else "程序或网络出错")
        by_status[s][0] += bool(ok); by_status[s][1] += 1
        by_type[qtype(t["id"])][0] += bool(ok); by_type[qtype(t["id"])][1] += 1
for d in (MEM, CTX, CTX2):
    for r in csv_rows(d / "judge" / "judged.csv"):
        if r["布局"] == "mono" and r["判对"] in ("True", "False"):  # 只算判过的（和表 4、表 15 的配对一样）
            s4_type[qtype(r["题号"])][0] += r["判对"] == "True"; s4_type[qtype(r["题号"])][1] += 1
rows = [["官方回答状态", s, v[1], v[0], pct(v[0] / v[1]), None] for s, v in sorted(by_status.items(), key=lambda kv: -kv[1][1])]
for k in sorted(by_type):
    v, s = by_type[k], s4_type[k]
    rows.append(["题型", k, v[1], v[0], pct(v[0] / v[1]), pct(s[0] / s[1]) if s[1] else None])
tot_rag = [sum(v[0] for v in by_type.values()), sum(v[1] for v in by_type.values())]
tot_s4 = [sum(v[0] for v in s4_type.values()), sum(v[1] for v in s4_type.values())]
rows.append(["全部", "", tot_rag[1], tot_rag[0], pct(tot_rag[0] / tot_rag[1]), pct(tot_s4[0] / tot_s4[1])])
sheet("表14 rag-qa真实_分状态分题型", "rag-qa 真实模型 8 格合起来（两种布局、两个并发、各两轮，每格 200 题；题次 = 题数 × 格数）。"
      "S4 一列是 S4 三次真实模型运行里 Monolithic 的 6 轮合起来，只算判过的题（11-19 中午第 1 轮有 1 题程序出错、没有判），作对照。",
      ["分组", "组", "题次", "判对", "正确率（%）", "S4 Monolithic 正确率（%）"], rows)

# ================================================================ 四、到目前为止的真实模型结果（表 15）


def span(xs):
    lo, hi = round(min(xs)), round(max(xs))
    return f"{lo}" if lo == hi else f"{lo}–{hi}"


def busiest(cells, layout):
    """隔离后最忙的主线程：是哪个进程、每题忙多久（各轮）。"""
    procs, ms = set(), []
    for (l, _, _), c in cells.items():
        if l == layout:
            k, (v, _, _) = max(busy(c).items(), key=lambda kv: kv[1][0])
            procs.add(PROC[k]); ms.append(v)
    return "、".join(sorted(procs)), ms


mono_ms = lambda cells: [busy(c)["orchestrator"][0] for (l, _, _), c in cells.items() if l == "mono"]
gains = lambda cells, s: [g * 100 for conc, rnd, layout, a, b, g, pa, pb in sorted(comparisons(cells), key=lambda x: (x[0], x[1])) if layout == s]


def lighter(cells, s):
    """每轮：隔离后最忙的主线程比同一轮 Monolithic 的编排器轻了百分之几。"""
    out = []
    for (l, rnd, conc), c in cells.items():
        if l == s:
            out.append((1 - max(v for v, _, _ in busy(c).values()) / busy(cells[("mono", rnd, conc)])["orchestrator"][0]) * 100)
    return out


pct_span = lambda xs: f"{round(min(xs))}" if round(min(xs)) == round(max(xs)) else f"{round(min(xs))}–{round(max(xs))}"
rows = []
proc, ms = busiest(mem, "split-memory")
mm = paired([(lab_mem[("mono", r)], lab_mem[("split-memory", r)]) for r in ("r1", "r2")])
rows.append(["隔离 MEMORY", "S4", "11-19 中午，2 轮", proc, span(ms), span(mono_ms(mem)), pct_span(lighter(mem, "split-memory")),
             "、".join(f"{g:+.0f}" for g in gains(mem, "split-memory")), mm[1], mm[2], *mm[5:]])
for s in ("split-both", "split-context"):
    p1, m1 = busiest(ctx, s); p2, m2 = busiest(ctx2, s)
    rows.append([LAYOUT[s], "S4", "11-19 晚、11-20 凌晨，共 4 轮", "、".join(sorted({*p1.split("、"), *p2.split("、")})), span(m1 + m2), span(mono_ms(ctx) + mono_ms(ctx2)),
                 pct_span(lighter(ctx, s) + lighter(ctx2, s)), "、".join(f"{g:+.0f}" for g in gains(ctx, s) + gains(ctx2, s)), merged[s][1], merged[s][2], *merged[s][5:]])
proc, ms = busiest(rag, "split-interaction")
rows.append(["隔离 INTERACTION + RETRIEVAL", "rag-qa", "11-20 凌晨，并发 16、37 各 2 轮", proc, span(ms), span(mono_ms(rag)), pct_span(lighter(rag, "split-interaction")),
             "、".join(f"{g:+.0f}" for g in gains(rag, "split-interaction")), rag_merged[1], rag_merged[2], *rag_merged[5:]])
sheet("表15 真实模型汇总", "到目前为止的真实模型结果（DeepSeek，2 核；S4 并发 37，rag-qa 并发 16、37）。吞吐变化：每轮和同一轮的 Monolithic 比。"
      f"正确率：各轮逐题配对后合起来（同一题出现多次，只作描述；{CI_NOTE}）。主线程：{WINDOW}里每题忙多久，各轮的范围；“轻了”是和同一轮 Monolithic 的编排器比。",
      ["隔离方式", "场景", "运行", "隔离后最忙的主线程", "它每题忙（毫秒）", "Monolithic 编排器每题忙（毫秒）", "比 Monolithic 编排器轻了（%）", "吞吐变化（%，每轮）",
       "正确率：Monolithic（%）", "正确率：隔离后（%）", "差（百分点）", "95% 区间下限", "95% 区间上限", "符号检验 p"], rows)

wb.save(OUT)
print(json.dumps({n: wb[n].max_row - 2 for n in wb.sheetnames}, ensure_ascii=False))
print("S4 模型输入（隔离对 Monolithic 8 组 / 同一布局 6 组）：", s4_between, s4_within)
print("rag 只在一边是“有结论”的题：两种布局之间（只有 Monolithic、只有隔离）", ans_between, "p =", f(judge.sign_test(*ans_between)),
      "；同一布局两轮之间（只有第 1 轮、只有第 2 轮）", ans_within)
for r in rows:
    print(r)
