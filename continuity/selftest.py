"""selftest.py -- runs every module's --selftest and a fire drill that proves this runner can fail.

    python continuity/selftest.py            all modules
    python continuity/selftest.py --drill    also inject a failing module and assert it is reported

Exit 0 only when every module passed (and, with --drill, the injected failure was caught).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["atomic.py", "paths.py", "digest.py", "weave.py", "brief.py"]


def run(path, args):
    p = subprocess.run([sys.executable, path] + args, capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]


def main():
    drill = "--drill" in sys.argv
    fails = 0
    for m in MODULES:
        path = os.path.join(HERE, m)
        if not os.path.exists(path):
            print(f"MISSING  {m}"); fails += 1; continue
        rc, last = run(path, ["--selftest"])
        print(("PASS  " if rc == 0 else "FAIL  ") + f"{m:<12} {last[0][:110]}")
        fails += (rc != 0)
    if drill:
        import tempfile
        d = tempfile.mkdtemp(prefix="continuity_drill_")
        bad = os.path.join(d, "drill_fail.py")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("import sys\nprint('SELFTEST 1 FAILURE(S) (injected)')\nsys.exit(1)\n")
        rc, _ = run(bad, ["--selftest"])                 # a module that fails must be REPORTED as a failure
        caught = rc != 0
        try:
            os.remove(bad); os.rmdir(d)
        except OSError:
            pass
        print(("PASS  " if caught else "FAIL  ") + "fire drill (an injected failure is reported)")
        fails += (not caught)
    print("CONTINUITY SELF-TESTS: " + ("ALL PASS" if not fails else f"{fails} FAILURE(S)"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
