"""记下这一轮的运行环境和设定（docs\\31：全量实验的记录要全面，后面不再重跑）。real.sh 在 run、rehearse 开头调用。

    python -I src/run_info.py <结果目录> 键=值 ...      写 <结果目录>/run-info.json

键值是 real.sh 传来的计划内容（plan、formal、count、limit_cny、judge_ref 等），原样记下。另外记：
- 程序：03_memctx_probe 下每个文件的 SHA-256（不含 node_modules、__pycache__、.git、local.env 和 .env 开头的文件、叫 private 的目录、链接）；
- ditto-exp 仓库（local.env 的 DITTO_EXP_REPO）：当前提交、标签、有没有没提交的改动，以及同步进仓库的那些文件（scripts/sync-repo.sh 的清单）
  和这里是否逐字节相同；
- Ditto、RETRIEVAL、yaml 的版本；镜像、容器里的 Node 版本和 CPU 型号；Docker 的版本、核数、内存、正在跑的容器数；
- 数据：题目文件、rag 知识文件清单的摘要；记忆库（Docker 卷里）的摘要；判分参考答案（judge_ref）的摘要。
不读 experiments\\private、local.env 和任何密钥。

已经有 run-info.json（中途断了再跑 run）：新的一份另存为 run-info-<时间>.json，不覆盖第一次的。程序（.md 说明文件除外）、数据、
记忆库、镜像、Ditto 版本和第一次不同就报错退出（前后两半就不是同一个实验；确认无误时设 ALLOW_CODE_CHANGE=1）。
正式运行（mode=run）时镜像、Node 版本、记忆库摘要读不到也报错退出：付费之前先弄清楚。
"""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PROBE = Path(__file__).resolve().parent.parent          # 03_memctx_probe
SEC = PROBE.parent                                       # 实验根目录
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "private"}
# 同步进公开仓库 worker-isolation\ 的（和 scripts/sync-repo.sh 相同）
SYNC_DIRS = ("src", "scripts", "tests", "report", "patches")
SYNC_FILES = ("package.json", "tsconfig.json", "local.env.example", "official/SOURCE.md", "rag-a/CHANGES.md", "relaxed/CHANGES.md")


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def secret_name(name):
    return name == "local.env" or (name.startswith(".env")) or (name.startswith("local.env") and name != "local.env.example")


def code_files():
    """03_memctx_probe 下的文件（相对路径 → 摘要）。不进私密的文件和目录，不跟链接。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(PROBE, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            p = Path(dirpath, name)
            if secret_name(name) or name.endswith(".pyc") or p.is_symlink():
                continue
            out[p.relative_to(PROBE).as_posix()] = sha256_file(p)
    return out


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def strip_credentials(url):
    """地址里万一夹着用户名、密码或令牌（https://user:token@host/...），去掉再记。"""
    return re.sub(r"(?<=://)[^/@]*@", "", url) if url else url


def git_info(code):
    repo = Path(os.environ["DITTO_EXP_REPO"]) if os.environ.get("DITTO_EXP_REPO") else None
    if not repo or not (repo / ".git").exists():
        return {"repo": str(repo) if repo else None, "exists": False}
    g = lambda *a: run(["git", "-C", str(repo), "-c", "core.quotepath=false", *a])
    listed = g("ls-files", "-z", "worker-isolation")
    if listed is None:
        return {"repo": str(repo), "exists": True, "error": "git ls-files 失败"}
    tracked = {f[len("worker-isolation/"):]: f for f in listed.split("\0") if f}
    synced = {k for k in code if k.startswith(tuple(d + "/" for d in SYNC_DIRS)) or k in SYNC_FILES}
    differing = sorted(k for k in synced & set(tracked) if not (repo / tracked[k]).exists() or sha256_file(repo / tracked[k]) != code[k])
    only_here = sorted(synced - set(tracked))
    only_repo = sorted(k for k in tracked if k not in synced and not k.endswith(("README.md", "代码导读.md")))
    return {"repo": str(repo), "exists": True, "commit": g("rev-parse", "HEAD"), "describe": g("describe", "--tags", "--always", "--dirty"),
            "uncommitted": bool(g("status", "--porcelain")), "remote": strip_credentials(g("remote", "get-url", "origin")),
            "synced_files": len(synced), "same_as_here": not differing and not only_here and not only_repo,
            "differing": differing, "only_here": only_here, "only_in_repo": only_repo}


