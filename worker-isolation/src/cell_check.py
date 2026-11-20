"""每格的验收（real.sh 调用），和离线演练的严格核对。只读结果，不改。

    python -I src/cell_check.py validate <结果文件> <最多程序或网络出错题数> <上游主机> <strict-cost|-> <题数> <并发> <场景>
                                         <最多模型输出不合格题数> <布局> <轮名标签> <热身题数> <是否要求附加文件 0|1>
        打印这一格的小结；合格退出码 0。后面几个参数给空串就不查这一项（以前几轮的格子没有附加文件、没记这些）。
    python -I src/cell_check.py extras <结果文件> <是否要求附加文件 0|1>
        只核对附加文件（每次调用的用时、rag 每题的报告）在、摘要对得上、rag 报告和成功的题一一对应。判分前重新核对用。
    python -I src/cell_check.py rehearsal <演练结果目录>
        离线演练（假模型的回复固定）：各格每题每一步发给出口的请求逐字相同（出口加防缓存编号之前），每题结果相同，
        rag 每题的官方报告逐字相同，每题都跑完。有一处不同就退出码 1。

validate 的检查（docs\\31 第 4 节“一格作废”）：模型上游、设定（场景、题数、并发、布局、轮名、热身、格子名）、
每个序号恰好一题且题号不重复、没有思考内容、费用算得出（冒烟另要求每个成功请求都算得出）、回复了却算不出费用的尝试、
程序或网络出错题数、模型输出不合格题数、稳态窗口有效、附加文件。
"""
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def own_rows(d, stem):
    """只看这一格自己的请求：出口按复位以后收到的记，上一格晚到的会混进来（按 cell 排除）。"""
    return [r for r in d["fake"]["rows"] if r.get("cell", stem) == stem]


def check_extras(path, require):
    """返回问题列表（空就是没问题）。require：全量计划要求两个附加文件一定在。"""
    d = load(path)
    extras = d["meta"].get("extras")
    if extras is None:
        return ["没有附加文件（meta.extras）"] if require else []
    problems = []
    need = ["calls.json.gz"] + (["reports.jsonl.gz"] if d["meta"]["scenario"] == "rag" else [])
    for k in need:
        e = extras.get(k)
        p = Path(path).with_name(e["file"]) if e else None
        # 附加文件必须是这一格自己的（文件名跟着结果文件名），摘要对得上
        if not e or e["file"] != f"{Path(path).stem}.{k}" or not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != e["sha256"]:
            problems.append(f"附加文件 {k} 缺了、不是这一格的或摘要对不上")
    if problems:
        return problems
    if d["meta"]["scenario"] == "rag":
        reports = [json.loads(l) for l in gzip.decompress(Path(path).with_name(extras["reports.jsonl.gz"]["file"]).read_bytes()).decode("utf-8").split("\n") if l.strip()]
        got = sorted((r["i"], r["id"]) for r in reports)
        want = sorted((t["i"], t["id"]) for t in d["tasks"] if t["ok"])
        if got != want:
            problems.append(f"rag 报告（{len(got)} 份）和跑完的题（{len(want)} 题）对不上")
    return problems


def remote_call_warnings(path):
    """被拆出各进程的每次调用记录：取不到的进程（只提醒，吞吐、正确率不受影响）。"""
    d = load(path)
    extras = d["meta"].get("extras") or {}
    if "calls.json.gz" not in extras:
        return []
    try:
        calls = json.loads(gzip.decompress(Path(path).with_name(extras["calls.json.gz"]["file"]).read_bytes()))
    except (OSError, ValueError) as e:  # 文件缺了或坏了：check_extras 已经报了，这里不再崩
        return [f"读不了每次调用的记录（{e}）"]
    remote = calls.get("remote") or {}
    expected = sorted(((d.get("window") or {}).get("remote") or {}).keys())
    out = [f"{name}：{v['error']}" for name, v in remote.items() if isinstance(v, dict) and "error" in v]
    out += [f"{name}：没有记录" for name in expected if name not in remote]
    return out


