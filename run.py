# -*- coding: utf-8 -*-
"""Frozen / script entrypoint for Weigou Manager."""
from paths import init_runtime_paths

init_runtime_paths()

from manager_app import main

if __name__ == "__main__":
    main()