def main():
    out = Path(sys.argv[1])
    plan = dict(a.split("=", 1) for a in sys.argv[2:])
    out.mkdir(parents=True, exist_ok=True)
    code = code_files()
    pkg = lambda name: json.loads((PROBE / "node_modules" / name / "package.json").read_text(encoding="utf-8")).get("version")
    image = plan.get("image", "")
    inside = (run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-c",
                   "node --version; grep -m1 'model name' /proc/cpuinfo | cut -d: -f2; nproc"]) or "").split("\n")
    inside += [None] * 3
    docker = run(["docker", "info", "--format", "{{json .}}"])
    docker = json.loads(docker) if docker else {}
    data_dir = SEC / "bench_data" / "longmemeval-s" / "derived"
    data = {}
    for arg in plan.get("scenario_args", "").split():
        if arg.startswith("/data/"):
            host = SEC / "bench_data" / arg[len("/data/"):]
            data[arg] = {"sha256": sha256_file(host), "bytes": host.stat().st_size} if host.exists() else None
    for name in ("memctx-lme-all500.manifest.json", "rag-a-all500.manifest.json"):
        if (data_dir / name).exists():
            data[name] = json.loads((data_dir / name).read_text(encoding="utf-8")).get("sha256")
    if plan.get("judge_ref") and Path(plan["judge_ref"]).exists():
        data["judge_ref"] = {"file": Path(plan["judge_ref"]).name, "sha256": sha256_file(plan["judge_ref"])}
    store = plan.get("store")
    # 先确认文件在再求摘要（管道里 sha256sum 失败时 cut 照样成功，会得到空串）
    store_sha = run(["docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,src={plan.get('volume')},dst=/store,readonly",
                     "--entrypoint", "/bin/sh", image, "-c", f"test -s /store/{store} && sha256sum /store/{store} | cut -c1-64"], timeout=600) if store else None
    info = {
        "written": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan,
        "code": {"files": code, "count": len(code)},
        "git": git_info(code),
        "versions": {"ditto": pkg("@codesoul-co/ditto"), "ditto-retrieval": pkg("@codesoul-co/ditto-retrieval"), "yaml": pkg("yaml")},
        "image": {"id": run(["docker", "image", "inspect", image, "--format", "{{.Id}} {{.Created}} {{.Os}}/{{.Architecture}}"]),
                  "node": inside[0], "cpu_model": (inside[1] or "").strip() or None, "nproc_in_container": inside[2]},
        "docker": {k: docker.get(k) for k in ("ServerVersion", "OperatingSystem", "OSType", "Architecture", "KernelVersion", "NCPU", "MemTotal",
                                             "CgroupVersion", "ContainersRunning")},
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "data": data, "store": {"file": store, "sha256": store_sha or None},
    }
    missing = [k for k, v in (("镜像", info["image"]["id"]), ("容器里的 Node 版本", info["image"]["node"]), ("记忆库摘要", info["store"]["sha256"])) if not v]
    if missing and plan.get("mode") == "run":
        print(f"读不到：{'、'.join(missing)}。付费之前先弄清楚（Docker 开着吗？镜像、记忆库在吗？）")
        sys.exit(4)
    target = out / "run-info.json"
    if target.exists():
        first = json.loads(target.read_text(encoding="utf-8"))
        changed = sorted(k for k in set(first["code"]["files"]) | set(code)
                         if first["code"]["files"].get(k) != code.get(k) and not k.endswith(".md"))
        same = lambda a, b: a == b
        other = [what for what, a, b in (("数据", first.get("data"), info["data"]), ("记忆库", (first.get("store") or {}).get("sha256"), info["store"]["sha256"]),
                                         ("镜像", (first.get("image") or {}).get("id"), info["image"]["id"]), ("Ditto 版本", first.get("versions"), info["versions"]))
                 if not same(a, b)]
        target = out / f"run-info-{time.strftime('%Y%m%d-%H%M%S')}.json"
        info["changed_since_first_run"] = {"code": changed, "other": other}
        target.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        if (changed or other) and os.environ.get("ALLOW_CODE_CHANGE") != "1" and plan.get("mode") == "run":
            print(f"和第一次 run 时不同：程序 {len(changed)} 个文件 {changed[:10]}；其他 {other}。新的一份记在 {target.name}。"
                  "确认这些变化不影响已跑完的格子以后，加 ALLOW_CODE_CHANGE=1 再跑。")
            sys.exit(3)
    else:
        target.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    g = info["git"]
    print(json.dumps({"run_info": target.name, "code_files": len(code), "git": g.get("describe"), "git_same_as_here": g.get("same_as_here"),
                      "ditto": info["versions"]["ditto"], "node": info["image"]["node"], "cpu": info["image"]["cpu_model"],
                      "containers_running": info["docker"]["ContainersRunning"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
