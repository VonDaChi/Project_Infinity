"""Entry point:  python -m webui

Adds the project root to sys.path first — the embedded interpreter runs in
isolated mode, so the current directory is not on the path automatically.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui.server import main

main()
