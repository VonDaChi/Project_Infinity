#!/usr/bin/env bash
# ===========================================================================
#  Project Infinity — Portable (Standalone) Python Setup for Linux
#  思路与 setup.bat 一致：把一份自带的 CPython 装进 python_embeded/，
#  只是 Windows 用官方 embedded zip，Linux 用 python-build-standalone
#  （indygreg 出品、现托管于 astral-sh，uv/rye 同款预编译构建，自带 pip、
#   静态链接、免系统依赖）。下载优先走国内镜像（ghproxy），不可达时回退官方源。
# ===========================================================================

set -uo pipefail

# --- 失败处理（提前定义：错误分支会调用，bash 顺序执行，必须在首次调用前定义） --
goto_download_fail() {
  echo
  echo "============================================================"
  echo " [失败] 自动下载/安装未完成。"
  echo " 可能原因：当前环境无外网访问，或镜像/官方源均不可达。"
  echo
  echo " 手动恢复步骤 (Manual recovery):"
  echo "   [1] 用浏览器下载对应 tarball（indygreg 已重定向至 astral-sh）："
  echo "       https://github.com/${PBS_OWNER2}/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL}"
  echo "   [2] 解压并把内容放入本目录的 python_embeded/ 下（使其含 bin/python3）"
  echo "   [3] 进入 python_embeded/ 所在目录，运行："
  echo "       python_embeded/bin/python3 -m pip install -r requirements.txt -i ${PIP_MIRROR}"
  echo "   [4] 重跑 setup.sh（检测到 python_embeded/bin/python3 后会跳过下载）"
  echo "============================================================"
  exit 1
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYDIR="$ROOT/python_embeded"
PYEXE="$PYDIR/bin/python3"
REQ="$ROOT/requirements.txt"

# --- 守卫：禁止在 Windows 挂载的文件系统（WSL /mnt、NTFS/FAT 等）上运行 --------
# 说明：Windows 与 Linux 各自拥有独立的 python_embeded/（已 gitignore）。若在 WSL 里
# 通过 /mnt/c 访问 Windows 上的项目目录运行，会把 Linux 的 Python 写进 Windows 的
# python_embeded/，触发 symlink 错误并污染 Windows 环境。本脚本仅允许在真正的本机
# Linux 文件系统（ext4 等）上运行。判定依据为文件系统类型（覆盖 WSL drvfs/9p 以及
# 真实 Linux 上直接挂载的 NTFS/FAT），避免误判原生 ext4/btrfs/overlay 等。
_PI_FSTYPE="$(stat -f -c '%T' "$ROOT" 2>/dev/null)"
case "$_PI_FSTYPE" in
  v9fs|9p|drvfs|ntfs|vfat|exfat|fuseblk|fuse.ntfs|fusectl)
    echo "[中止] 检测到在 Windows 挂载的文件系统 ($_PI_FSTYPE) 上运行。"
    echo "       为避免污染 Windows 的 python_embeded/，请勿在 WSL 中通过 /mnt 访问"
    echo "       Windows 项目目录来运行本脚本；请在本机 Linux 文件系统（如"
    echo "       ~/Project_Infinity，ext4）中 clone 本项目后，再运行 setup.sh。"
    exit 1;;
esac

# --- 镜像 / 官方源配置（集中管理，便于更换） ---------------------------------
PBS_OWNER="indygreg"
PBS_OWNER2="astral-sh"
PYVER="3.11.9"
PBS_TAG="20240726"

# 架构 -> python-build-standalone target triple
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  TRIPLE="x86_64-unknown-linux-gnu" ;;
  aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;;
  *) echo "[错误] 不支持的 CPU 架构: $ARCH（仅支持 x86_64 / aarch64）"; exit 1 ;;
esac

TARBALL="cpython-${PYVER}+${PBS_TAG}-${TRIPLE}-install_only.tar.gz"
TMP_ZIP="$ROOT/$TARBALL"

PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_OFFICIAL="https://pypi.org/simple"

# 下载源列表：astral-sh 为主（indygreg 已重定向至此），ghproxy 镜像优先，indygreg 兜底
SRC_LIST=(
  "https://github.com/${PBS_OWNER2}/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL}"
  "https://mirror.ghproxy.com/https://github.com/${PBS_OWNER2}/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL}"
  "https://ghproxy.net/https://github.com/${PBS_OWNER2}/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL}"
  "https://github.com/${PBS_OWNER}/python-build-standalone/releases/download/${PBS_TAG}/${TARBALL}"
)

echo
echo "============================================================"
echo " Project Infinity — Portable Python 环境搭建 (Linux)"
echo "============================================================"
echo

