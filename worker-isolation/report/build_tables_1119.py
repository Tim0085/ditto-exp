"""汇报用的结果表格：从各次实验的结果文件读数，生成一个 Excel，每张表对应汇报文档里的一节。只读结果，不改。

    python -I build_tables_1119.py <sec1-2.0 目录> <输出 xlsx>

11-19 的汇报（汇报_20261119\结果表格.xlsx）就是这个程序 11-19 晚的输出，之后表 1 的说明行另外手改过（删掉了开头的“汇报第三节。”）。
原来放在 _tmp\scratch\report\build_tables.py，2026-11-20 复制到这里留底，内容没改。11-20 的表格见 build_tables_1120.py。
"""
import csv
import json
import sys
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.stdout.reconfigure(encoding="utf-8")
SEC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
R = SEC / "results"
LAYOUT = {"mono": "Monolithic", "split-memory": "隔离 MEMORY", "split-context": "隔离 CONTEXT", "split-both": "两个都隔离（各一个进程）",
          "split-memctx": "MEMORY、CONTEXT 合进同一进程", "split-interaction": "隔离 INTERACTION + RETRIEVAL",
          "split-interaction-memory": "隔离 INTERACTION + RETRIEVAL，再隔离 MEMORY"}
PROC = {"orchestrator": "编排器", "memory": "MEMORY 进程", "context": "CONTEXT 进程", "memctx": "MEMORY+CONTEXT 进程", "interaction": "INTERACTION+RETRIEVAL 进程"}
REAL = [("中午（隔离 MEMORY）", R / "memctx-real-s4"), ("晚上（隔离 CONTEXT、两个都隔离）", R / "memctx-real-s4-ctx")]

csv_rows = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig")))
f = lambda v, n=3: None if v in (None, "") else round(float(v), n)
pct = lambda v: None if v in (None, "") else round(float(v) * 100, 1)
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
    for r in rows:
        ws.append(r)
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(48, max(9, width * 1.6))
    ws.freeze_panes = "A3"


def raw_cells(d):
    out = []
    for p in sorted((d / "raw").glob("*.json")):
        x = json.loads(p.read_text(encoding="utf-8"))
        out.append((p.stem, x))
    return out


def busy_rows(run, d):
    rows = []
    for stem, x in raw_cells(d):
        w, m = x.get("window"), x["meta"]
        if not w:
            continue
        procs = {"orchestrator": w["orchestrator"], **w["remote"]}
        for k, v in procs.items():
            rows.append([run, LAYOUT.get(m["layout"], m["layout"]), m["concurrency"], m["label"].partition("|")[2], PROC.get(k, k),
                         round(v["main_busy_ms"] / w["completed"]), round(v["main_busy_share"] * 100), round(v["container_cores_used"], 2)])
    return rows


# ---------------------------------------------------------------- 0 / 4.2 S4 真实模型
rows = []
for run, d in REAL:
    acc = {c["格子"]: c for c in json.loads((d / "judge" / "accuracy.json").read_text(encoding="utf-8"))["cells"]}
    for c in csv_rows(d / "cells.csv"):
        a = acc.get(c["cell"], {})
        rows.append([run, LAYOUT.get(c["layout"], c["layout"]), c["round"], int(c["tasks"]) - int(c["failed"]), int(c["failed"]),
                     f(c["throughput_per_s"]), f(float(c["latency_p50_ms"]) / 1000, 1), f(float(c["latency_p95_ms"]) / 1000, 1),
                     a.get("判对"), pct(a.get("正确率")), pct(a.get("正确率_只算判过的"))])
sheet("4.2 S4真实模型_每格", "S4 真实模型（DeepSeek deepseek-flash，关闭思考），2 核，并发 37，每格 200 题。正确率：GPT-4o 按 LongMemEval 官方口径判；“全部”把没跑完的算错。",
      ["哪次运行", "布局", "轮", "跑完", "失败", "吞吐（题/秒）", "每题用时 p50（秒）", "每题用时 p95（秒）", "判对题数", "正确率（全部 200 题，%）", "正确率（只算判过的，%）"], rows)

