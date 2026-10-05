"""brief.py -- the SessionStart hook of claude-continuity, and the verifier that proves it delivered.

WHAT
  At every session start Claude Code runs this file as a SessionStart hook. It reads the hook JSON from stdin,
  renders a small boot brief from files the project already keeps, and prints exactly one JSON line:

      {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<brief>"}}

  The brief starts with the line `CONTINUITY BRIEF (auto-injected at session start):` and never exceeds
  CAP (9,600 bytes UTF-8). Its sections, in this order, which is also the drop order from the tail:

      1. NOW              NOW.md, whole when inside its budget (20 non-blank lines / 2,400 bytes), else the
                          first whole lines that fit plus a visible trimmed notice
      2. CORRECTIONS      the last 8 bullet lines of CORRECTIONS.md, newest first, each cut to 220 chars
      3. UNWOVEN SESSIONS transcripts written since the last CONTINUITY_LOG.md weave (their work is not in the
                          log yet), excluding the session that is booting
      4. LAST 5 SESSIONS  from the newest digests in .continuity/digests/
      5. RECENT LOG       the last 3 hand-written lines of CONTINUITY_LOG.md (auto-weave lines skipped)
      6. BUDGETS          NOW.md size against its budget, digest count, log size

  When the whole brief would exceed CAP, sections are dropped from the tail and a notice names each one.
  Missing files are omitted, so a fresh project still boots with a brief.

WHY
  A hook that throws blanks the session's context, so hook mode is fail silent: any exception prints nothing
  and exits 0. That makes a broken hook invisible at the process level, so the loud path is separate:
  `--verify` reads the newest transcripts, where Claude Code records every SessionStart hook run and the
  hook's own stdout, and reports per boot whether the brief was actually DELIVERED, not just whether the
  hook ran.

USAGE
  python continuity/brief.py                       hook mode (stdin = hook JSON, may be empty)
  python continuity/brief.py --print               render the brief as plain text; errors are shown
  python continuity/brief.py --verify [--n 20]     OK / DEAD / MUTE per boot; exit 0 only if the newest
                                                   boot delivered a brief
  python continuity/brief.py --selftest            both-ways tests in a temp directory; exit 0 on pass

  --root <dir>         project root (the hook's `cwd` field wins when present, then --root, then the cwd)
  --transcripts <dir>  transcript dir (default: in hook mode the folder of the hook's transcript_path; else
                       $CLAUDE_CONFIG_DIR or ~/.claude, then projects/<slug>, slug = the absolute root with every
                       character outside [A-Za-z0-9] replaced by '-'; see paths.py)

Standard library only. Python 3.9+. Path rules for Windows, macOS and Linux; run end to end on Windows only.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import transcripts_dir  # noqa: E402  (one transcript-dir rule for every module)

CAP = 9600
MARKER = "CONTINUITY BRIEF"
HEAD = MARKER + " (auto-injected at session start):"
NOW_FILE, CORR_FILE, LOG_FILE = "NOW.md", "CORRECTIONS.md", "CONTINUITY_LOG.md"
DIGEST_DIR = os.path.join(".continuity", "digests")
NOW_MAX_LINES, NOW_MAX_BYTES = 20, 2400
CORR_ROWS, CORR_CHARS = 8, 220
UNWOVEN_ROWS, ASK_CHARS = 8, 90
LAST_N, LOG_TAIL, LOG_CHARS = 5, 3, 400
VERIFY_N = 20
PROBE_LINES = 400          # the first user prompt sits in the first handful of transcript rows

AUTO_WEAVE = re.compile(r"auto-weave, run|^- short sessions x|^- automated: |^- \*\*\d\d:\d\d|^#{1,6}\s|^_.*_\s*$")   # + headings and the italic file note
BULLET = re.compile(r"^[-*+] \S")
SPAN = re.compile(r"^- span \(local\):\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")
PROMPT_HEAD = re.compile(r"^## \[(\d{2}:\d{2})\] PROMPT\s*$")
DIGEST_SID = re.compile(r"^# Session digest:\s*([0-9A-Za-z-]{8,})")
TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
SKIP_PROMPT = ("<local-command-", "Caveat:", "[Request interrupted")


# ----------------------------------------------------------------------------- small helpers
def _read_text(path):
    """The one file reader every section uses. None when the file is absent."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _clip(s, n):
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _oneline(s):
    return " ".join(TAG.sub(" ", s).split())


def default_transcripts(root, transcript_path=None):
    return str(transcripts_dir(root, transcript_path))


