import sys
from pathlib import Path

# The tools are plain scripts, not a package; make them importable in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
