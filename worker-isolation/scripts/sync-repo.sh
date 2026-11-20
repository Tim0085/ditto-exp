#!/usr/bin/env bash
# 把本实验的程序同步到公开仓库 ditto-exp 的 worker-isolation\（仓库只放代码，结果、数据、密钥都不放）。
# 官方文件的副本也不放（上游没有声明许可证），仓库里用 scripts/fetch-official.sh 从上游取。
#
#   bash scripts/sync-repo.sh        仓库路径取 local.env 的 DITTO_EXP_REPO
#
# 同步的：src、scripts、tests、report、patches 五个目录（整个换掉，删掉的文件仓库里也删）和 package.json、tsconfig.json、
# local.env.example、official/SOURCE.md、rag-a/CHANGES.md、relaxed/CHANGES.md。仓库自己的 README.md、代码导读.md 不动。
# 同步完在仓库里查一遍：密钥前缀、像密钥的串、邮箱、本机路径、私密目录的路径，有就报错（文件已复制，不提交就行）。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f local.env ]; then set -a; . ./local.env; set +a; fi
REPO=${DITTO_EXP_REPO:?在 local.env 里写 DITTO_EXP_REPO=<ditto-exp 仓库的路径>}
DEST=$REPO/worker-isolation
[ -d "$REPO/.git" ] || { echo "$REPO 不是 git 仓库" >&2; exit 1; }
mkdir -p "$DEST"
for d in src scripts tests report patches; do rm -rf "${DEST:?}/$d"; cp -r "$d" "$DEST/$d"; done
for f in package.json tsconfig.json local.env.example official/SOURCE.md rag-a/CHANGES.md relaxed/CHANGES.md; do
  mkdir -p "$DEST/$(dirname "$f")"; cp "$f" "$DEST/$f"
done
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +

bad=0
hit() { echo "不能公开（$1）：" >&2; echo "$2" | head -5 >&2; bad=1; }
[ -n "${DEEPSEEK_KEY_PREFIX:-}" ] || echo "提醒：没有设 DEEPSEEK_KEY_PREFIX（local.env），查不了真实的密钥前缀" >&2
if [ -n "${DEEPSEEK_KEY_PREFIX:-}" ]; then
  out=$(grep -rnF --exclude-dir=.git -e "$DEEPSEEK_KEY_PREFIX" "$REPO" || true); [ -z "$out" ] || hit "DeepSeek 密钥前缀" "$out"
fi
# 像密钥的串：sk- 前面不紧挨字母、数字、下划线、连字符（和出口的打码规则一样，task-based 这样的词不算）；
# 测试里的假密钥（sk-tst…、sk-other…、sk-zzzz…）和演练用的 sk-rehearse 除外
out=$(grep -rnoE --exclude-dir=.git -e "(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{6,}" "$REPO" | grep -vE "sk-(tst|other|zzzz|rehearse)" || true)
[ -z "$out" ] || hit "像密钥的串" "$out"
out=$(grep -rnoE --exclude-dir=.git --include='*.ts' --include='*.py' --include='*.sh' --include='*.md' --include='*.json' --include='*.yaml' --include='*.example' \
  -e "[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}" "$REPO" || true)
[ -z "$out" ] || hit "邮箱" "$out"
out=$(grep -rnE --exclude-dir=.git -e "experiments[/\\\\]private|Users[/\\\\]|[A-Za-z]:[/\\\\]Academic" "$REPO" || true); [ -z "$out" ] || hit "本机路径或私密目录" "$out"
for f in local.env .env; do [ ! -e "$DEST/$f" ] || hit "本机配置文件" "$DEST/$f"; done
[ "$bad" = 0 ] || { echo "同步完了，但上面这些不能公开：先改掉再提交" >&2; exit 1; }
echo "已同步到 $DEST，敏感内容检查通过。接着在仓库里：git status，git add -A，git commit"
