"""Make the repository root importable so `custom_components...` resolves in tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