rows = []
for run, d in REAL:
    for p in sorted(csv_rows(d / "pairs.csv"), key=lambda p: (p["layout"], p["round"])):
        rows.append([run, LAYOUT.get(p["layout"], p["layout"]), p["round"], f(p["mono_throughput"]), f(p["split_throughput"]), pct(p["throughput_gain"]),
                     f(float(p["mono_p95_ms"]) / 1000, 1), f(float(p["split_p95_ms"]) / 1000, 1), pct(-float(p["p95_cut"]))])
sheet("0 S4真实模型_拆开对比", "每行是同一次运行的同一轮：Monolithic 和隔离后的布局比。百分比 = 隔离后 ÷ 同一轮 Monolithic − 1。",
      ["哪次运行", "隔离方式", "轮", "Monolithic 吞吐", "隔离后吞吐", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "p95 变化（%）"], rows)

rows = []
for run, d in REAL:
    for p in json.loads((d / "judge" / "accuracy.json").read_text(encoding="utf-8"))["pairs"]:
        if not p.get("共同题数"):
            continue
        rows.append([run, p["比较"], p["A"], p["B"], p["共同题数"], pct(p["A正确率"]), pct(p["B正确率"]), p["只有A对"], p["只有B对"],
                     pct(p["正确率差B减A"]), pct(p["差的95%区间下限"]), pct(p["差的95%区间上限"]), f(p["符号检验p"])])
sheet("4.5 S4真实模型_正确率配对", "只用两边都跑完、都判过的题。区间是 Agresti–Min 配对区间；“两轮合并”同一题算了两次，只作描述。",
      ["哪次运行", "比较", "A", "B", "共同题数", "A 正确率（%）", "B 正确率（%）", "只有 A 对", "只有 B 对", "差 B−A（百分点）", "95% 区间下限", "95% 区间上限", "符号检验 p"], rows)

rows = []
for run, d in REAL:
    rows += busy_rows(run, d)
sheet("4.3 S4真实模型_主线程忙", "稳态窗口里主线程忙的总时间 ÷ 完成题数。吞吐约等于 1 ÷ 最忙那条主线程每题忙多久。",
      ["哪次运行", "布局", "并发", "轮", "进程", "每题主线程忙（毫秒）", "主线程忙的比例（%）", "用掉的核"], rows)

# ---------------------------------------------------------------- 4.1 假模型端到端（S1–S4）
rows = []
for p in csv_rows(R / "memctx-e2e-20261119" / "pairs.csv"):
    rows.append([p["group"], p["cores"], int(p["concurrency"]), p["round"], LAYOUT.get(p["layout"], p["layout"]), f(p["mono_throughput"]), f(p["split_throughput"]),
                 pct(p["throughput_gain"]), f(float(p["mono_p95_ms"]) / 1000, 1), f(float(p["split_p95_ms"]) / 1000, 1), pct(-float(p["p95_cut"]))])
rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4]))
sheet("2+4.1 S1-S4假模型端到端", "11-19 凌晨，假模型（理解问题中位 1 秒、回答 2 秒），每格 200 题。组名：s1-qa=S1 只用记忆，s2=S2 长文档，s3=S3 整段历史交给 CONTEXT，s4=S4 协作；u100/u500=记忆库用户数；k=核数。",
      ["组", "核数", "并发", "轮", "隔离方式", "Monolithic 吞吐", "隔离后吞吐", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "p95 变化（%）"], rows)

rows = []
for d in csv_rows(R / "memctx-probe-20261119" / "demands.csv"):
    rows.append([d["config"], d["scenario"], f(d["task_busy_ms"], 0), f(d["orchestrator_ms"], 0), f(d["other_workers_ms"], 0),
                 f(d.get("MEMORY_ms"), 0), f(d.get("CONTEXT_ms"), 0), pct(d["target_share"])])
sheet("2 S1-S4单题测量", "单题测量：Monolithic，1 核，并发 1，模型延迟 0，每题主线程忙多久（毫秒）。“被突出的 Worker 占比”太高（一家独大）或总量太小，隔离都没有收益。",
      ["配置", "场景", "每题合计", "编排器本身", "其他 Worker", "MEMORY", "CONTEXT", "被突出的 Worker 占比（%）"], rows)

# ---------------------------------------------------------------- 4.4 合进同一进程
rows = []
for p in sorted(csv_rows(R / "memctx-memctx-20261119" / "pairs.csv"), key=lambda p: (p["layout"], p["round"])):
    rows.append([LAYOUT.get(p["layout"], p["layout"]), p["round"], f(p["mono_throughput"]), f(p["split_throughput"]), pct(p["throughput_gain"]),
                 f(float(p["mono_p95_ms"]) / 1000, 1), f(float(p["split_p95_ms"]) / 1000, 1), p["requests_same"]])
