#!/usr/bin/env bash
# ===========================================================================
#  Project Infinity — WebUI 启动器 (Linux / Portable Python)
#  浏览器界面，局域网可访问。端口与 PIN 在 config/webui.yml 里调整。
#  与 launch_webui.bat 行为一致。
# ===========================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYEXE="$ROOT/python_embeded/bin/python3"

echo
echo "============================================================"
echo " Project Infinity - WebUI (Linux)"
echo "============================================================"
echo

# --- 守卫：禁止在 Windows 挂载的文件系统（WSL /mnt、NTFS/FAT 等）上运行 --------
# 说明：Windows 与 Linux 各自拥有独立的 python_embeded/（已 gitignore）。若在 WSL 里
# 通过 /mnt/c 访问 Windows 上的项目目录运行，会把 Linux 的 Python 写进 Windows 的
# python_embeded/，触发 symlink 错误并污染 Windows 环境。本脚本仅允许在真正的本机
# Linux 文件系统（ext4 等）上运行。
_PI_FSTYPE="$(stat -f -c '%T' "$ROOT" 2>/dev/null)"
case "$_PI_FSTYPE" in
  v9fs|9p|drvfs|ntfs|vfat|exfat|fuseblk|fuse.ntfs|fusectl)
    echo "[中止] 检测到在 Windows 挂载的文件系统 ($_PI_FSTYPE) 上运行。"
    echo "       为避免污染 Windows 的 python_embeded/，请在本机 Linux 文件系统"
    echo "       （如 ~/Project_Infinity，ext4）中 clone 本项目后，再运行本脚本。"
    exit 1;;
esac

# --- 确保嵌入式环境就绪（setup.sh 幂等） ------------------------------------
if [ ! -x "$PYEXE" ]; then
  echo "[信息] 未检测到嵌入式 Python，先运行 setup.sh 搭建环境..."
  if [ -f "$ROOT/setup.sh" ]; then
    bash "$ROOT/setup.sh"
  else
    echo "[错误] 找不到 setup.sh，无法自动搭建环境。"
    exit 1
  fi
  if [ ! -x "$PYEXE" ]; then
    echo "[错误] 环境搭建未完成，无法启动。请检查网络或按 setup.sh 提示手动安装。"
    exit 1
  fi
fi

cd "$ROOT"
# 内嵌 Python 为隔离模式，cwd 不在 sys.path，故用脚本式入口而非 -m webui
"$PYEXE" "$ROOT/webui/__main__.py"
rc=$?
if [ $rc -ne 0 ]; then
  echo
  echo "[错误] WebUI 启动失败（退出码 $rc）。请查看上方报错；或先运行 launch.sh 确认后端可用。"
  echo "按 Enter 退出..."
  read -r
  exit $rc
fi

echo
echo "服务已停止。"
echo "按 Enter 退出..."
read -r