def _local(ts):
    """ISO-8601 UTC -> 'YYYY-MM-DD HH:MM' local time; the raw prefix if it does not parse."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (ts or "?")[:16].replace("T", " ")


# ----------------------------------------------------------------------------- sections
def now_section(root):
    text = _read_text(os.path.join(root, NOW_FILE))
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    kept, size, nonblank = [], 0, 0
    for i, ln in enumerate(lines):
        b = len(ln.encode("utf-8")) + (1 if kept else 0)
        nb = 1 if ln.strip() else 0
        if size + b > NOW_MAX_BYTES or nonblank + nb > NOW_MAX_LINES:
            left = sum(1 for x in lines[i:] if x.strip())
            while kept and not kept[-1].strip():
                kept.pop()
            body = "\n".join(kept)
            return ("NOW (NOW.md):\n" + body + ("\n" if body else "")
                    + f"[{left} line(s) of NOW.md trimmed - over the budget; read the file]")
        kept.append(ln)
        size += b
        nonblank += nb
    return "NOW (NOW.md):\n" + "\n".join(kept)


def corrections_section(root):
    text = _read_text(os.path.join(root, CORR_FILE))
    if text is None:
        return None
    rows = [ln.rstrip() for ln in text.splitlines() if BULLET.match(ln)][-CORR_ROWS:]
    if not rows:
        return None
    rows.reverse()
    return "CORRECTIONS (CORRECTIONS.md, newest first):\n" + "\n".join(_clip(r, CORR_CHARS) for r in rows)


def first_prompt(path):
    """The first real user prompt of a transcript, read from its head only (transcripts reach many MB)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(PROBE_LINES):
                raw = fh.readline()
                if not raw:
                    break
                if '"user"' not in raw and '"queue-operation"' not in raw:
                    continue
                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                if o.get("isMeta") or o.get("toolUseResult") is not None:
                    continue
                txt = None
                if o.get("type") == "queue-operation" and isinstance(o.get("content"), str):
                    txt = o["content"]
                elif o.get("type") == "user" and isinstance(o.get("message"), dict):
                    c = o["message"].get("content")
                    if isinstance(c, str):
                        txt = c
                    elif isinstance(c, list):
                        txt = " ".join(b.get("text", "") for b in c
                                       if isinstance(b, dict) and b.get("type") == "text")
                if not txt or txt.lstrip().startswith(SKIP_PROMPT):
                    continue
                txt = _oneline(txt)
                if txt:
                    return txt
    except OSError:
        pass
    return ""


def unwoven_section(root, tdir, session_id, now):
    log_m = _mtime(os.path.join(root, LOG_FILE))
    if log_m is None or not tdir or not os.path.isdir(tdir):
        return None
    cands = []
    with os.scandir(tdir) as it:
        for e in it:
            if not e.name.endswith(".jsonl"):
                continue
            stem = e.name[:-6]
            if session_id and stem == session_id:
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            if mt > log_m:
                cands.append((mt, stem, e.path))
    cands.sort(reverse=True)
    since = max(0.0, (now - log_m) / 3600.0)
    head = (f"UNWOVEN SESSIONS - {len(cands)} session(s) have written work since the last {LOG_FILE} weave "
            f"({since:.1f}h ago):")
    if not cands:
        return head + "\n- none: every other session's work is already in the log"
    rows = ["Their work is not in the log yet; read the transcript before building on \"what we just did\"."]
    for mt, stem, path in cands[:UNWOVEN_ROWS]:
        ask = _clip(first_prompt(path), ASK_CHARS)
        rows.append(f"- {max(0.0, (now - mt) / 3600.0):.1f}h ago  {stem[:8]}  " + (f'"{ask}"' if ask else "(no prompt)"))
    if len(cands) > UNWOVEN_ROWS:
        rows.append(f"- +{len(cands) - UNWOVEN_ROWS} more in {tdir}")
    return head + "\n" + "\n".join(rows)


def _digest_key(name):
    """'digest_<date>_<sid8>.md' -> '<date>'; '' for names outside the pattern."""
    if not (name.startswith("digest_") and name.endswith(".md")):
        return ""
    return name[7:-3].rsplit("_", 1)[0]


def parse_digest(path, name):
    text = _read_text(path)
    if text is None:
        return None
    date = hhmm = ask = None
    sid = None
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if sid is None:
            m = DIGEST_SID.match(ln)
            if m:
                sid = m.group(1)[:8]
        if date is None:
            m = SPAN.match(ln)
            if m:
                date, hhmm = m.group(1), m.group(2)
        if ask is None and PROMPT_HEAD.match(ln):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("#"):
                    break
                if nxt.strip():
                    body.append(nxt)
                if len(" ".join(body)) > 400:
                    break
            ask = _oneline(" ".join(body))
        if date and ask is not None and sid:
            break
    if not date:
        return None
    if sid is None:
        stem = name[:-3] if name.endswith(".md") else name
        sid = stem.rsplit("_", 1)[-1][:8]
    return (date, hhmm or "00:00", sid, ask or "")