sheet("4.4 合进同一进程_离线", "S4，假模型，2 核，并发 37，顺序 Monolithic → 合进同一进程 → 两个都隔离 → 两个都隔离 → 合进同一进程 → Monolithic。",
      ["布局", "轮", "Monolithic 吞吐", "隔离后吞吐", "吞吐变化（%）", "Monolithic p95（秒）", "隔离后 p95（秒）", "发给模型的请求和 Monolithic 相同"], rows)
sheet("4.4 合进同一进程_主线程忙", "稳态窗口里每题主线程忙多久。", ["运行", "布局", "并发", "轮", "进程", "每题主线程忙（毫秒）", "主线程忙的比例（%）", "用掉的核"],
      busy_rows("假模型 11-19 20:36", R / "memctx-memctx-20261119"))

# ---------------------------------------------------------------- 5 rag-qa
d = csv_rows(R / "rag-probe-20261119" / "demands.csv")[0]
sheet("5 rag-qa_单题测量", "单题测量：Monolithic，1 核，并发 1，模型延迟 0，前 50 题，3 个新容器各 1 遍。",
      ["每题合计（毫秒）", "INTERACTION", "RETRIEVAL", "MEMORY", "编排器和 CONTEXT、INFER", "INTERACTION 占比（%）", "INTERACTION 进出数据（KB）", "MEMORY 进出数据（KB）"],
      [[f(d["task_busy_ms"], 0), f(d["INTERACTION_ms"], 0), f(d["RETRIEVAL_ms"], 0), f(d["MEMORY_ms"], 0),
        f(float(d["orchestrator_ms"]) + float(d["other_workers_ms"]), 0), round(float(d["INTERACTION_ms"]) / float(d["task_busy_ms"]) * 100),
        f(d["INTERACTION_transfer_kb"], 0), f(d["MEMORY_transfer_kb"], 0)]])
rows = [[LAYOUT.get(e["layout"], e["layout"]), e["cores"], int(e["concurrency"]), pct(e["throughput_gain"]), e["split_demands_ms"], f(e["mono_demand_ms"], 0)]
        for e in csv_rows(R / "rag-probe-20261119" / "estimate.csv") if e["http_ms"] == "0.3"]
sheet("5 rag-qa_推算", "阶段二：均值分析推算（每次跨进程调用另加 0.3 毫秒）。", ["隔离方式", "核数", "并发", "推算吞吐变化（%）", "隔离后各进程每题需求（毫秒）", "Monolithic 每题需求（毫秒）"], rows)
rows = []
for p in sorted(csv_rows(R / "rag-e2e-20261119" / "pairs.csv"), key=lambda p: (int(p["concurrency"]), p["layout"], p["round"])):
    rows.append([int(p["concurrency"]), p["round"], LAYOUT.get(p["layout"], p["layout"]), f(p["mono_throughput"]), f(p["split_throughput"]), pct(p["throughput_gain"]),
                 f(float(p["mono_p95_ms"]) / 1000, 1), f(float(p["split_p95_ms"]) / 1000, 1), p["requests_same"]])
sheet("5 rag-qa_端到端", "端到端实测：假模型，2 核，每格 200 题，并发 16、37 做 A B B A。", ["并发", "轮", "隔离方式", "Monolithic 吞吐", "隔离后吞吐", "吞吐变化（%）",
      "Monolithic p95（秒）", "隔离后 p95（秒）", "发给模型的请求和 Monolithic 相同"], rows)
sheet("5 rag-qa_主线程忙", "稳态窗口里每题主线程忙多久。", ["运行", "布局", "并发", "轮", "进程", "每题主线程忙（毫秒）", "主线程忙的比例（%）", "用掉的核"],
      busy_rows("假模型 11-19 21:01", R / "rag-e2e-20261119"))

