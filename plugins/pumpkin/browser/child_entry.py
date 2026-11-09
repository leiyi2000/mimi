import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser.code_child import BrowserCodeChild


sys.exit(BrowserCodeChild().run(int(sys.argv[1])))
