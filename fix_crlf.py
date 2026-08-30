#!/usr/bin/env python3
# fix_crlf.py
# One-click repair for Windows batch files that were checked out with LF
# line endings (Git autocrlf/input or editor defaults). Run this from the
# project root if launch.bat/setup.bat parse strangely.

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def normalize_crlf(name: str) -> bool:
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        print(f"[skip] {name} not found")
        return False
    with open(path, "rb") as f:
        data = f.read()
    normalized = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    with open(path, "wb") as f:
        f.write(normalized)
    stray = normalized.count(b"\n") - normalized.count(b"\r\n")
    print(f"[ok] {name}: CRLF normalized (stray LF = {stray})")
    return True


if __name__ == "__main__":
    for bat in ("launch.bat", "setup.bat", "launch_webui.bat"):
        normalize_crlf(bat)
    if sys.stdin.isatty():
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
