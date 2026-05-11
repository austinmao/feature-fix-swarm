import sys
from pathlib import Path

LIB_ROOT = Path(__file__).resolve().parents[2]
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))