# ---------------------------------------------------------------- 1 第 8 版、2 bench 筛选、6 费用（来自已有文档的结论表）
sheet("1 第8版BFCL", "docs\\08 第 7 节：BFCL multi-turn base，并发 37，带 CoT 的三组吞吐（题/秒）。原始数据在 原始结果\\第8版BFCL_11-16\\。",
      ["CPU 设置", "Monolithic", "等配", "Targeted", "等配比 Monolithic", "Targeted 比等配"],
      [["2 核绑核（第一次）", 1.18, 1.08, 1.10, "−8.5%", "+2.5%"], ["3 核绑核", 1.21, 1.14, 1.11, "−6.0%", "−2.1%"], ["3.25 核配额", 1.16, 0.60, 1.06, "−48.8%", "+77.5%"]])
sheet("2 bench筛选", "汇报第三节。隔离的效果 = 隔离后的吞吐相对同一轮 Monolithic 的变化。S1–S4 是假模型实测（表 3），原始数据见 原始结果\\ 下对应的文件夹。",
      ["bench（用法）", "被突出的 Worker", "活的分布", "隔离的效果", "原始数据"],
      [["BFCL multi-turn base", "各 Worker", "工具在 Python 子进程里执行，Worker 几乎没活", "实测各种分法 −6% 到 −49%", "第8版BFCL_11-16"],
       ["BFCL multi_turn_long_context", "CONTEXT", "工具返回每题中位只有 7 KB，CONTEXT 几乎没活", "没有负载，未测", "—"],
       ["BIRD（官方写法，SQL 在子进程）", "INTERACTION", "每条查询主线程只忙约 5 毫秒", "推算 0% 到 +4%", "bench重活测量_11-18"],
       ["S2：LongBench v2，长文档进上下文后挑选", "CONTEXT", "太轻，每题只忙 8–30 毫秒", "实测 −1% 到 0%", "S1-S4单题测量与推算、S1-S4假模型端到端"],
       ["S1：LongMemEval，只用长期记忆", "MEMORY", "MEMORY 占 97%", "实测 −2% 到 0%", "同上"],
       ["S3：LongMemEval，整段历史交给 CONTEXT", "CONTEXT", "CONTEXT 占 96%，每题还要传回约 13 MB", "实测 −7% 到 −36%", "同上"],
       ["S4：LongMemEval，CONTEXT 与 MEMORY 协作", "CONTEXT + MEMORY", "两个都有活：每题 MEMORY 约 358 毫秒、CONTEXT 约 253 毫秒", "隔离 MEMORY +47% 到 +60%", "同上"],
       ["LongMemEval，官方 rag-qa", "INTERACTION（+ RETRIEVAL）", "INTERACTION 占 89%", "+16% 到 +27%", "rag-qa_离线"]])
sheet("6 费用", "程序按用量估算，没有和账单核对。", ["项", "DeepSeek（元）", "GPT-4o（美元）", "说明"],
      [["11-16 第 8 版两次真实实测", 37.6, None, ""], ["11-19 S4 拆 MEMORY（4 格）", 21.9, 0.42, "按实际时段折算"],
       ["11-19 S4 拆 CONTEXT、两个都隔离（6 格）", 32.8, 0.61, "按实际时段折算"], ["11-19 作废重跑的那一次", 27.4, None, "运行中改了程序，整轮作废"],
       ["假模型的全部离线测量", 0, 0, ""]])

RENAME = {"2 bench筛选": "表1 bench筛选", "2 S1-S4单题测量": "表2 S1-S4活的分布", "2+4.1 S1-S4假模型端到端": "表3 S1-S4假模型实测",
          "0 S4真实模型_拆开对比": "表4 S4真实_吞吐与用时", "4.3 S4真实模型_主线程忙": "表5 S4真实_主线程", "4.2 S4真实模型_每格": "表6 S4真实_每格",
          "4.5 S4真实模型_正确率配对": "表7 S4真实_正确率配对", "4.4 合进同一进程_离线": "表8 合进同一进程", "4.4 合进同一进程_主线程忙": "表9 合进同一进程_主线程",
          "5 rag-qa_单题测量": "表10 rag-qa活的分布", "5 rag-qa_端到端": "表11 rag-qa实测", "5 rag-qa_主线程忙": "表12 rag-qa主线程"}
for name in [n for n in wb.sheetnames if n not in RENAME]:
    del wb[name]
for old, new in RENAME.items():
    wb[old].title = new
order = list(RENAME.values())
wb._sheets = [wb[n] for n in order]
wb.save(OUT)
print(json.dumps({n: wb[n].max_row - 2 for n in order}, ensure_ascii=False))