def validate(a):
    path, maxfail, host_want, strict, count, conc, scen, maxmodel = a[0], int(a[1]), a[2], a[3], int(a[4]), int(a[5]), a[6], int(a[7] or 0)
    layout, label, warm, require = (a[8:12] + ["", "", "", "0"])[:4]
    d = load(path)
    m, f, w = d["meta"], d["meta"]["failedTasks"], d.get("window")
    stem = Path(path).stem
    model_fail = sum(1 for t in d["tasks"] if not t["ok"] and t.get("failKind") == "model-output")
    infra_fail = f - model_fail
    rows = own_rows(d, stem)
    lat = sorted(r["latency_ms"] for r in rows if r["ok"] and r["kind"] == "answer" and r.get("latency_ms") is not None)
    print(f"    跑完 {len(d['tasks']) - f}/{len(d['tasks'])} 题，失败 {f}" + (f"（其中模型输出不合格 {model_fail}）" if model_fail else "")
          + (f"；吞吐 {w['throughput_per_s']:.3f} 题/秒" if w else "；稳态窗口无效"),
          f"；回答一步模型用时中位 {lat[len(lat)//2]/1000:.1f} 秒" if lat else "",
          f"；本格 DeepSeek 费用（高峰价）{sum(r['micro_cny'] or 0 for r in rows)/1e6:.2f} 元", sep="")
    bad = []
    hostgot = (m.get("fakeSettings") or {}).get("upstreamHost")
    if hostgot != host_want:
        bad.append(f"模型上游是 {hostgot}，不是 {host_want}：这一格不是预期的模型" + (f"（出口读数没取到：{m['fakeStatsError']}）" if m.get("fakeStatsError") else ""))
    if not m.get("real") or m["scenario"] != scen or m["count"] != count or m["concurrency"] != conc or len(d["tasks"]) != count:
        bad.append(f"格子的设定不对：real={m.get('real')} 场景 {m['scenario']}（应为 {scen}） 题数 {m['count']} 并发 {m['concurrency']}")
    if layout and m["layout"] != layout:
        bad.append(f"布局是 {m['layout']}，计划是 {layout}")
    # 格子名、轮名、热身：防止别的格子的结果被改了名混进来
    if m.get("tag") != stem:
        bad.append(f"结果里记的格子名是 {m.get('tag')}，文件名是 {stem}")
    if label and m.get("label") != label:
        bad.append(f"轮名标签是 {m.get('label')}，应为 {label}")
    if warm and m.get("warmup") != int(warm):
        bad.append(f"热身 {m.get('warmup')} 题，应为 {warm}")
    # 每个序号恰好一题，题号不重复（题目文件不够时 run.ts 会循环凑数，这里拦下）
    if sorted(t["i"] for t in d["tasks"]) != list(range(count)) or len({t["id"] for t in d["tasks"]}) != len(d["tasks"]):
        bad.append("序号不是 0 到题数减 1 各一题，或者有重复的题")
    if any(r["reasoning_chars"] for r in rows):
        bad.append("出现了思考内容：关闭思考没生效")
    # 冒烟（最多失败 0 题）另外要求每个成功的请求都算得出费用：算不出就按全额预扣记账，正式各格会提前撞上限。
    unknown = [r for r in rows if r["ok"] and r["micro_cny"] is None]
    if strict == "strict-cost" and unknown:
        bad.append(f"有 {len(unknown)} 个请求算不出费用（{unknown[0]['error']}），usage：{unknown[0]['usage']}")
    # 费用不明只允许来自没拿到回复的尝试（超时放弃、连接断开）。回复了 2xx 却没有可用的 usage 或读不出，说明接口格式变了，
    # 这一格不算（docs\21 第 3 节）。逐次尝试看，不只看请求的最终状态。
    unreadable = [(r["task"], t["outcome"]) for r in rows for t in (r.get("tries") or []) if t.get("outcome") in ("usage-unknown-held", "unreadable-held")]
    if unreadable:
        bad.append(f"有 {len(unreadable)} 次尝试回复了却算不出费用（第一个：{unreadable[0]}）")
    held = sum(1 for r in rows for t in (r.get("tries") or []) if str(t.get("outcome", "")).endswith("-held"))
    if held:
        print(f"    有 {held} 次尝试没拿到回复（超时或断线），按全额预扣保留")
    if infra_fail:
        print("    第一个程序或网络错误：", next(t.get("error") for t in d["tasks"] if not t["ok"] and t.get("failKind") != "model-output"))
    if model_fail:
        print("    第一个模型输出不合格：", next(t.get("error") for t in d["tasks"] if not t["ok"] and t.get("failKind") == "model-output"))
    if infra_fail > maxfail:
        bad.append(f"程序或网络出错 {infra_fail} 题，超过 {maxfail}")
    if model_fail > maxmodel:
        bad.append(f"模型输出不合格 {model_fail} 题，超过 {maxmodel}")
    if w is None:
        bad.append(f"稳态窗口无效：{d.get('windowError')}")
    bad += check_extras(path, require == "1")
    for warn in remote_call_warnings(path):
        print(f"    提醒：被拆出进程的每次调用用时没取全（{warn}）；吞吐、正确率不受影响")
    if m.get("closeErrors"):
        print(f"    提醒：收尾时关闭出错 {m['closeErrors']}")
    for b in bad:
        print("    " + b)
    return 0 if not bad else 1


