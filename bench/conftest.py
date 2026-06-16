"""Pytest bootstrap for the membench suite.

Puts ``bench/`` on ``sys.path`` so ``import membench`` resolves, regardless of
the directory pytest is invoked from. Deliberately does NOT touch ``engine/`` or
``plugins/`` — the membench package is isolated (fairness §7.5).
"""

import sys
from pathlib import Path

_BENCH_DIR = Path(__file__).resolve().parent
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))
