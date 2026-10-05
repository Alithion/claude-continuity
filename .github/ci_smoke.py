"""ci_smoke.py -- the README's first run, end to end, in a throwaway directory. CI runs it on Linux, macOS and Windows.

    python .github/ci_smoke.py

Installs the kit into a project whose path has a space, writes one synthetic Claude Code transcript, runs digest and
weave exactly as the README says, runs the SessionStart hook exactly as .claude/settings.json records it, checks the
transcript lookup that does not rest on the folder name, then uninstalls. Nothing outside the temp dir is read or written:
HOME, USERPROFILE and CLAUDE_CONFIG_DIR all point inside it. Exit 0 only when every one of the EXPECTED checks ran and passed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

EXPECTED = 14
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "smoke-token-4c1f"
SID = "5f0c2a9e-7d41-4b8e-9a63-2e1d0c7b8a51"
SID_Q = "9b2e4d10-3c5a-4f6e-8d71-0a9c8b7e6d52"
WRITTEN = "notes/plan.md"
results = []


def check(name, fn):
    try:
        ok, why = fn()
    except Exception as e:
        ok, why = False, f"{type(e).__name__}: {e}"
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {why}"), flush=True)
    return ok


def run(args, cwd, env, stdin=None, shell=False):
    p = subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300, shell=shell)
    return p.returncode, p.stdout, p.stderr


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript(path, cwd, sid, token):
    """A finished session: one prompt, a Write, an answer. Padded past digest's 50 KB floor; mtime two hours back."""
    t0 = time.time() - 7200

    def ts(k):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 + 30 * k)) + ".000Z"

    base = {"cwd": cwd, "sessionId": sid, "version": "2.0.0", "isSidechain": False}
    rows = [
        dict(base, type="user", timestamp=ts(0), message={"role": "user", "content": f"Draft the plan ({token})."}),
        dict(base, type="assistant", timestamp=ts(1), message={"role": "assistant", "content": [
            {"type": "text", "text": "Writing the plan now."},
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": WRITTEN, "content": "plan"}}]}),
        dict(base, type="user", timestamp=ts(2), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok " + "x" * 60000}]}),
        dict(base, type="assistant", timestamp=ts(3), message={"role": "assistant", "content": [
            {"type": "text", "text": f"Wrote {WRITTEN}."}]}),
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.utime(path, (t0 + 120, t0 + 120))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="continuity_ci_"))
    home, config = os.path.join(tmp, "home"), os.path.join(tmp, "config")
    proj, proj_q = os.path.join(tmp, "my project"), os.path.join(tmp, "second project")
    for d in (home, config, proj, proj_q, os.path.join(proj, ".claude")):
        os.makedirs(d, exist_ok=True)
    env = dict(os.environ, HOME=home, USERPROFILE=home, CLAUDE_CONFIG_DIR=config, PYTHONIOENCODING="utf-8")
    py, kit = sys.executable, os.path.join(proj, ".continuity", "kit", "continuity")
    settings = os.path.join(proj, ".claude", "settings.json")
    original = b'{\n  "permissions": {\n    "allow": ["Bash(ls:*)"]\n  }\n}\n'
    with open(settings, "wb") as fh:
        fh.write(original)
    tx = os.path.join(config, "projects", slug(proj), SID + ".jsonl")
    st = {}

    def s1():
        rc, out, err = run([py, os.path.join(REPO, "install.py")], proj, env)
        st["install_out"] = out
        return rc == 0, f"exit {rc}: {(out + err).strip()[-300:]}"

    def ours(s):
        return [h for g in (s.get("hooks", {}) or {}).get("SessionStart", []) or [] for h in g.get("hooks", []) or []
                if h.get("_kit") == "claude-continuity"]

    def load():
        with open(settings, encoding="utf-8") as fh:
            return json.load(fh)

    def s2():
        s = load()
        hooks = ours(s)
        if len(hooks) != 1:
            return False, f"{len(hooks)} continuity hooks"
        h = hooks[0]
        st["hook"] = h
        args = h.get("args") or []
        return (os.path.isfile(h.get("command", "")) and len(args) == 1 and os.path.isfile(args[0])
                and s.get("permissions") == {"allow": ["Bash(ls:*)"]}), json.dumps(h)[:300]

    def s3():
        with open(settings + ".bak-continuity", "rb") as fh:
            return fh.read() == original, "backup differs from the original settings"

    def s4():
        need = ["CONTINUITY_LOG.md", "NOW.md", "CORRECTIONS.md", ".continuity/kit/continuity/brief.py",
                ".continuity/kit/hooks/session_start.py"]
        missing = [n for n in need if not os.path.isfile(os.path.join(proj, *n.split("/")))]
        return not missing, f"missing {missing}"

    def s5():
        rc1, o1, e1 = run([py, os.path.join(kit, "digest.py"), "--run"], proj, env)
        rc2, o2, e2 = run([py, os.path.join(kit, "weave.py"), "--run"], proj, env)
        return (rc1 == 0 and rc2 == 0 and "nothing to do" in o1.lower()), f"digest {rc1} weave {rc2}: {(o1 + e1 + o2 + e2).strip()[-300:]}"

    def s6():
        out = st.get("install_out", "").splitlines()
        i = next(k for k, l in enumerate(out) if l.startswith("Schedule these two"))
        lines = [l.strip() for l in out[i + 1:i + 3]]
        if len(lines) != 2 or not all(("digest.py" in lines[0], "weave.py" in lines[1])):
            return False, f"schedule lines not found: {lines}"
        rcs = []
        for l in lines:
            rc, o, e = run(l, proj, env, shell=True) if os.name == "nt" else run(["/bin/sh", "-c", l], proj, env)
            rcs.append(rc)
        return rcs == [0, 0], f"exit codes {rcs}: {lines}"

    def s7():
        transcript(tx, proj, SID, TOKEN)
        size = os.path.getsize(tx)
        return size >= 51200, f"{size} bytes"

    def digests(root):
        d = os.path.join(root, ".continuity", "digests")
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".md")] if os.path.isdir(d) else []

    def s8():
        rc, out, err = run([py, os.path.join(kit, "digest.py"), "--run"], proj, env)
        ds = digests(proj)
        if rc != 0 or len(ds) != 1:
            return False, f"exit {rc}, {len(ds)} digests: {(out + err).strip()[-300:]}"
        with open(ds[0], encoding="utf-8") as fh:
            text = fh.read()
        return TOKEN in text and WRITTEN in text, "digest lacks the prompt token or the written path"

    def s9():
        rc, out, err = run([py, os.path.join(kit, "weave.py"), "--run"], proj, env)
        with open(os.path.join(proj, "CONTINUITY_LOG.md"), encoding="utf-8") as fh:
            log = fh.read()
        return rc == 0 and SID[:8] in log, f"exit {rc}: {(out + err).strip()[-300:]}"

    def s10():
        h = st["hook"]
        new_sid = "0d7e6f5a-1b2c-4d3e-8f90-a1b2c3d4e5f6"
        payload = json.dumps({"session_id": new_sid, "transcript_path": os.path.join(os.path.dirname(tx), new_sid + ".jsonl"),
                              "cwd": proj, "hook_event_name": "SessionStart", "source": "startup"})
        rc, out, err = run([h["command"]] + list(h.get("args") or []), proj, env, stdin=payload)
        lines = [l for l in out.splitlines() if l.strip()]
        if rc != 0 or len(lines) != 1:
            return False, f"exit {rc}, {len(lines)} stdout lines: {(out + err).strip()[-300:]}"
        ctx = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
        return ctx.startswith("CONTINUITY BRIEF") and SID[:8] in ctx, ctx[:300]

    def s11():
        txq = os.path.join(config, "projects", "folder-not-named-by-slug", SID_Q + ".jsonl")
        transcript(txq, proj_q, SID_Q, TOKEN + "-q")
        rc, out, err = run([py, os.path.join(kit, "digest.py"), "--run"], proj_q, env)
        ds = digests(proj_q)
        return rc == 0 and len(ds) == 1, f"exit {rc}, {len(ds)} digests: {(out + err).strip()[-300:]}"

    def s12():
        rc, out, err = run([py, os.path.join(REPO, "install.py"), "--uninstall"], proj, env)
        s = load()
        return rc == 0 and not ours(s) and s.get("permissions") == {"allow": ["Bash(ls:*)"]}, f"exit {rc}: {(out + err).strip()[-300:]}"

    def s13():
        rc, out, err = run([py, os.path.join(REPO, "install.py"), "--uninstall"], proj, env)
        return rc == 0 and "nothing to do" in out.lower(), f"exit {rc}: {(out + err).strip()[-300:]}"

    def s14():
        rc, out, err = run([py, os.path.join(kit, "selftest.py"), "--drill"], proj, env)
        return rc == 0 and "ALL PASS" in out, f"exit {rc}: {(out + err).strip()[-300:]}"

    try:
        for n, fn in enumerate((s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14), 1):
            check(f"S{n}", fn)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(results)
    ok = len(results) == EXPECTED and passed == EXPECTED
    print(f"CI SMOKE: {passed}/{EXPECTED} PASS" if ok else
          f"CI SMOKE: {EXPECTED - passed if len(results) == EXPECTED else 'count'} FAILURE(S) "
          f"({passed} passed of {len(results)} run, {EXPECTED} expected)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