def rehearsal(out):
    """离线演练的严格核对：假模型的回复固定，所以各格的一切都应当逐字相同。"""
    cells = {}
    for p in sorted(Path(out, "raw").glob("*.json")):
        d = load(p)
        rows = own_rows(d, p.stem)
        req = {}
        for r in rows:
            k = (r["task"], r["kind"])
            if k in req:
                print(f"{p.stem}：同一（题、步骤）记了两次 {k}")
                return 1
            req[k] = r["sha256"]
        tasks = {t["i"]: t for t in d["tasks"]}
        res = {i: (t["id"], t["ok"], t.get("answer"), t.get("queries"), t.get("hits"), t.get("ragStatus"), t.get("evidence"), t.get("claims")) for i, t in tasks.items()}
        reports = {}
        extras = d["meta"].get("extras") or {}
        if "reports.jsonl.gz" in extras:
            for l in gzip.decompress(p.with_name(extras["reports.jsonl.gz"]["file"]).read_bytes()).decode("utf-8").split("\n"):
                if l.strip():
                    r = json.loads(l)
                    reports[r["i"]] = json.dumps(r["report"], ensure_ascii=False, sort_keys=False)
        cells[p.stem] = {"layout": d["meta"]["layout"], "req": req, "res": res, "reports": reports,
                         "failed": [t["i"] for t in d["tasks"] if not t["ok"]]}
    if len(cells) < 2:
        print(f"{out}\\raw 里只有 {len(cells)} 格，没法比")
        return 1
    names = list(cells)
    ref = cells[names[0]]
    bad = 0
    for n in names:
        c = cells[n]
        if c["failed"]:
            print(f"{n}（{c['layout']}）：有题没跑完 {c['failed'][:5]}")
            bad += 1
        if n == names[0]:
            continue
        keys = set(ref["req"]) | set(c["req"])
        diff = sorted(k for k in keys if ref["req"].get(k) != c["req"].get(k))
        if diff:
            print(f"{n}（{c['layout']}）对 {names[0]}（{ref['layout']}）：{len(diff)}/{len(keys)} 个请求不同，第一个 {diff[0]}")
            bad += 1
        rdiff = sorted(i for i in set(ref["res"]) | set(c["res"]) if ref["res"].get(i) != c["res"].get(i))
        if rdiff:
            print(f"{n}（{c['layout']}）对 {names[0]}：{len(rdiff)} 题的结果不同，第一个序号 {rdiff[0]}")
            bad += 1
        pdiff = sorted(i for i in set(ref["reports"]) | set(c["reports"]) if ref["reports"].get(i) != c["reports"].get(i))
        if pdiff:
            print(f"{n}（{c['layout']}）对 {names[0]}：{len(pdiff)} 题的 rag 报告不同，第一个序号 {pdiff[0]}")
            bad += 1
    layouts = sorted({c["layout"] for c in cells.values()})
    n_req = len(ref["req"])
    if bad:
        print(f"离线演练核对没过：{bad} 处不同（见上面）")
        return 1
    print(f"离线演练核对通过：{len(cells)} 格（{', '.join(layouts)}），每格 {len(ref['res'])} 题、{n_req} 个模型请求，"
          f"各格的请求（出口加防缓存编号之前）、每题结果{'、rag 报告' if ref['reports'] else ''}逐字相同，全部跑完")
    return 0


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "validate":
        sys.exit(validate(args))
    if cmd == "extras":
        problems = check_extras(args[0], args[1] == "1")
        for p in problems:
            print("    " + p)
        sys.exit(1 if problems else 0)
    if cmd == "rehearsal":
        sys.exit(rehearsal(args[0]))
    raise SystemExit(f"不认识的命令 {cmd}")
