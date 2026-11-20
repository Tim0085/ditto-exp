"""S4 真实模型验证的汇总（docs\\20 用的表）。只排表，不改数据。

    python -I src/real_report.py <结果目录>     打印 Markdown；另写 <结果目录>/model.csv（每格的模型用时、token、费用）

用到：export.py 写的 cells.csv、pairs.csv；raw\\*.json（每格原始记录，含出口记下的每个模型请求）；
judge\\accuracy.json（GPT-4o 判分，没判就不出正确率）；egress\\ledger.jsonl、requests.jsonl（出口的账本，含冒烟）。
费用：账本按 DeepSeek 高峰价记（上限）；另按每个请求发出的时刻折算（官网：北京时间周一到周五 9–12、14–18 点高峰，
其余时间半价；法定节假日没有区分，那几天会多算）。
"""
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(sys.argv[1])
NAME = {"mono": "Monolithic", "direct": "Direct（不用 Ditto 运行时）", "split-memory": "拆 MEMORY", "split-context": "拆 CONTEXT",
        "split-both": "两个都拆", "split-memctx": "MEMORY 和 CONTEXT 合进一个进程",
        "split-interaction": "拆 INTERACTION+RETRIEVAL", "split-interaction-memory": "拆 INTERACTION+RETRIEVAL 和 MEMORY"}
# 模型的各步：S4 是检索词、回答两步；rag（官方 rag-qa）是理解问题、挑证据、回答、核对四步。出口把同一题同一步的第二次记成 answer#2。
STEPS = ("keyword", "plan", "select", "answer", "verify")
STEP_NAME = {"keyword": "检索词", "plan": "理解问题", "select": "挑证据", "answer": "回答", "verify": "核对"}
BJ = timezone(timedelta(hours=8))


def is_peak(at):
    t = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(BJ)
    return t.weekday() < 5 and (9 <= t.hour < 12 or 14 <= t.hour < 18)


def by_time_cny(rows):
    """按发出时刻折算（高峰全价、其余半价）。按每次尝试算：重试可能跨过 12:00、14:00、18:00 这些时刻。"""
    total = 0
    for r in rows:
        tries = r.get("tries") or []
        if tries:
            total += sum((t.get("micro") or 0) * (1 if is_peak(t["at"]) else 0.5) for t in tries if t.get("outcome") == "settled")
        else:
            total += (r.get("micro_cny") or 0) * (1 if is_peak(r["at"]) else 0.5)
    return total / 1e6


def read_csv(path):
    return list(csv.DictReader(open(path, encoding="utf-8-sig"))) if path.exists() else []


def num(v, fmt="{:.2f}"):
    return "—" if v in (None, "") else fmt.format(float(v))


def pct(v):
    return "—" if v in (None, "") else f"{float(v) * 100:+.0f}%"


cells = read_csv(OUT / "cells.csv")
pairs = read_csv(OUT / "pairs.csv")
acc = json.loads((OUT / "judge" / "accuracy.json").read_text(encoding="utf-8")) if (OUT / "judge" / "accuracy.json").exists() else None
acc_by_cell = {c["格子"]: c for c in acc["cells"]} if acc else {}
raw = {c["cell"]: json.loads((OUT / "raw" / f"{c['cell']}.json").read_text(encoding="utf-8")) for c in cells}

# ---------------------------------------------------------------- 每格的模型用时、token、费用

def own_rows(cell):
    """这一格自己的模型请求。出口按复位以后收到的记，上一格晚到的请求会混进来（例如 DeepSeek 排队 10 分钟的那种），按 cell 排除。"""
    return [r for r in raw[cell]["fake"]["rows"] if r.get("cell", cell) == cell]


def base(kind):
    """出口把同一题同一步的第二次记成 answer#2：用时、token 按基础步骤合起来算。"""
    return kind.partition("#")[0]