def digests_list(root):
    d = os.path.join(root, DIGEST_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    with os.scandir(d) as it:
        for e in it:
            if e.name.endswith(".md") and e.is_file():
                try:
                    out.append((e.name, e.path, e.stat().st_mtime))
                except OSError:
                    continue
    return out


def last_sessions_section(root, digests):
    if not digests:
        return None
    # Preselect cheaply (2,000 digests must not mean 2,000 reads): the newest filename dates, whole days at a
    # time so same-day ties are broken by the parsed span, plus the newest by mtime for odd filenames.
    by_name = sorted((x for x in digests if _digest_key(x[0])), key=lambda x: _digest_key(x[0]), reverse=True)
    pick, last_key = [], None
    for x in by_name:
        k = _digest_key(x[0])
        if len(pick) >= LAST_N and k != last_key:
            break
        pick.append(x)
        last_key = k
        if len(pick) >= 60:
            break
    seen = {x[1] for x in pick}
    pick += [x for x in sorted(digests, key=lambda x: x[2], reverse=True)[:30] if x[1] not in seen]
    rows = [r for r in (parse_digest(p, n) for n, p, _ in pick) if r]
    if not rows:
        return None
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = [f"- {d} {t}  {s}  {_clip(a, ASK_CHARS) if a else '(no prompt)'}" for d, t, s, a in rows[:LAST_N]]
    return f"LAST {LAST_N} SESSIONS:\n" + "\n".join(out)


def recent_log_section(root):
    text = _read_text(os.path.join(root, LOG_FILE))
    if text is None:
        return None
    tail = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not AUTO_WEAVE.search(ln)][-LOG_TAIL:]
    if not tail:
        return None
    return f"RECENT LOG ({LOG_FILE}):\n" + "\n".join(_clip(t, LOG_CHARS) for t in tail)


def budgets_section(root, digests):
    parts = []
    now_text = _read_text(os.path.join(root, NOW_FILE))
    if now_text is not None:
        body = now_text.replace("\r\n", "\n").strip("\n")
        n_lines = sum(1 for ln in body.split("\n") if ln.strip())
        n_bytes = len(body.encode("utf-8"))
        over = " OVER" if n_lines > NOW_MAX_LINES or n_bytes > NOW_MAX_BYTES else ""
        parts.append(f"NOW.md {n_lines}/{NOW_MAX_LINES} lines, {n_bytes:,}/{NOW_MAX_BYTES:,} B{over}")
    parts.append(f"digests {len(digests):,}")
    log_size = None
    try:
        log_size = os.path.getsize(os.path.join(root, LOG_FILE))
    except OSError:
        pass
    if log_size is not None:
        parts.append(f"{LOG_FILE} {log_size:,} B")
    return "BUDGETS: " + "; ".join(parts)


# ----------------------------------------------------------------------------- render + cap
def render(root, tdir, session_id=None, now=None, cap=CAP):
    now = time.time() if now is None else now
    digests = digests_list(root)
    sections = [
        ("NOW", now_section(root)),
        ("CORRECTIONS", corrections_section(root)),
        ("UNWOVEN SESSIONS", unwoven_section(root, tdir, session_id, now)),
        (f"LAST {LAST_N} SESSIONS", last_sessions_section(root, digests)),
        ("RECENT LOG", recent_log_section(root)),
        ("BUDGETS", budgets_section(root, digests)),
    ]
    sections = [(n, t) for n, t in sections if t]

    def compose(secs, dropped):
        body = HEAD + "\n\n" + "\n\n".join(t for _, t in secs)
        if dropped:
            body += (("\n\n" if secs else "") + f"[{len(dropped)} tail section(s) trimmed to stay under the "
                     f"{cap:,}-byte cap, in drop order: {'; '.join(dropped)}]")
        return body.rstrip() + "\n"

    dropped = []
    out = compose(sections, dropped)
    while len(out.encode("utf-8")) > cap and sections:
        dropped.append(sections.pop()[0])
        out = compose(sections, dropped)
    raw = out.encode("utf-8")
    if len(raw) > cap:                      # only reachable with an absurdly small cap: cut at a line boundary
        raw = raw[:cap]
        nl = raw.rfind(b"\n")
        raw = raw[: nl + 1] if nl > 0 else raw
        out = raw.decode("utf-8", errors="ignore")
    return out


def envelope(brief):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}})


# ----------------------------------------------------------------------------- verify
def _unwrap(stdout):
    """The brief as the session saw it: the hook's stdout is a JSON envelope, so unwrap additionalContext."""
    s = (stdout or "").lstrip()
    if s.startswith("{"):
        try:
            ctx = (json.loads(s).get("hookSpecificOutput") or {}).get("additionalContext")
            if isinstance(ctx, str):
                return ctx
        except Exception:
            pass
    return stdout or ""


