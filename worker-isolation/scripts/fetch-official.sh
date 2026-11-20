#!/usr/bin/env bash
# 取 Ditto 官方文件。公开仓库（ditto-exp）里不放官方文件的副本：上游 CodeSoul-co/Ditto 没有声明许可证。
# 从上游同一个提交（9cde78b，本实验各轮用的就是它）取本实验用到的官方文件放进 official\，再按 patches\MANIFEST 生成
# rag-a\、relaxed\（官方文件加本实验的几处小改动，改了什么见 rag-a\CHANGES.md、relaxed\CHANGES.md 和 patches\*.patch），
# 最后用 patches\SHA256SUMS 逐个核对：和实验时用的文件逐字节相同。官方 redis 客户端按官方锁文件 npm ci 装。
#
#   bash scripts/fetch-official.sh [上游仓库地址或本地克隆的路径]     默认 https://github.com/CodeSoul-co/Ditto.git
#
# 文件都已经在（例如实验目录本身）：只核对，不覆盖。
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
UPSTREAM=${1:-https://github.com/CodeSoul-co/Ditto.git}
COMMIT=9cde78bbac882cc0467bfc03437c15698d99cc3a
DEPS=official/examples/_shared/tools/storage/dependencies

if [ -f official/ditto.yaml ] || [ -d rag-a/examples ] || [ -d relaxed/examples ]; then
  sha256sum -c --quiet patches/SHA256SUMS || { echo "official\\、rag-a\\、relaxed\\ 已经有文件，但和清单对不上：先人工核对（这个脚本不覆盖已有文件）" >&2; exit 1; }
  echo "官方文件都在，和实验时用的逐字节相同"; exit 0
fi
work=.fetch-official
rm -rf "$work"
# 关掉换行符转换：Windows 上 git 默认把 LF 换成 CRLF，摘要就对不上了
git -c core.autocrlf=false clone --quiet --no-checkout "$UPSTREAM" "$work"
git -C "$work" -c core.autocrlf=false checkout --quiet "$COMMIT"
grep -v '^#' patches/MANIFEST | while read -r target source patchfile; do
  [ -n "$target" ] || continue
  mkdir -p "$(dirname "$target")"
  cp "$work/$source" "$target"
  if [ "$patchfile" != - ]; then patch -s --no-backup-if-mismatch "$target" < "patches/$patchfile"; fi
done
rm -rf "$work"
sha256sum -c --quiet patches/SHA256SUMS
(cd "$DEPS" && npm ci --ignore-scripts --no-audit --no-fund)
echo "官方文件取好了，和实验时用的逐字节相同；官方 redis 客户端已装（$DEPS/node_modules）"