RAG = any(raw[c["cell"]]["meta"]["scenario"] == "rag" for c in cells)
FIRST = "plan" if RAG else "keyword"  # 第一步：rag 是理解问题，S4 是检索词
steps = [k for k in STEPS if any(base(r["kind"]) == k for c in cells for r in own_rows(c["cell"]))]
model = []
for c in cells:
    rows = own_rows(c["cell"])
    def med(kind):
        xs = [r["latency_ms"] for r in rows if r["ok"] and base(r["kind"]) == kind]
        return statistics.median(xs) / 1000 if xs else None
    answers = [r for r in rows if r["ok"] and base(r["kind"]) == "answer" and r.get("usage")]
    # usage 里的数可能缺或不是数（费用不明的请求）：只取是数的，汇总不能在付费以后才崩。
    count = lambda r, k: r["usage"].get(k) if isinstance(r.get("usage"), dict) and isinstance(r["usage"].get(k), int) else None
    prompt = sum(count(r, "prompt_tokens") or 0 for r in rows)
    hit = sum(count(r, "prompt_cache_hit_tokens") or 0 for r in rows)
    answers = [r for r in answers if count(r, "prompt_tokens") is not None and count(r, "completion_tokens") is not None]
    model.append({
        "格子": c["cell"], "布局": NAME.get(c["layout"], c["layout"]), "轮": c["round"], "模型请求": len(rows), "成功": sum(1 for r in rows if r["ok"]),
        # 发出去的次数减去发出去过的请求数（被预算或停机拒绝、一次都没发的不算）。
        "重试多发的请求": sum(r["upstream_calls"] for r in rows) - sum(1 for r in rows if r["upstream_calls"]),
        **{f"{STEP_NAME[k]}一步用时中位(秒)": med(k) for k in steps},
        "回答一步平均输入token": statistics.mean(r["usage"]["prompt_tokens"] for r in answers) if answers else None,
        "回答一步平均输出token": statistics.mean(r["usage"]["completion_tokens"] for r in answers) if answers else None,
        "缓存命中占输入": hit / prompt if prompt else None,
        "费用_高峰价(元)": sum(r["micro_cny"] or 0 for r in rows) / 1e6,
        "费用不明而保留(元)": sum(r.get("held_micro") or 0 for r in rows) / 1e6,
        "费用_按时段(元)": by_time_cny(rows),
    })
with open(OUT / "model.csv", "w", newline="", encoding="utf-8-sig") as f:
    if model:
        w = csv.DictWriter(f, list(model[0]))
        w.writeheader()
        for r in model:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
model_by_cell = {m["格子"]: m for m in model}

# ---------------------------------------------------------------- 表

print("## 每格\n")
print(f"| 格子 | 布局 | 并发 | 跑完 / 失败{'（其中模型输出不合格）' if RAG else ''} | 吞吐（题/秒） | 每题用时 p50 / p95（秒） | "
      f"模型用时中位：{STEP_NAME[FIRST]} / 回答（秒） | 回答一步平均输入 token | 正确率 |")
print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
for c in cells:
    m, a = model_by_cell[c["cell"]], acc_by_cell.get(c["cell"])
    done = int(c["tasks"]) - int(c["failed"])
    model_fail = sum(1 for t in raw[c["cell"]]["tasks"] if not t["ok"] and t.get("failKind") == "model-output")
    # 两种分母并列：全部题（没跑完、没判成的算错）和只算判过的（官方脚本按提交的预测计）。
    accuracy = "—" if not a else "{:.1f}%（{}/{}；只算判过的 {:.1f}%）".format(a["正确率"] * 100, a["判对"], a["题数"], a["正确率_只算判过的"] * 100)
    p50 = float(c["latency_p50_ms"]) / 1000 if c["latency_p50_ms"] else None
    p95 = float(c["latency_p95_ms"]) / 1000 if c["latency_p95_ms"] else None
    print(f"| {c['cell']} | {NAME.get(c['layout'], c['layout'])} | {c['concurrency']} | {done} / {c['failed']}{f'（{model_fail}）' if RAG else ''} | "
          f"{num(c['throughput_per_s'], '{:.3f}')} | {num(p50, '{:.1f}')} / {num(p95, '{:.1f}')} | "
          f"{num(m.get(STEP_NAME[FIRST] + '一步用时中位(秒)'), '{:.1f}')} / {num(m.get('回答一步用时中位(秒)'), '{:.1f}')} | {num(m['回答一步平均输入token'], '{:,.0f}')} | {accuracy} |")