def scan_boot(path):
    """The newest boot recorded in one transcript, or None when the file holds no timestamped row.

    Claude Code writes one row per SessionStart hook run:
      {"type": "attachment", "timestamp": ..., "attachment": {"type": "hook_success" | "hook_non_blocking_error"
       | "hook_error" | "hook_cancelled", "hookEvent": "SessionStart", "stdout": ..., "stderr": ..., "exitCode": ...}}
    and, when context was injected, a `hook_additional_context` row, which is not a hook run and is ignored.
    A resumed session boots again later in the same file, so the whole file is scanned and the records within
    a minute of the newest one form the boot that decides."""
    first_ts, recs = None, []
    try:
        with open(path, "rb") as fh:
            for i, raw in enumerate(fh):
                if first_ts is None and i < PROBE_LINES and b'"timestamp"' in raw:
                    try:
                        first_ts = json.loads(raw).get("timestamp")
                    except Exception:
                        pass
                if b"SessionStart" not in raw:
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                a = j.get("attachment") if j.get("type") == "attachment" else None
                if not isinstance(a, dict) or a.get("hookEvent") != "SessionStart":
                    continue
                kind = a.get("type") or ""
                if not kind.startswith("hook_") or kind == "hook_additional_context":
                    continue
                recs.append({"ts": j.get("timestamp") or "", "kind": kind, "stdout": a.get("stdout") or "",
                             "stderr": (a.get("stderr") or "").strip(), "exit": a.get("exitCode")})
    except OSError:
        return None
    sid = os.path.basename(path)[:8]
    if not recs:
        if not first_ts:
            return None
        return {"ts": first_ts, "sid": sid, "tag": "DEAD", "note": "no SessionStart hook record: the hook never ran"}
    newest = max(r["ts"] for r in recs)

    def _sec(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    top = _sec(newest)
    boot = [r for r in recs if r["ts"] == newest or (top is not None and _sec(r["ts"]) is not None
                                                      and top - _sec(r["ts"]) <= 60)]
    for r in boot:
        if r["kind"] == "hook_success":
            text = _unwrap(r["stdout"])
            if text.lstrip().startswith(MARKER):
                n = len(text.encode("utf-8"))
                return {"ts": newest, "sid": sid, "tag": "OK", "bytes": n,
                        "note": f"delivered {n:,}B, marker present"}
    errs = [r for r in boot if r["kind"] not in ("hook_success", "hook_cancelled")]
    if errs:
        e = errs[0]
        return {"ts": newest, "sid": sid, "tag": "DEAD",
                "note": f"{e['kind']} exit={e['exit']} :: {_clip(' '.join(e['stderr'].split()), 110) or '(no stderr)'}"}
    succ = [r for r in boot if r["kind"] == "hook_success"]
    if succ:
        s = succ[0]
        return {"ts": newest, "sid": sid, "tag": "MUTE",
                "note": f"ran (exit {s['exit']}) but delivered no brief (stdout {len(s['stdout'])}B, no marker)"}
    return {"ts": newest, "sid": sid, "tag": "SKIP", "note": "hook_cancelled (boot interrupted)"}


def verify(tdir, n=VERIFY_N):
    """(exit_code, lines). The newest n transcripts by mtime bound the set; the boot TIMESTAMP orders it, since
    an open session's transcript mtime advances on every write."""
    files = []
    if tdir and os.path.isdir(tdir):
        with os.scandir(tdir) as it:
            for e in it:
                if e.name.endswith(".jsonl") and e.is_file():
                    try:
                        files.append((e.stat().st_mtime, e.path))
                    except OSError:
                        continue
    files.sort(reverse=True)
    boots = [b for b in (scan_boot(p) for _, p in files[:n]) if b]
    if not boots:
        return 1, [f"CONTINUITY HOOK: no boots recorded yet (transcripts: {tdir})"]
    boots.sort(key=lambda b: b["ts"], reverse=True)
    lines = [f"  {b['tag']:<4} {_local(b['ts'])}  {b['sid']}  {b['note']}" for b in boots]
    decisive = next((b for b in boots if b["tag"] != "SKIP"), None)
    if decisive is None:
        return 1, lines + ["CONTINUITY HOOK: no boots recorded yet (every recent boot was cancelled)"]
    counts = {t: sum(1 for b in boots if b["tag"] == t) for t in ("OK", "DEAD", "MUTE")}
    tally = f"({counts['OK']} ok, {counts['DEAD']} dead, {counts['MUTE']} mute among the newest {len(boots)} boots)"
    when = _local(decisive["ts"])
    if decisive["tag"] == "OK":
        return 0, lines + [f"CONTINUITY HOOK: newest boot {when} DELIVERED {decisive['bytes']}B {tally}"]
    fix = ("the hook failed or never ran: check the SessionStart entry in .claude/settings.json and the stderr above"
           if decisive["tag"] == "DEAD" else
           "the hook ran but printed no brief: run `python3 .continuity/kit/continuity/brief.py --print` from the project root to see the error")
    return 1, lines + [f"CONTINUITY HOOK: newest boot {when} {decisive['tag']} {tally}", f"  FIX: {fix}"]


# ----------------------------------------------------------------------------- entry points
def _args(argv):
    ap = argparse.ArgumentParser(add_help=True, description="claude-continuity SessionStart brief")
    ap.add_argument("--root")
    ap.add_argument("--transcripts")
    ap.add_argument("--print", dest="print_", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--n", type=int, default=VERIFY_N)
    ap.add_argument("--selftest", action="store_true")
    return ap.parse_known_args(argv)[0]


def _resolve(a, hook):
    root = None
    if isinstance(hook.get("cwd"), str) and hook["cwd"].strip():
        root = hook["cwd"]
    root = os.path.abspath(root or a.root or os.getcwd())
    return root, (a.transcripts or default_transcripts(root, hook.get("transcript_path")))


def _hook_json(stdin_text):
    try:
        j = json.loads(stdin_text) if stdin_text and stdin_text.strip() else {}
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def hook_main(argv, stdin_text, out=None):
    """Hook mode. Exactly one JSON line on success; nothing at all on any failure; always 0."""
    out = out or sys.stdout
    try:
        a = _args(argv)
        hook = _hook_json(stdin_text)
        root, tdir = _resolve(a, hook)
        sid = hook.get("session_id") if isinstance(hook.get("session_id"), str) else None
        line = envelope(render(root, tdir, sid))
        out.write(line + "\n")
        out.flush()
    except BaseException:
        pass
    return 0


def _read_stdin():
    try:
        if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
            return ""
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in argv:
        return selftest()
    if "--verify" in argv or "--print" in argv:          # the loud paths: errors surface
        a = _args(argv)
        root = os.path.abspath(a.root or os.getcwd())
        tdir = a.transcripts or default_transcripts(root)
        if a.verify:
            rc, lines = verify(tdir, a.n)
            print("\n".join(lines))
            return rc
        hook = _hook_json(_read_stdin())
        root, tdir = _resolve(a, hook)
        sys.stdout.write(render(root, tdir, hook.get("session_id") if isinstance(hook.get("session_id"), str) else None))
        return 0
    return hook_main(argv, _read_stdin())


# ----------------------------------------------------------------------------- selftest (both ways)
def _w(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _transcript(prompt, extra=(), ts="2026-09-01T10:00:00.000Z"):
    rows = [{"type": "queue-operation", "operation": "enqueue", "timestamp": ts},
            *extra,
            {"type": "user", "isMeta": True, "timestamp": ts,
             "message": {"role": "user", "content": "<local-command-caveat>Caveat: ignore</local-command-caveat>"}},
            {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
            {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _hook_rec(kind, ts, stdout="", stderr="", code=0):
    return {"type": "attachment", "timestamp": ts,
            "attachment": {"type": kind, "hookName": "SessionStart:startup", "hookEvent": "SessionStart",
                           "stdout": stdout, "stderr": stderr, "exitCode": code, "durationMs": 40}}


def _digest(sid, date, hhmm, prompt):
    return (f"# Session digest: {sid}\n- file: {sid}.jsonl (0.1 MB)\n- span (local): {date} {hhmm} -> {hhmm}\n"
            f"- prompts: 1 | tool calls: 2\n\n## [{hhmm}] PROMPT\n{prompt}\n\n### [{hhmm}] outcome\nDone.\n")


def selftest():
    fails, checks = [], [0]

    def check(cond, what):
        checks[0] += 1
        if not cond:
            fails.append(what)

    tmp = tempfile.mkdtemp(prefix="continuity_brief_")
    now = time.time()
    try:
        root = os.path.join(tmp, "proj")
        tdir = os.path.join(tmp, "transcripts")
        os.makedirs(tdir)
        now_ok = "# Now\n\n- ship the parser\n- review the weave\n"
        _w(os.path.join(root, NOW_FILE), now_ok)
        _w(os.path.join(root, CORR_FILE), "# Corrections\n\n" + "".join(
            f"- correction {i}: {'x' * (300 if i == 10 else 10)}\n  detail line {i}\n" for i in range(1, 11)))
        log = os.path.join(root, LOG_FILE)
        _w(log, "# Continuity log\n\n- 2026-09-01 real entry one\n- 2026-09-02 real entry two\n"
                "## 2026-09-03 sessions (auto-weave, run 12:00)\n- **10:00-10:30 abcdef12** auto line\n"
                "- short sessions x3 (a, b, c)\n- automated: nightly job\n- 2026-09-03 real entry three\n"
                "- **11:00-11:20 12345678** auto line two\n", mtime=now - 3600)
        for i, (d, t) in enumerate([("2026-09-01", "09:00"), ("2026-09-03", "08:15"), ("2026-09-03", "17:40")]):
            sid = f"d{i}{i}{i}{i}aaa-0000-0000-0000-000000000000"
            _w(os.path.join(root, DIGEST_DIR, f"digest_{d}_{sid[:8]}.md"), _digest(sid, d, t, f"digest ask {i}"))
        new_id, old_id, cur_id = ("aaaa1111-0000-0000-0000-000000000001", "bbbb2222-0000-0000-0000-000000000002",
                                  "cccc3333-0000-0000-0000-000000000003")
        _w(os.path.join(tdir, new_id + ".jsonl"), _transcript("<pasted_content id=\"1\">Please fix the newer thing</pasted_content>"), now - 600)
        _w(os.path.join(tdir, old_id + ".jsonl"), _transcript("An older prompt that was already woven"), now - 7200)
        _w(os.path.join(tdir, cur_id + ".jsonl"), _transcript("The booting session itself"), now - 5)

        # 1 everything fits: one JSON line, six headers in order, under the cap
        buf = io.StringIO()
        rc = hook_main(["--transcripts", tdir], json.dumps({"cwd": root, "session_id": cur_id}), out=buf)
        lines = buf.getvalue().splitlines()
        check(rc == 0 and len(lines) == 1, "hook mode must print exactly one line and exit 0")
        brief = ""
        try:
            env = json.loads(lines[0])
            hso = env["hookSpecificOutput"]
            check(hso["hookEventName"] == "SessionStart", "envelope hookEventName")
            brief = hso["additionalContext"]
        except Exception as e:
            fails.append(f"JSON line does not parse: {e}")
        check(brief.startswith(HEAD + "\n"), "brief must start with the head line")
        heads = ["NOW (NOW.md):", "CORRECTIONS (CORRECTIONS.md, newest first):", "UNWOVEN SESSIONS - 1 session(s)",
                 "LAST 5 SESSIONS:", f"RECENT LOG ({LOG_FILE}):", "BUDGETS: "]
        pos = [brief.find("\n" + h) for h in heads]
        check(all(p > 0 for p in pos) and pos == sorted(pos), f"six headers in order, got positions {pos}")
        check("trimmed to stay under" not in brief, "nothing may be trimmed when everything fits")
        check(len(brief.encode("utf-8")) <= CAP, "brief over the cap")
        check("- ship the parser\n- review the weave" in brief, "NOW body whole")

        # 2 corrections: last 8 top-level bullets, newest first, cut to 220
        corr = brief.split("CORRECTIONS (CORRECTIONS.md, newest first):\n", 1)[-1].split("\n\n", 1)[0].splitlines()
        check(len(corr) == 8 and corr[0].startswith("- correction 10") and corr[-1].startswith("- correction 3"),
              f"corrections rows wrong: {corr[:1]} .. {corr[-1:]}")
        check(all(len(c) <= CORR_CHARS for c in corr) and corr[0].endswith("..."), "correction not cut to 220")
        check("detail line" not in brief, "nested lines are not corrections")

        # 3 unwoven: names the newer transcript only, never the older one or the booting session
        check("aaaa1111" in brief and "Please fix the newer thing" in brief, "unwoven row must name the newer transcript")
        check("pasted_content" not in brief, "prompt tags must be stripped")
        check("bbbb2222" not in brief and "cccc3333" not in brief, "unwoven must skip the older and the current session")

        # 4 last sessions: newest digest first, with sid8 and ask
        ls = brief.split("LAST 5 SESSIONS:\n", 1)[-1].split("\n\n", 1)[0].splitlines()
        check(len(ls) == 3 and ls[0].startswith("- 2026-09-03 17:40  d2222aaa  digest ask 2")
              and ls[-1].startswith("- 2026-09-01 09:00"), f"last sessions order wrong: {ls}")

        # 5 recent log skips every auto-weave shape
        rl = brief.split(f"RECENT LOG ({LOG_FILE}):\n", 1)[-1].split("\n\n", 1)[0].splitlines()
        check(rl == ["- 2026-09-01 real entry one", "- 2026-09-02 real entry two", "- 2026-09-03 real entry three"],
              f"recent log wrong: {rl}")
        check("auto line" not in brief and "short sessions" not in brief and "automated:" not in brief,
              "auto-weave lines leaked into RECENT LOG")
        check(brief.rstrip().splitlines()[-1].startswith("BUDGETS: NOW.md 3/20 lines") and "digests 3" in brief,
              "budgets line")

        # 6 oversized NOW: whole lines only, visible notice, BUDGETS says OVER
        big = "\n".join(f"- line {i:02d} " + "y" * 60 for i in range(1, 41))
        _w(os.path.join(root, NOW_FILE), big + "\n")
        b2 = render(root, tdir, cur_id, now)
        sec = b2.split("NOW (NOW.md):\n", 1)[-1].split("\n\n", 1)[0].splitlines()
        body, notice = sec[:-1], sec[-1]
        check(re.fullmatch(r"\[\d+ line\(s\) of NOW.md trimmed - over the budget; read the file\]", notice) is not None,
              f"NOW notice missing: {notice[:80]}")
        check(all(re.fullmatch(r"- line \d\d y{60}", ln) for ln in body), "NOW trimmed mid-line")
        check(len(body) <= NOW_MAX_LINES and len("\n".join(body).encode()) <= NOW_MAX_BYTES, "NOW over its budget")
        left = int(notice[1:].split(" ", 1)[0])
        check(left + len(body) == 40, f"NOW notice count {left} + kept {len(body)} != 40")
        check(" OVER" in b2.split("BUDGETS: ", 1)[-1], "BUDGETS must flag NOW over budget")
        _w(os.path.join(root, NOW_FILE), now_ok)

        # 7 a small cap drops from the TAIL and names each section, in drop order
        small = 700
        b3 = render(root, tdir, cur_id, now, cap=small)
        check(len(b3.encode("utf-8")) <= small, f"cap {small} exceeded: {len(b3.encode())}")
        m = re.search(r"\[(\d+) tail section\(s\) trimmed to stay under the 700-byte cap, in drop order: ([^\]]+)\]", b3)
        check(m is not None, "trim notice missing under a small cap")
        if m:
            names = m.group(2).split("; ")
            order = ["BUDGETS", "RECENT LOG", "LAST 5 SESSIONS", "UNWOVEN SESSIONS", "CORRECTIONS", "NOW"]
            check(names == order[:len(names)] and int(m.group(1)) == len(names), f"drop order wrong: {names}")
            check(b3.startswith(HEAD) and ("NOW (NOW.md):" in b3) == ("NOW" not in names), "kept sections inconsistent")
            check("BUDGETS: " not in b3, "BUDGETS must be the first section dropped")
        b4 = render(root, tdir, cur_id, now, cap=120)
        check(len(b4.encode("utf-8")) <= 120 and b4.startswith(HEAD), "absurd cap must still hold and keep the head")

        # 8 a fresh project (no files at all) still gets a brief; a missing log omits UNWOVEN
        fresh = os.path.join(tmp, "fresh")
        os.makedirs(fresh)
        b5 = render(fresh, os.path.join(tmp, "none"), None, now)
        check(b5.startswith(HEAD) and "BUDGETS: digests 0" in b5 and "UNWOVEN" not in b5, f"fresh brief wrong: {b5!r}")

        # 9 a broken reader inside rendering: exit 0 and NO output
        g = globals()
        real = g["_read_text"]

        def _broken(_p):
            raise RuntimeError("planted reader failure")
        g["_read_text"] = _broken
        try:
            buf = io.StringIO()
            rc = hook_main(["--transcripts", tdir], json.dumps({"cwd": root}), out=buf)
            check(rc == 0 and buf.getvalue() == "", "a render exception must print nothing and exit 0")
        finally:
            g["_read_text"] = real
        buf = io.StringIO()
        check(hook_main(["--transcripts", tdir, "--unknown-flag"], "{not json", out=buf) == 0
              and buf.getvalue().count("\n") == 1, "bad stdin / unknown flag must still yield the brief")

        # 10 the real process: stdin JSON in, one JSON line out, exit 0
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--transcripts", tdir],
                           input=json.dumps({"cwd": root, "session_id": cur_id}).encode(), capture_output=True, timeout=60)
        out_lines = p.stdout.decode("utf-8").splitlines()
        check(p.returncode == 0 and len(out_lines) == 1 and not p.stderr, f"subprocess hook rc={p.returncode} err={p.stderr[:120]!r}")
        real_line = out_lines[0] if out_lines else ""
        check(json.loads(real_line or "{}").get("hookSpecificOutput", {}).get("additionalContext", "").startswith(HEAD),
              "subprocess brief missing the head")

        # 11 --verify: OK with the renderer's own envelope, MUTE without the marker, DEAD on hook_error
        def vdir(name, recs, ts="2026-09-05T12:00:00.000Z"):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            _w(os.path.join(d, "eeee5555-0000-0000-0000-000000000005.jsonl"), _transcript("hello there verify", recs, ts))
            return d
        ctx_row = {"type": "attachment", "timestamp": "2026-09-05T12:00:00.100Z",
                   "attachment": {"type": "hook_additional_context", "hookEvent": "SessionStart", "content": ["x"]}}
        rc, v = verify(vdir("v_ok", [_hook_rec("hook_success", "2026-09-05T12:00:00.000Z", stdout=real_line), ctx_row]))
        check(rc == 0 and "DELIVERED" in v[-1] and v[0].lstrip().startswith("OK"), f"verify OK fixture: {rc} {v[-1]}")
        mute_out = envelope("some other hook's context, not ours")
        rc, v = verify(vdir("v_mute", [_hook_rec("hook_success", "2026-09-05T12:00:00.000Z", stdout=mute_out)]))
        check(rc == 1 and " MUTE " in v[-2] and v[0].lstrip().startswith("MUTE"), f"verify MUTE fixture: {rc} {v[-2:]}")
        rc, v = verify(vdir("v_dead", [_hook_rec("hook_error", "2026-09-05T12:00:00.000Z", stderr="spawn EINVAL", code=1)]))
        check(rc == 1 and " DEAD " in v[-2] and "EINVAL" in v[0], f"verify DEAD fixture: {rc} {v[-2:]}")
        rc, v = verify(vdir("v_never", []))
        check(rc == 1 and " DEAD " in v[-2] and "never ran" in v[0], f"verify never-ran fixture: {rc} {v[-2:]}")
        d_mix = vdir("v_mix", [_hook_rec("hook_success", "2026-09-04T12:00:00.000Z", stdout=real_line)],
                     ts="2026-09-04T12:00:00.000Z")
        _w(os.path.join(d_mix, "ffff6666-0000-0000-0000-000000000006.jsonl"),
           _transcript("newer dead boot", [_hook_rec("hook_non_blocking_error", "2026-09-06T12:00:00.000Z",
                                                     stderr="boom", code=1)], "2026-09-06T12:00:00.000Z"), now - 3600)
        rc, v = verify(d_mix)
        check(rc == 1 and " DEAD " in v[-2], "the newest BOOT timestamp must decide, not file mtime")
        empty = os.path.join(tmp, "v_empty")
        os.makedirs(empty)
        rc, v = verify(empty)
        check(rc == 1 and "no boots recorded yet" in v[-1], "empty transcript dir must say no boots recorded yet")

        # 12 scale: 2,000 digests + 300 unwoven transcripts render under 2 s and under the cap
        big_root = os.path.join(tmp, "big")
        _w(os.path.join(big_root, LOG_FILE), "- only line\n", now - 86400)
        _w(os.path.join(big_root, NOW_FILE), big + "\n")
        _w(os.path.join(big_root, CORR_FILE), "".join(f"- c{i} " + "z" * 250 + "\n" for i in range(50)))
        ddir = os.path.join(big_root, DIGEST_DIR)
        os.makedirs(ddir)
        for i in range(2000):
            day = f"2026-{1 + i // 180:02d}-{1 + (i // 6) % 28:02d}"
            sid = f"{i:08x}-0000-0000-0000-000000000000"
            with open(os.path.join(ddir, f"digest_{day}_{sid[:8]}.md"), "w", encoding="utf-8") as fh:
                fh.write(_digest(sid, day, f"{i % 24:02d}:{i % 60:02d}", "ask " + "w" * 200))
        btd = os.path.join(tmp, "big_t")
        os.makedirs(btd)
        tx = _transcript("a long first prompt " * 20)
        for i in range(300):
            _w(os.path.join(btd, f"{i:08x}-1111-1111-1111-111111111111.jsonl"), tx, now - i)
        # The guard is on READS, not wall-clock: antivirus scanning of freshly written files made a 2 s bound fail
        # at random on a loaded Windows machine. The preselect caps digest reads at 60 + 30; a generous clock stays
        # as the catastrophic backstop.
        reads, real_parse = [0], parse_digest

        def counting_parse(path, name):
            reads[0] += 1
            return real_parse(path, name)

        globals()["parse_digest"] = counting_parse
        try:
            t0 = time.perf_counter()
            b6 = render(big_root, btd, None, time.time())
            dt = time.perf_counter() - t0
        finally:
            globals()["parse_digest"] = real_parse
        check(reads[0] <= 90, f"render opened {reads[0]} of 2,000 digests (the preselect must cap reads at 90)")
        check(dt < 10.0, f"render took {dt:.2f}s on 2,000 digests (10 s backstop)")
        check(len(b6.encode("utf-8")) <= CAP, "big brief over the cap")
        check("UNWOVEN SESSIONS - 300 session(s)" in b6 and "+292 more" in b6, "big unwoven count / overflow row")
    except Exception as e:
        fails.append(f"selftest crashed: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        for f in fails:
            print("  FAIL " + f)
        print(f"SELFTEST FAIL {fails[0]}")
        return 1
    print(f"  render on 2,000 digests: {dt:.2f}s, {reads[0]} digest reads")
    print(f"SELFTEST PASS ({checks[0]} checks: sections in order, NOW trim, tail drop, unwoven, log filter, "
          f"verify OK/MUTE/DEAD, fail-silent, scale)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
