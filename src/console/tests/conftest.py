import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
for p in (_SRC / "common" / "python", _SRC / "console"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
