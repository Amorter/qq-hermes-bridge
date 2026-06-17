"""Pytest config: make the repo root importable so ``import napcat`` works."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
