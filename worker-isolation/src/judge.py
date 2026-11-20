"""GPT-4o 判分：LongMemEval 官方口径。docs\\19 第 4 节。

照官方 src/evaluation/evaluate_qa.py（github.com/xiaowu0162/LongMemEval，提交 9e0b455f）：
- 提示词：get_anscheck_prompt 的五种模板，一字未改（下面原样复制）；题号含 “_abs” 的用“无法回答”那一种；
- 参数：model gpt-4o-2024-08-06、n=1、temperature=0、max_tokens=10；
- 判定：回复里含 yes（不分大小写）就算对。
- 回答先 strip 再判（官方 run_generation.py 370 行）。
和官方不同的地方：
- 同一道题、文字完全相同的回答只判一次（temperature 0，同一段文字判两次结果应当相同）：省钱，相同的回答也得到相同的判定；
- 8 个请求并发；失败最多重试 5 次（官方用 backoff 一直重试）。重试完还失败（例如被中转拒绝）的记为“判分失败”，单列，
  其余照判；再跑一次会重判这些；
- 每格正确率给两种：全部题（没跑完、没判成的算错）和只算跑完且判过的题。两两比较只用两边都跑完、都判过的题。

    python -I src/judge.py --check <GPT-4o 文件>                     检查文件填好没有（不联网，不打印密钥）
    python -I src/judge.py --smoke --key <GPT-4o 文件>                两条已知答案的样例：确认中转能用、判得对
    python -I src/judge.py --key <GPT-4o 文件> --cells <原始记录目录> --lme <longmemeval_s_cleaned.json> --out <目录>
"""
import argparse
import csv
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from math import comb
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
OFFICIAL = "xiaowu0162/LongMemEval src/evaluation/evaluate_qa.py @ 9e0b455f4ef0e2ab8f2e582289761153549043fc"
PLACEHOLDER = "把这里"


# ---------------------------------------------------------------- 官方提示词（原样复制，evaluate_qa.py 24–43 行）

def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response)
    return prompt


# ---------------------------------------------------------------- 调 GPT-4o

