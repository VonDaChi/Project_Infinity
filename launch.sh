#!/usr/bin/env bash
# ===========================================================================
#  Project Infinity — 启动菜单（Portable Python, Linux）
#  先确保嵌入式环境就绪，再按数字选择后端启动。与 launch.bat 行为一致。
# ===========================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYEXE="$ROOT/python_embeded/bin/python3"

echo
echo "============================================================"
echo " Project Infinity 启动器 (Linux)"
echo "============================================================"
echo

# --- 确保环境就绪（setup.sh 幂等） ------------------------------------------
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

# --- 各后端启动 -------------------------------------------------------------
forge() {
  "$PYEXE" "$ROOT/main.py"
}

ollama() {
  "$PYEXE" "$ROOT/play.py"
}

kobold() {
  echo
  echo "KoboldCpp 地址：仅输入主机或 IP 即可（默认 localhost）"
  echo "将自动拼接为 http://HOST:5001/v1"
  read -r -p "KoboldCpp 主机 [默认 localhost]：" KOIP
  KOIP="${KOIP:-localhost}"
  # 若用户直接粘贴了完整 URL（含 ://），原样使用；否则补 :5001/v1
  case "$KOIP" in
    *://*) KOURL="$KOIP" ;;
    *)     KOURL="http://${KOIP}:5001/v1" ;;
  esac
  echo "使用端点：$KOURL"
  read -r -p "额外参数（可空）：" EXTRA
  # 用 eval 仅展开用户显式输入的额外参数（与 .bat 行为一致）
  "$PYEXE" "$ROOT/play_with_kobold.py" --base-url "$KOURL" $EXTRA
}

# 云后端统一 Key 检查：缺失则临时输入，仍缺失则返回菜单
require_key() {
  local var="$1" prompt="$2" script="$3"
  local val="${!var:-}"
  if [ -z "$val" ]; then
    echo "[提示] 未检测到 $var 环境变量。"
    echo "       请在系统/用户环境变量中设置后重跑，或在此临时输入："
    read -r -p "$var=：" K
    if [ -n "$K" ]; then
      export "$var=$K"
      val="$K"
    fi
  fi
  if [ -z "$val" ]; then
    echo "[错误] 缺少 $var，无法启动该后端。"
    return 1
  fi
  "$PYEXE" "$ROOT/$script"
}

deepseek() { require_key DEEPSEEK_API_KEY "DEEPSEEK_API_KEY" "play_with_deepseek.py"; }
openai()   { require_key OPENAI_API_KEY    "OPENAI_API_KEY"    "play_with_gpt.py"; }
gemini()   { require_key GEMINI_API_KEY    "GEMINI_API_KEY"    "play_with_gemini.py"; }
claude()   { require_key ANTHROPIC_API_KEY "ANTHROPIC_API_KEY" "play_with_claude.py"; }

lang() {
  echo
  echo "  1) 中文 (Chinese)"
  echo "  2) English"
  read -r -p "选择语言 / Select language [1-2]：" LC
  case "$LC" in
    1) "$PYEXE" "$ROOT/set_language.py" zh ;;
    2) "$PYEXE" "$ROOT/set_language.py" en ;;
  esac
}

# --- 菜单循环 ---------------------------------------------------------------
while true; do
  echo
  echo "============================================================"
  echo " Project Infinity — 选择启动项"
  echo "============================================================"
  echo "  1) 生成世界 (main.py / World Forge)"
  echo "  2) Ollama        (play.py, 本地模型)"
  echo "  3) KoboldCpp     (play_with_kobold.py, 本地/局域网)"
  echo "  4) DeepSeek      (play_with_deepseek.py, 需 DEEPSEEK_API_KEY)"
  echo "  5) OpenAI        (play_with_gpt.py, 需 OPENAI_API_KEY)"
  echo "  6) Gemini        (play_with_gemini.py, 需 GEMINI_API_KEY)"
  echo "  7) Claude        (play_with_claude.py, 需 ANTHROPIC_API_KEY)"
  echo "  L) 语言 / Language"
  echo "  Q) 退出"
  echo "============================================================"
  read -r -p "请输入序号 [1-7 / L / Q]：" CHOICE

  case "$CHOICE" in
    q|Q)  echo "已退出。"; exit 0 ;;
    l|L)  lang ;;
    1)    forge ;;
    2)    ollama ;;
    3)    kobold ;;
    4)    deepseek ;;
    5)    openai ;;
    6)    gemini ;;
    7)    claude ;;
    *)    echo "无效输入，请重试。" ;;
  esac
done
