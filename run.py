# -*- coding: utf-8 -*-
"""Frozen / script entrypoint for Weigou Manager."""
from __future__ import annotations

import os
import sys

# Ensure project / _MEIPASS is on sys.path (PyInstaller onedir + script mode)
_BASE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
if _BASE and _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from paths import init_runtime_paths

init_runtime_paths()

from manager_app import main

if __name__ == "__main__":
    main()
