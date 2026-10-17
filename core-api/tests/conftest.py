import os
import sys
from pathlib import Path

os.environ["APP_ENV"] = "test"  # before anything imports app.core.settings
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
