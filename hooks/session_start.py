"""SessionStart hook shim: runs continuity/brief.py in the project the hook fires in. Fails silent (a throwing hook blanks
the session's context), exits 0 always."""
import os
import subprocess
import sys

try:
    here = os.path.dirname(os.path.abspath(__file__))
    brief = os.path.join(os.path.dirname(here), "continuity", "brief.py")
    data = sys.stdin.read()
    p = subprocess.run([sys.executable, brief], input=data, capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace")
    if p.returncode == 0 and p.stdout.strip():
        sys.stdout.write(p.stdout.strip().splitlines()[-1] + "\n")
except Exception:
    pass
sys.exit(0)