# --- 已存在则跳过下载/安装 ---------------------------------------------------
if [ -x "$PYEXE" ]; then
  echo "[OK] 检测到 $PYEXE 已存在，环境就绪，跳过下载与安装。"
  echo "     如需强制重建，请先删除 python_embeded/ 目录后重跑本脚本。"
  exit 0
fi

# --- 检查必备工具 ------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  echo "[错误] 未找到 curl，请先安装（例如: sudo apt install curl）。"
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "[错误] 未找到 tar，请先安装（例如: sudo apt install tar）。"
  exit 1
fi

# --- 1. 下载 standalone Python tarball（多镜像依次尝试 + 校验 gzip 头） -------
echo "[1/4] 下载 Python ${PYVER} standalone (${TRIPLE}) ..."

download_ok=0
for url in "${SRC_LIST[@]}"; do
  echo "      [尝试] $url"
  if [ -f "$TMP_ZIP" ]; then rm -f "$TMP_ZIP"; fi
  if curl -fL --retry 2 --connect-timeout 20 -o "$TMP_ZIP" "$url" 2>/dev/null; then
    # 校验文件头是否为 gzip（1f 8b），否则视为下载到错误页面，重试
    magic="$(head -c2 "$TMP_ZIP" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    if [ "$magic" = "1f8b" ]; then
      echo "      [成功] 校验通过（真实 gzip）"
      download_ok=1
      break
    else
      echo "      [失败] 校验未通过（非 gzip），尝试下一个源..."
      rm -f "$TMP_ZIP"
    fi
  else
    echo "      [失败] 下载出错，尝试下一个源..."
    rm -f "$TMP_ZIP"
  fi
done

if [ "$download_ok" -ne 1 ]; then
  goto_download_fail
fi
echo "      [OK] 已下载到 $TMP_ZIP"

# --- 2. 解压到 python_embeded/（install_only 压缩包顶层为 python/，剥离一层即可） ---
echo "[2/4] 解压到 $PYDIR ..."
mkdir -p "$PYDIR"
if ! tar -xzf "$TMP_ZIP" -C "$PYDIR" --strip-components=1 2>/dev/null; then
  echo "[错误] 解压失败。"
  rm -rf "$PYDIR"/{bin,lib,include,share} "$TMP_ZIP"
  goto_download_fail
fi
# 兼容极少数 python/install/ 布局：若未找到 python3，则定位并平移
if [ ! -x "$PYEXE" ]; then
  PYBIN="$(find "$PYDIR" -type f -path '*/bin/python3*' 2>/dev/null | head -1)"
  if [ -n "$PYBIN" ]; then
    PYPREFIX="$(dirname "$(dirname "$PYBIN")")"
    cp -a "$PYPREFIX/." "$PYDIR"/ 2>/dev/null
  fi
fi
if [ ! -x "$PYEXE" ]; then
  echo "[错误] 解压后未找到 $PYEXE"
  rm -f "$TMP_ZIP"
  goto_download_fail
fi
rm -f "$TMP_ZIP"

if [ ! -x "$PYEXE" ]; then
  echo "[错误] 解压后未找到 $PYEXE"
  goto_download_fail
fi
echo "      [OK] $( "$PYEXE" --version 2>&1 )"

# --- 3. 确认 pip 可用（standalone install_only 自带 pip） --------------------
echo "[3/4] 确认 pip 可用 ..."
if ! "$PYEXE" -m pip --version >/dev/null 2>&1; then
  echo "      [信息] 未检测到 pip，尝试 ensurepip ..."
  "$PYEXE" -m ensurepip --upgrade >/dev/null 2>&1 || {
    echo "[错误] 引导 pip 失败。"
    goto_download_fail
  }
fi
echo "      [OK] $("$PYEXE" -m pip --version 2>&1)"

# --- 4. 安装依赖（镜像优先 -> 官方回退） ------------------------------------
echo "[4/4] 安装依赖（国内镜像优先）..."
"$PYEXE" -m pip install -r "$REQ" --no-cache-dir -i "$PIP_MIRROR"
if [ $? -ne 0 ]; then
  echo "      [镜像不可达/部分失败] 回退官方 PyPI 源重试..."
  "$PYEXE" -m pip install -r "$REQ" --no-cache-dir -i "$PIP_OFFICIAL"
  if [ $? -ne 0 ]; then
    echo "[错误] 依赖安装失败，请检查网络后重试。"
    goto_download_fail
  fi
fi

echo
echo "[OK] 全部完成！Python ${PYVER} 与依赖已装入 python_embeded/"
echo "接下来请运行 launch.sh 选择要启动的后端，或 launch_webui.sh 启动 WebUI。"
exit 0
