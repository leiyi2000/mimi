import sys
from pathlib import Path

from dotenv import load_dotenv

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

load_dotenv(PLUGIN_ROOT.parent.parent / ".env")
