#!/usr/bin/env python3
# set_language.py
# Standalone helper for launch.bat to switch UI language.
# Keeps launch.bat simple and avoids long inline Python that breaks if .bat
# gets converted to LF line endings.

import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import i18n


def main():
    if len(sys.argv) < 2:
        print("Usage: set_language.py <zh|en>", file=sys.stderr)
        sys.exit(1)

    lang = sys.argv[1].lower().strip()
    if lang in ("zh", "cn", "chinese", "中文"):
        i18n.set_lang("zh")
        print("已切换为中文，下次启动游戏生效。")
    elif lang in ("en", "english", "英文"):
        i18n.set_lang("en")
        print("Switched to English.")
    else:
        print(f"Unsupported language: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