def load_key(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    base, key, model = (str(cfg.get(k, "")).strip() for k in ("baseUrl", "apiKey", "model"))
    problems = []
    if not base or PLACEHOLDER in base or not base.startswith(("https://", "http://")):
        problems.append("baseUrl 没填，或者不是 http(s):// 开头的地址")
    if not key or PLACEHOLDER in key:
        problems.append("apiKey 没填")
    if not model:
        problems.append("model 没填（官方是 gpt-4o-2024-08-06）")
    if problems:
        raise SystemExit("GPT-4o 文件 {}：{}".format(Path(path).name, "；".join(problems)))
    if base.startswith("http://"):
        print("提醒：GPT-4o 中转地址是 http://，密钥会明文传输；中转支持的话改成 https://", file=sys.stderr)
    return base.rstrip("/"), key, model


class CallLimit(Exception):
    """判分请求次数到了上限：整批停下。"""


class JudgeFailed(Exception):
    """这一条重试完还失败：记下来，其余照判。"""


class JudgeFatal(Exception):
    """鉴权、余额、权限出错，或中转返回的不是 gpt-4o：整批停下，已判的保留。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：Python 默认跟随时会把 Authorization 头带到新地址（包括别的主机、http 地址）。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused; put the final address in baseUrl", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect)


class Judge:
    def __init__(self, key_file, max_calls, allow_other_model=False, stop_file=None):
        self.base, self.key, self.model = load_key(key_file)
        self.allow_other_model = allow_other_model
        self.stop_file = Path(stop_file) if stop_file else None  # real.sh down 放的停止标记
        self.calls, self.max_calls, self.lock = 0, max_calls, threading.Lock()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.returned_models = set()
        self.fatal = None

    def redact(self, text):
        return str(text).replace(self.key, "***")

    def ask(self, prompt):
        """返回 (回复原文, 是否算对, 中转返回的模型名)。官方：回复 strip 后含 yes 就算对。"""
        body = json.dumps({"model": self.model, "messages": [{"role": "user", "content": prompt}],
                           "n": 1, "temperature": 0, "max_tokens": 10}).encode("utf-8")
        last = "unknown"
        for attempt in range(6):
            with self.lock:
                if not self.fatal and self.stop_file and self.stop_file.exists():
                    self.fatal = "用 real.sh down 停下了"
                if self.fatal:
                    raise JudgeFatal(self.fatal)
                # max_calls 为 0：不设上限（全量两轮的判分不设请求次数上限）
                if self.max_calls and self.calls >= self.max_calls:
                    raise CallLimit(f"判分请求超过上限 {self.max_calls} 次，停下")
                self.calls += 1
            request = urllib.request.Request(self.base + "/chat/completions", data=body, method="POST",
                                             headers={"authorization": "Bearer " + self.key, "content-type": "application/json"})
            try:
                with OPENER.open(request, timeout=60) as response:
                    data = json.loads(response.read().decode("utf-8"))
                returned = data.get("model")
                # 中转返回的模型必须和请求的一样（gpt-4o-mini 之类也以 gpt-4o 开头，不能只看前缀）。
                # 没返回模型名也停：确认不了是官方模型，就不算官方口径（11-20）。
                if (returned is None or str(returned) != self.model) and not self.allow_other_model:
                    self.stop(f"请求的是 {self.model}，中转返回的是 {returned if returned is not None else '（没返回模型名）'}："
                              "停止判分（确实可以接受就加 --allow-other-model，real.sh 里用 JUDGE_ALLOW_OTHER_MODEL=1）")
                    raise JudgeFatal(self.fatal)
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError(f"判分回复为空或不是文字：{content!r}")  # 不当成“判错”，记为判分失败
                content = content.strip()
                with self.lock:
                    self.returned_models.add(str(returned) if returned is not None else "（中转没返回模型名）")
                    for k in self.usage:
                        self.usage[k] += int((data.get("usage") or {}).get(k) or 0)
                return content, "yes" in content.lower(), str(returned) if returned is not None else None
            except JudgeFatal:
                raise
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300] if e.fp else e.msg}"
                if e.code in (401, 402, 403):
                    self.stop(f"判分中转拒绝（{self.redact(last)}）：鉴权、余额或权限问题，停止判分")
                    raise JudgeFatal(self.fatal)
                if 300 <= e.code < 400:  # 拒绝了重定向：地址配得不对，每条都会一样，整批停
                    self.stop(f"判分中转要求重定向（{self.redact(last)}）：把最终地址填进 baseUrl")
                    raise JudgeFatal(self.fatal)
                if e.code < 500 and e.code not in (408, 409, 429):
                    break
            except Exception as e:  # 断线、超时、回复读不懂、回复为空
                last = str(e)[:300]
            time.sleep(min(30, 2 ** attempt))
        raise JudgeFailed(self.redact(last))

    def stop(self, reason):
        with self.lock:
            self.fatal = self.fatal or self.redact(reason)


# ---------------------------------------------------------------- 统计

def sign_test(b, c):
    """双侧符号检验：只有一方答对的题里，两边各占几题。"""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def diff_ci(b, c, n):
    """
    配对的正确率差 B − A = (c − b) / n，和它的 95% 置信区间。
    区间用 Agresti–Min 的做法：2×2 配对表每格加 0.5 再用正态近似，只有一方对的题很少、甚至为 0 时也不会退化成 [0, 0]
    。没有共同题时返回 None。
    """
    if n == 0:
        return None
    d = (c - b) / n
    b2, c2, n2 = b + 0.5, c + 0.5, n + 2
    d2 = (c2 - b2) / n2
    se = max(0.0, b2 + c2 - (c2 - b2) ** 2 / n2) ** 0.5 / n2
    return d, max(-1.0, d2 - 1.96 * se), min(1.0, d2 + 1.96 * se)


def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--key")
    ap.add_argument("--cells")
    ap.add_argument("--lme")
    ap.add_argument("--out")
    ap.add_argument("--max-calls", type=int, default=1500, help="判分请求次数上限；0 是不设上限")
    ap.add_argument("--require-ok", action="store_true", help="只判有验收标记（<格子>.ok）的格子，缺了就报错")
    ap.add_argument("--allow-other-model", action="store_true", help="允许请求官方以外的判分模型（结果就不是官方口径）")
    ap.add_argument("--stop-file", help="这个文件一出现就停止判分（real.sh down 放的）")
    a = ap.parse_args()
    if a.out and not (a.check or a.smoke):
        # 先删掉上一次的正确率表：这次中途出错（包括判分文件没填好、模型不对就退出）时，汇总不会拿旧表当新结果。
        # 判过的标签在 judged.jsonl，照样复用。
        Path(a.out).mkdir(parents=True, exist_ok=True)
        for name in ("accuracy.json", "accuracy.csv", "accuracy-pairs.csv", "judged.csv"):
            (Path(a.out) / name).unlink(missing_ok=True)

    if a.check:
        base, _, model = load_key(a.check)
        # 只打印主机名：地址里万一夹着用户名、密码或令牌，不进日志
        print(json.dumps({"ok": True, "file": Path(a.check).name, "host": urllib.parse.urlsplit(base).hostname, "model": model}, ensure_ascii=False))
        return

    judge = Judge(a.key, a.max_calls, a.allow_other_model, a.stop_file)
    # 冒烟和正式判分用同一条规则：冒烟在 DeepSeek 花钱之前，模型不对要在这里就拦下。
    if judge.model != "gpt-4o-2024-08-06" and not a.allow_other_model:
        raise SystemExit(f"判分模型是 {judge.model}，不是官方的 gpt-4o-2024-08-06；确实要用请加 --allow-other-model（real.sh 里用 JUDGE_ALLOW_OTHER_MODEL=1），结果就不是官方口径")
    if a.smoke:
        cases = [("What degree did I graduate with?", "Business Administration", "You graduated with a degree in Business Administration.", True),
                 ("What degree did I graduate with?", "Business Administration", "I don't know which degree you graduated with.", False)]
        bad = 0
        for q, ans, hyp, expected in cases:
            try:
                raw, label, _ = judge.ask(get_anscheck_prompt("single-session-user", q, ans, hyp))
            except (JudgeFatal, JudgeFailed, CallLimit) as e:
                raise SystemExit(f"判分冒烟没过：{e}")
            print(json.dumps({"response": hyp, "judge_said": raw, "label": label, "expected": expected}, ensure_ascii=False))
            bad += label != expected
        print(json.dumps({"smoke": "ok" if not bad else "wrong", "model_requested": judge.model,
                          "model_returned": sorted(judge.returned_models), "calls": judge.calls}, ensure_ascii=False))
        if not judge.model.startswith("gpt-4o-2024-08-06"):
            print("提醒：请求的不是官方的 gpt-4o-2024-08-06")
        if bad:
            raise SystemExit(1)
        return

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    refs = {e["question_id"]: e for e in json.loads(Path(a.lme).read_text(encoding="utf-8"))}
    cells, seen = [], {}
    for path in sorted(Path(a.cells).glob("*.json")):
        if a.require_ok and not path.with_suffix(".ok").exists():
            raise SystemExit(f"{path.name} 没有验收标记（{path.stem}.ok），不判。先让 real.sh 验收这一格")
        d = json.loads(path.read_text(encoding="utf-8"))
        rnd = d["meta"]["label"].partition("|")[2]
        # 配对按（布局，轮）：两格撞了，后一格会盖掉前一格的标签。
        if (d["meta"]["layout"], rnd) in seen:
            raise SystemExit(f"{path.name} 和 {seen[(d['meta']['layout'], rnd)]} 的布局、轮名都是 {d['meta']['layout']} {rnd}：配对会混，先人工核对")
        seen[(d["meta"]["layout"], rnd)] = path.name
        cells.append({"cell": path.stem, "layout": d["meta"]["layout"], "round": rnd, "tasks": d["tasks"]})
    if not cells:
        raise SystemExit(f"{a.cells} 里没有格子")
    # 两两比较按序号 i 配对：各格里序号 i 必须是同一道题。记录里的题按完成先后排，所以按序号比，不按数组顺序。
    order = {t["i"]: t["id"] for t in cells[0]["tasks"]}
    for c in cells[1:]:
        if {t["i"]: t["id"] for t in c["tasks"]} != order:
            raise SystemExit(f"{c['cell']} 的题目和 {cells[0]['cell']} 对不上（同一序号不是同一道题），不能配对")

    # 已判过的（同一道题、同一段回答、同一个判分模型）直接用，可以断点续判；上次判分失败的这次重判。
    hyp_of = lambda t: t["answer"].strip()  # 官方：回答先 strip（run_generation.py 370 行）
    key_of = lambda t: (t["id"], hashlib.sha256(hyp_of(t).encode("utf-8")).hexdigest())
    cache_file = out / "judged.jsonl"
    cache, failed = {}, {}
    if cache_file.exists():
        # 按 "\n" 分行（splitlines 还会在 U+2028 之类的字符处断开，回答原文里可能有）。
        lines = [l for l in cache_file.read_text(encoding="utf-8", errors="replace").split("\n") if l.strip()]
        good = []
        for n, line in enumerate(lines):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                if n == len(lines) - 1:
                    # 写到一半的最后一行（被强杀）：去掉它，那段回答这次重判；不去掉的话它会夹在新记录中间，下次续判就读不了
                    tmp = cache_file.with_name(cache_file.name + ".partial")
                    tmp.write_text("".join(l + "\n" for l in good), encoding="utf-8")
                    tmp.replace(cache_file)
                    break
                raise
            good.append(line)
            # 只复用同一个判分模型、而且（官方口径时）返回的也正是这个模型判的标签。
            if e.get("label") is not None and e.get("judge_model") == judge.model and (a.allow_other_model or e.get("judge_model_returned") == judge.model):
                cache[(e["question_id"], e["hyp_sha256"])] = e
        with open(cache_file, "a", encoding="utf-8") as f:
            if cache_file.read_text(encoding="utf-8", errors="replace")[-1:] not in ("", "\n"):
                f.write("\n")
    todo = {}
    for c in cells:
        for t in c["tasks"]:
            if t.get("ok") and isinstance(t.get("answer"), str) and key_of(t) not in cache:
                todo[key_of(t)] = hyp_of(t)
    print(f"要判 {len(todo)} 段回答（已判过 {len(cache)}，相同的回答只判一次）")
    write_lock = threading.Lock()

    def work(item):
        (qid, sha), hyp = item
        ref = refs[qid]
        prompt = get_anscheck_prompt(ref["question_type"], ref["question"], ref["answer"], hyp, abstention="_abs" in qid)
        try:
            raw, label, returned = judge.ask(prompt)
            error = None
        except JudgeFatal:
            return  # 整批停：已经判完的都写进了 judged.jsonl，不再发请求
        except CallLimit as e:
            judge.stop(str(e))  # 到了请求上限：和整批停一样，照样写出部分结果（标明没判完），不让异常穿出线程池
            return
        except JudgeFailed as e:
            raw, label, returned, error = None, None, None, str(e)
        entry = {"question_id": qid, "hyp_sha256": sha, "label": label, "judge_response": raw, "error": error, "judge_model": judge.model,
                 "judge_model_returned": returned,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with write_lock:
            with open(cache_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            (cache if label is not None else failed)[(qid, sha)] = entry

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(work, todo.items()))

    # 每题、每格。label：True 判对，False 判错，None 没跑完或判分失败（两两比较时不用）。
    # rag：模型输出过不了官方检查的题（run.ts 记 failKind="model-output"）照官方就是答不出，记 False（不用问 GPT），
    # 两两比较时也算进去，不然一边答对、一边答不出的题会被整对剔掉。程序、网络出错的照旧不算。
    has_fail_kind = any("failKind" in t for c in cells for t in c["tasks"])
    rows, per_cell, labels = [], [], {}
    for c in cells:
        got, gpt = {}, {}
        for t in c["tasks"]:
            ref = refs[t["id"]]
            answered = bool(t.get("ok") and isinstance(t.get("answer"), str))
            model_fail = not t.get("ok") and t.get("failKind") == "model-output"
            entry = (cache.get(key_of(t)) or failed.get(key_of(t))) if answered else None
            gpt[t["i"]] = entry["label"] if entry else None
            label = gpt[t["i"]] if answered else (False if model_fail else None)
            got[t["i"]] = label
            rows.append({"格子": c["cell"], "布局": c["layout"], "轮": c["round"], "序号": t["i"], "题号": t["id"], "题型": ref["question_type"],
                         "跑完": answered, "判对": "" if label is None else label,
                         "判分失败": ((entry or {}).get("error") or ("" if entry else "没判（判分中止）")) if answered
                         else ("模型输出不合格，按答错计" if model_fail else ""),
                         "检索词": " | ".join(t.get("queries") or []), "检索到的记忆条数": t.get("hits"),
                         "结束原因": t.get("finishReason"), "回答": hyp_of(t)[:2000] if answered else "", "标准答案": str(ref["answer"])[:500],
                         "运行错误": t.get("error")})
        labels[(c["layout"], c["round"])] = got
        n = len(got)
        judged = [v for v in gpt.values() if v is not None]  # GPT 判过的（“只算判过的”正确率的分母）
        correct = sum(1 for v in got.values() if v)
        by_type = {}
        for t in c["tasks"]:
            qt = refs[t["id"]]["question_type"] + ("（无法回答）" if "_abs" in t["id"] else "")
            by_type.setdefault(qt, []).append(bool(got[t["i"]]))
        per_cell.append({"格子": c["cell"], "布局": c["layout"], "轮": c["round"], "题数": n,
                         "跑完": sum(1 for t in c["tasks"] if t.get("ok")), "判分失败": sum(1 for t in c["tasks"] if t.get("ok") and got[t["i"]] is None),
                         **({"模型输出不合格": sum(1 for t in c["tasks"] if not t.get("ok") and t.get("failKind") == "model-output")} if has_fail_kind else {}),
                         "判对": correct, "正确率": correct / n if n else 0.0, "正确率_只算判过的": sum(judged) / len(judged) if judged else 0.0,
                         **{f"正确率_{k}": sum(v) / len(v) for k, v in sorted(by_type.items())}})

    def compare(kind, specs):
        """specs：[(A 的布局, 轮), (B 的布局, 轮)] 的列表；多于一对时把只有一方对的题数加起来（两轮合并）。"""
        n = b = cc = a_right = b_right = excluded = 0
        for (la, ra), (lb, rb) in specs:
            A, B = labels.get((la, ra)), labels.get((lb, rb))
            if A is None or B is None:
                return None
            both = [i for i in set(A) & set(B) if A[i] is not None and B[i] is not None]
            excluded += len(set(A) | set(B)) - len(both)
            n += len(both); a_right += sum(A[i] for i in both); b_right += sum(B[i] for i in both)
            b += sum(1 for i in both if A[i] and not B[i]); cc += sum(1 for i in both if B[i] and not A[i])
        row = {"比较": kind, "A": " + ".join(f"{la} {ra}" for (la, ra), _ in specs), "B": " + ".join(f"{lb} {rb}" for _, (lb, rb) in specs),
               "共同题数": n, "排除的题（没跑完或没判成）": excluded}
        if n == 0:  # 没有共同题：不给正确率差、区间和 p 值
            return {**row, "A正确率": None, "B正确率": None, "只有A对": None, "只有B对": None, "正确率差B减A": None,
                    "差的95%区间下限": None, "差的95%区间上限": None, "符号检验p": None, "没有检出差别（p>0.05）": None}
        d, lo, hi = diff_ci(b, cc, n)
        p = sign_test(b, cc)
        return {**row, "A正确率": a_right / n, "B正确率": b_right / n,
                "只有A对": b, "只有B对": cc, "正确率差B减A": d, "差的95%区间下限": lo, "差的95%区间上限": hi,
                "符号检验p": p, "没有检出差别（p>0.05）": p > 0.05}

    # 每种拆法和同一轮的 Monolithic 比；每种布局自己两轮互比作参照（拆 MEMORY 那轮是 2 种布局，拆 CONTEXT 那轮是 3 种）。
    # 轮名带并发时（rag 那一轮：c16-r1、c37-r1……，real.sh 的 round_label），“两轮合并”和“跑两次”按并发分组做；
    # S4 的轮名是 r1、r2，只有一组，结果和以前一样。
    # 有 Direct（不经过 Ditto 运行时，docs\\31）时另比：Monolithic、各种拆法对同一轮的 Direct（A 是 Direct）。
    layouts = sorted({l for l, _ in labels}, key=lambda l: (l != "mono", l != "direct", l))
    rounds = sorted({r for _, r in labels})
    groups = {}
    for r in rounds:
        groups.setdefault(r.rpartition("-")[0], []).append(r)
    specs = []

    def against(base, kind, others):
        """others 里每种布局和同一轮的 base 比，每轮一行；同一组有两轮时再加一行两轮合并。"""
        for s in others:
            specs.extend((f"{kind}：{s}", [((base, r), (s, r))]) for r in rounds)
            for g, rs in groups.items():
                if len(rs) > 1:
                    specs.append((f"{kind}：{s}（{g + ' ' if g else ''}两轮合并；同一题出现两次，只作描述）", [((base, r), (s, r)) for r in rs]))

    against("mono", "拆开对合并", [l for l in layouts if l not in ("mono", "direct")])
    if "direct" in layouts:
        against("direct", "对 Direct", [l for l in layouts if l != "direct"])
    for g, rs in groups.items():
        if len(rs) > 1:
            specs += [(f"参照：{l} 跑两次" + (f"（{g}）" if g else ""), [((l, rs[0]), (l, rs[1]))]) for l in layouts]
    pairs = [x for x in (compare(kind, s) for kind, s in specs) if x]
    # 比较很多（全量 S4 有 30 多行），按 0.05 会碰出一两次假“显著”：另给全部比较一起做 Holm 校正后的 p。
    # 主比较是各拆法对 Monolithic 的“两轮合并”那几行（docs\31 第 4 节），其余是描述和参照。
    ps = sorted((p["符号检验p"], k) for k, p in enumerate(pairs) if p["符号检验p"] is not None)
    running = 0.0
    for rank, (pv, k) in enumerate(ps):
        running = max(running, min(1.0, (len(ps) - rank) * pv))
        pairs[k]["符号检验p_Holm校正（全部比较一起）"] = running

    write_csv(out / "judged.csv", rows)
    write_csv(out / "accuracy.csv", per_cell)
    write_csv(out / "accuracy-pairs.csv", pairs)
    used = sorted({str(e.get("judge_model_returned") or "（中转没返回模型名）") for e in cache.values()})
    unjudged = sum(r["判分失败"] for r in per_cell)
    complete = unjudged == 0 and not judge.fatal
    summary = {"official": OFFICIAL, "judge_model_requested": judge.model, "judge_model_returned": used,
               "judge_calls_this_run": judge.calls, "judge_usage_this_run": judge.usage, "judge_failures": len(failed),
               "unjudged_answers_in_cells": unjudged, "stopped": judge.fatal, "complete": complete,
               "cells": per_cell, "pairs": pairs}
    (out / "accuracy.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in per_cell:
        print(f'{r["格子"]:40s} 正确率 {r["正确率"]:.3f}（{r["判对"]}/{r["题数"]}，跑完 {r["跑完"]}，没判成 {r["判分失败"]}）')
    for p in pairs:
        if p["共同题数"] == 0:
            print(f'{p["比较"]}：{p["A"]} vs {p["B"]}，没有两边都判过的题，不比较')
            continue
        print(f'{p["比较"]}：{p["A"]} {p["A正确率"]:.3f} vs {p["B"]} {p["B正确率"]:.3f}，只有一方对 {p["只有A对"]}/{p["只有B对"]}，'
              f'差 {p["正确率差B减A"]*100:+.1f} 个百分点（95% 区间 {p["差的95%区间下限"]*100:+.1f} 到 {p["差的95%区间上限"]*100:+.1f}），p={p["符号检验p"]:.3f}')
    print(f"本次判分请求 {judge.calls} 次，判分失败 {len(failed)} 段，返回的模型 {sorted(judge.returned_models)}")
    if not complete:
        print(f"判分没做完：{unjudged} 段回答没判成" + (f"；停下的原因：{judge.fatal}" if judge.fatal else "")
              + "。上面的正确率不完整，先别用；处理好以后再跑 bash scripts/real.sh judge，判过的不会重判")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
