"""讓 tests/test_brokers/* 能 import backend.brokers 子模組。"""

import os
import sys

_BACKEND_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