if RAG:
    # 官方 rag-qa 的回答状态：answered（有结论）、insufficient-evidence（证据不足）、conflicting-evidence（证据冲突）、needs-clarification（要澄清）。
    print("\n## 官方回答状态（rag-qa）\n")
    print("| 格子 | answered | insufficient-evidence | conflicting-evidence | needs-clarification | 模型输出不合格 | 程序或网络出错 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for c in cells:
        ts = raw[c["cell"]]["tasks"]
        n = lambda status: sum(1 for t in ts if t["ok"] and t.get("ragStatus") == status)
        print(f"| {c['cell']} | {n('answered')} | {n('insufficient-evidence')} | {n('conflicting-evidence')} | {n('needs-clarification')} | "
              f"{sum(1 for t in ts if not t['ok'] and t.get('failKind') == 'model-output')} | {sum(1 for t in ts if not t['ok'] and t.get('failKind') != 'model-output')} |")

def same_steps(cell_a, cell_b):
    """两格 Ditto 发给出口的请求（出口加防缓存编号之前）按步骤逐字比。按（题、步骤、第几次）比，分母是两边的并集：
    只在一边出现的请求（例如只有一边重来了一次回答）单独列出。"""
    A = {(r["task"], r["kind"]): r["sha256"] for r in own_rows(cell_a)}
    B = {(r["task"], r["kind"]): r["sha256"] for r in own_rows(cell_b)}
    def same(k):
        keys = [key for key in set(A) | set(B) if base(key[1]) == k]
        only = sum(1 for key in keys if (key in A) != (key in B))
        return f"{sum(1 for key in keys if key in A and key in B and A[key] == B[key])}/{len(keys)}" + (f"（只在一边 {only}）" if only else "")
    return " / ".join(same(k) for k in steps)


sec = lambda v: None if v in (None, "") else float(v) / 1000  # 没有稳态成功题时 p95 是空的
cell_of = lambda layout, rnd: next(c for c in cells if c["layout"] == layout and c["round"] == rnd)
pooled_row = lambda p: p["round"].startswith("两轮合并")


def same_or_dash(base_layout, p):
    """两轮合并的行不逐请求比（各轮的比较在上面各行）。"""
    return "—" if pooled_row(p) else same_steps(cell_of(base_layout, p["round"])["cell"], cell_of(p["layout"], p["round"])["cell"])

print("\n## 拆开对 Monolithic（同一轮、同一并发，2 核；“两轮合并”是两轮吞吐之和的比，主数看它）\n")
print(f"| 拆法 | 轮 | 吞吐：Monolithic → 拆开（变化） | 每题用时 p95：Monolithic → 拆开（变化） | "
      f"Ditto 发给出口的请求逐字相同（出口加防缓存编号之前）：{' / '.join(STEP_NAME[k] + '一步' for k in steps)} |")
print("| --- | --- | --- | --- | --- |")
for p in sorted(pairs, key=lambda p: (p["layout"], p["round"])):
    print(f"| {NAME.get(p['layout'], p['layout'])} | {p['round']} | {num(p['mono_throughput'], '{:.3f}')} → {num(p['split_throughput'], '{:.3f}')}（{pct(p['throughput_gain'])}） | "
          f"{num(sec(p['mono_p95_ms']), '{:.1f}')} → {num(sec(p['split_p95_ms']), '{:.1f}')}（{pct(-float(p['p95_cut']) if p['p95_cut'] else None)}） | "
          f"{same_or_dash('mono', p)} |")

# 对 Direct（docs\\31）：Direct 不经过 Ditto 的运行时（图、循环、路由、调度），同样的步骤在一个进程里直接调用，仍用 Ditto 的 Worker 处理函数。
# Monolithic 对 Direct 是 Ditto 运行时本身的代价；各种拆法对 Direct 是拆开以后比“不经过运行时的写法”快还是慢。
pairs_direct = read_csv(OUT / "pairs-direct.csv")
if pairs_direct:
    print("\n## 对 Direct（不经过 Ditto 运行时、仍用 Ditto 的 Worker 处理函数；同一轮、同一并发，2 核）\n")
    print(f"| 布局 | 轮 | 吞吐：Direct → 这一布局（变化） | 每题用时 p95：Direct → 这一布局（变化） | "
          f"请求逐字相同：{' / '.join(STEP_NAME[k] + '一步' for k in steps)} |")
    print("| --- | --- | --- | --- | --- |")
    for p in sorted(pairs_direct, key=lambda p: (p["layout"] != "mono", p["layout"], p["round"])):
        print(f"| {NAME.get(p['layout'], p['layout'])} | {p['round']} | {num(p['direct_throughput'], '{:.3f}')} → {num(p['layout_throughput'], '{:.3f}')}（{pct(p['throughput_gain'])}） | "
              f"{num(sec(p['direct_p95_ms']), '{:.1f}')} → {num(sec(p['layout_p95_ms']), '{:.1f}')}（{pct(-float(p['p95_cut']) if p['p95_cut'] else None)}） | "
              f"{same_or_dash('direct', p)} |")
    print("\nMonolithic 对 Direct 的差别预计很小（每题主线程多几毫秒），两轮之间的波动可能比它大；看下面“每题主线程忙多久”一表更准。")

# 合并对照（docs\\31）：MEMORY 和 CONTEXT 合进一个进程对各占一个进程。跨进程的调用两边完全一样，只差两个重活共用一条主线程还是各用一条。
pairs_memctx = read_csv(OUT / "pairs-memctx.csv")
if pairs_memctx:
    print("\n## 合并对照：MEMORY 和 CONTEXT 合进一个进程 对 各占一个进程（同一轮、同一并发，2 核）\n")
    print("| 轮 | 吞吐：两个都拆 → 合进一个进程（变化） | 每题用时 p95：两个都拆 → 合进一个进程（变化） | 请求逐字相同 |")
    print("| --- | --- | --- | --- |")
    for p in pairs_memctx:
        print(f"| {p['round']} | {num(p['both_throughput'], '{:.3f}')} → {num(p['memctx_throughput'], '{:.3f}')}（{pct(p['throughput_gain'])}） | "
              f"{num(sec(p['both_p95_ms']), '{:.1f}')} → {num(sec(p['memctx_p95_ms']), '{:.1f}')}（{pct(-float(p['p95_cut']) if p['p95_cut'] else None)}） | "
              f"{p['requests_same'] or '—'} |")

# 每题主线程忙多久、每题 CPU：窗口里的总数 ÷ 窗口里完成的题数
print("\n## 每题主线程忙多久、每题用掉的 CPU（稳态窗口，毫秒）\n")
procs = [n for n in ("orchestrator", "memory", "context", "memctx", "interaction") if any(c.get(f"{n}_busy_ms_per_task") for c in cells)]
pname = {"orchestrator": "编排器", "memory": "MEMORY 进程", "context": "CONTEXT 进程", "memctx": "MEMORY+CONTEXT 进程", "interaction": "INTERACTION 进程"}
print("| 格子 | 布局 | 吞吐（题/秒） | " + " | ".join(f"{pname[n]}主线程" for n in procs) + " | 各进程合计 CPU | 整台虚拟机 CPU 忙 |")
print("| --- | --- | --- | " + " | ".join("---" for _ in procs) + " | --- | --- |")
for c in cells:
    print(f"| {c['cell']} | {NAME.get(c['layout'], c['layout'])} | {num(c['throughput_per_s'], '{:.3f}')} | "
          + " | ".join(num(c.get(f'{n}_busy_ms_per_task'), '{:.0f}') for n in procs)
          + f" | {num(c.get('cpu_ms_per_task'), '{:.0f}')} | {num(c.get('vm_cpu_busy_share'), '{:.0%}')} |")
segs = [k for k in ("throughput_seg_20_199", "throughput_seg_199_349", "throughput_seg_349_end") if any(c.get(k) for c in cells)]
if segs:
    print("\n按派题的序号分段的吞吐（题/秒；500 题一格里记忆库越来越大。第一段“第 20 题做完到第 199 题派出”可以和以前 200 题的结果比）：\n")
    print("| 格子 | 第 20–199 题 | 第 199–349 题 | 第 349 题–最后 |")
    print("| --- | --- | --- | --- |")
    for c in cells:
        print(f"| {c['cell']} | " + " | ".join(num(c.get(k), '{:.3f}') for k in segs) + " |")

if not acc:
    print("\n## 正确率\n\n**判分还没有结果**（judge\\accuracy.json 不在：没判、判分中途出错，或者被停下了）。")
if acc:
    print("\n## 正确率（GPT-4o 判分，LongMemEval 官方口径）\n")
    used = {acc["judge_model_requested"], *acc["judge_model_returned"]}
    if used != {"gpt-4o-2024-08-06"}:
        print(f"**不是官方口径**：判分用到的模型是 {', '.join(sorted(used))}（开了 JUDGE_ALLOW_OTHER_MODEL），官方是 gpt-4o-2024-08-06。\n")
    if not acc.get("complete", True):
        print(f"**判分没做完**：{acc.get('unjudged_answers_in_cells')} 段回答没判成"
              + (f"（{acc['stopped']}）" if acc.get("stopped") else "") + "。下面的数不完整，先别用；处理好以后再跑 `bash scripts/real.sh judge`。\n")
    print("| 比较 | A | B | 共同题数（排除） | A 正确率 | B 正确率 | 只有 A 对 / 只有 B 对 | 差 B − A，百分点（95% 区间） | 符号检验 p | Holm 校正后 p |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in acc["pairs"]:
        excluded = p["排除的题（没跑完或没判成）"]
        if not p["共同题数"]:
            print(f"| {p['比较']} | {p['A']} | {p['B']} | 0（{excluded}） | — | — | — | — | — | — |")
            continue
        holm = p.get("符号检验p_Holm校正（全部比较一起）")
        diff = "{:+.1f}（{:+.1f} 到 {:+.1f}）".format(p["正确率差B减A"] * 100, p["差的95%区间下限"] * 100, p["差的95%区间上限"] * 100)
        print(f"| {p['比较']} | {p['A']} | {p['B']} | {p['共同题数']}（{excluded}） | {p['A正确率'] * 100:.1f}% | {p['B正确率'] * 100:.1f}% | "
              f"{p['只有A对']} / {p['只有B对']} | {diff} | {p['符号检验p']:.3f} | {'—' if holm is None else f'{holm:.3f}'} |")
    print("\np > 0.05 只说明没有检出差别，不等于证明相同；要看差值的区间有多窄，并和“同一布局跑两次”的参照比。"
          "区间是 Agresti–Min 配对区间。“两轮合并”同一题算了两次，区间偏窄，只作描述。"
          "主比较是各拆法对 Monolithic 的“两轮合并”；比较很多，另给全部比较一起做 Holm 校正后的 p。")
    print(f"判分：请求的模型 {acc['judge_model_requested']}，中转返回的模型 {', '.join(acc['judge_model_returned']) or '—'}；判分失败 {acc.get('judge_failures', 0)} 段。")

print("\n## 模型和费用（每格）\n")
print("| 格子 | 模型请求（成功） | 重试多发 | 缓存命中占输入 | 费用：高峰价 / 按时段（元） |")
print("| --- | --- | --- | --- | --- |")
for m in model:
    print(f"| {m['格子']} | {m['模型请求']}（{m['成功']}） | {m['重试多发的请求']} | {num(m['缓存命中占输入'], '{:.1%}')} | {m['费用_高峰价(元)']:.2f} / {m['费用_按时段(元)']:.2f} |")

ledger = OUT / "egress" / "ledger.jsonl"
if ledger.exists():
    def records(path):
        """逐行读；写到一半的残片（出口被强杀时留下的，出口重启时会在后面补一行标记）跳过并计数。
        只按 "\\n" 分行：splitlines 还会在 U+2028 之类的字符处断开，模型回答原文里可能有。"""
        out, skipped = [], 0
        # 出口被强杀时最后一行可能写到半个中文字符：按替换读，坏行跳过
        for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
        return out, skipped
    spent = held = 0
    events, skipped = records(ledger)
    for e in events:
        spent += e["micro"] if e["t"] == "settle" else 0
        held += e["micro"] if e["t"] == "retain" else 0
    requests, skipped_requests = records(OUT / "egress" / "requests.jsonl")
    if skipped or skipped_requests:
        print(f"\n注意：账本里有 {skipped} 行、请求记录里有 {skipped_requests} 行写到一半（出口被强杀过），已跳过。")
    by_time = by_time_cny(requests)
    print(f"\n全部（含冒烟）：{len(requests)} 个模型请求，成功 {sum(1 for r in requests if r['ok'])}，实际发往 DeepSeek {sum(r['upstream_calls'] for r in requests)} 次；"
          f"账本按高峰价已结清 {spent / 1e6:.2f} 元，费用不明而保留 {held / 1e6:.2f} 元；按发出时刻折算约 {by_time + held / 1e6:.2f} 元（保留部分按高峰价算）。"
          "这是账本估算，没有和 DeepSeek 账单核对。")
    late_file = OUT / "egress" / "late.jsonl"
    if late_file.exists():
        late, _ = records(late_file)
        if late:
            print(f"\n被出口放弃（超时）以后又自己完成的请求 {len(late)} 个，DeepSeek 照常收费约 {sum(e.get('micro') or 0 for e in late) / 1e6:.2f} 元"
                  "（已经算在上面的“费用不明而保留”里，账本没有再加）。明细在 egress\\late.jsonl。")
