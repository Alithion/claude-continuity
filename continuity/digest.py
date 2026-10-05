"""digest.py -- turn Claude Code session transcripts into short, greppable per-session digests.

What it does
    Claude Code already saves every session of a project as a JSON-lines transcript under
    ~/.claude/projects/<slug>/<session id>.jsonl. Nobody reads those files: they are large and noisy.
    This module parses them (no model calls, standard library only) into one markdown digest per session:
    the user's prompts in order, the assistant's final text for each turn, the files the session wrote or
    edited, and a tally of tool calls. A cursor records what was digested, so a scheduled run only touches
    transcripts that changed, and marks which digests the weave step has not consumed yet.

Why
    The next session can only build on the previous ones if what happened in them is somewhere small and
    stable. The digest is that layer; weave.py turns it into one log line per session and brief.py puts
    the unwoven ones in front of the next session at boot.

Usage
    python continuity/digest.py --run [--root DIR] [--since YYYY-MM-DD] [--min-kb 50] [--session PREFIX ...]
    python continuity/digest.py --pending            list digests not yet woven (exit 2 if a digest is missing)
    python continuity/digest.py --mark-woven SID8 ...  flag sessions as woven (refused if the digest is missing)
    python continuity/digest.py --selftest           fixtures in a temp dir, both directions, exit 0/1
    --transcripts DIR overrides the transcript directory (tests use it).

Where things live
    transcripts  $CLAUDE_CONFIG_DIR or ~/.claude, then projects/<slug>/, where slug is the absolute project
                 root with every character outside [A-Za-z0-9] replaced by '-' (C:\\Users\\x\\proj -> C--Users-x-proj)
    digests      <root>/.continuity/digests/digest_<YYYYMMDD>_<HHMM>_<sid8>.md   (session's local start time)
    cursor       <root>/.continuity/cursor.json
                 {"sessions": {"<session id>": {"digest", "mtime", "size", "prompts", "woven", "digested_at"}},
                  "updated": "<iso utc>"}

Digest layout (weave.py and brief.py parse these header lines)
    # Session digest: <session id>
    - file: <transcript filename> (<size> MB)
    - span (local): YYYY-MM-DD HH:MM -> [YYYY-MM-DD ]HH:MM
    - prompts: <n> | tool calls: <m>

    ## [HH:MM] PROMPT          the user's text, capped at 6000 chars with the cut noted
    ### [HH:MM] outcome        the assistant's final text for that turn, capped at 1500 chars
    ## FILES WRITTEN/EDITED    "- <path>" lines from Write/Edit/NotebookEdit inputs, deduped, in order
    ## TOOL TALLY              "Name:count, ..." by count, then name
    The last two sections are omitted when empty: absence means none.

Rules it keeps
    Re-digest only when a transcript's mtime or size changed. A transcript that grew after being woven is
    set back to woven=false so the weave picks up the tail; an mtime-only touch keeps the woven flag.
    The cursor advances only after the digest is verified on disk, and is not rewritten at all when nothing
    changed. Nothing is ever deleted. Every write goes through atomic.py. Digest text is git-safe: no
    control characters except newline and tab, no trailing whitespace, and no line that starts like a merge
    conflict marker (such lines get a leading space). Automated prompts (text that opens with a tag such as
    <scheduled-task>) are kept and marked with an "[auto:<tag>]" first line.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from atomic import write_json, write_text  # noqa: E402
from paths import project_slug, transcripts_dir  # noqa: E402  (one rule for every module)

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path")
MAX_PROMPT_CHARS = 6000
MAX_OUTCOME_CHARS = 1500
DEFAULT_MIN_KB = 50

# User rows Claude Code injects that are not something the user typed.
SKIP_PREFIXES = ("<system-reminder", "<local-command", "caveat:", "[request interrupted")
TAG_RE = re.compile(r"^<([A-Za-z][\w-]*)")
# Wrappers a desktop client puts around a PERSON's own text (a paste, a dictation). They look like tags but are not
# automation, so the prompt keeps its human status and is never marked [auto:...].
HUMAN_WRAPPERS = {"pasted_content", "pasted_text", "user_content"}
INJECTED_RE = re.compile(r"<(system-reminder|local-command-[\w-]+)>.*?</\1>", re.S)
MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")
RESERVED_RE = re.compile(
    r"^(# Session digest:|#{2,3} \[[0-9?]{2}:[0-9?]{2}\] (PROMPT|outcome)\s*$|## FILES WRITTEN/EDITED\s*$|## TOOL TALLY\s*$)")
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- locations

def default_transcript_dir(root):
    """$CLAUDE_CONFIG_DIR (or ~/.claude)/projects/<slug>, absolute then real path, exact then case-insensitive.
    The rule lives in paths.py so digest, weave and brief always read the same folder."""
    return transcripts_dir(root)


def continuity_dir(root):
    return Path(root) / ".continuity"


def digests_dir(root):
    return continuity_dir(root) / "digests"


def cursor_path(root):
    return continuity_dir(root) / "cursor.json"


# ---------------------------------------------------------------- cursor

def load_cursor(root):
    """Returns the cursor dict. A corrupt cursor is preserved beside itself before a fresh one is started."""
    p = cursor_path(root)
    if not p.exists():
        return {"sessions": {}}
    raw = p.read_bytes()
    try:
        cur = json.loads(raw.decode("utf-8"))
        if not isinstance(cur, dict) or not isinstance(cur.get("sessions"), dict):
            raise ValueError("cursor has no sessions map")
        return cur
    except (ValueError, UnicodeDecodeError) as e:
        keep = p.with_name("cursor.json.corrupt-" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        write_text(str(keep), raw.decode("utf-8", "replace"))
        print(f"WARN cursor unreadable ({e}); kept a copy at {keep}, starting a fresh cursor")
        return {"sessions": {}}


def save_cursor(root, cur, touched=None):
    """Writes the cursor. With `touched`, re-reads the file first and replaces only those sessions, so a
    weave that marked sessions while this run was working is not overwritten."""
    if touched is not None:
        disk = load_cursor(root) if cursor_path(root).exists() else {"sessions": {}}
        for sid in touched:
            disk["sessions"][sid] = cur["sessions"][sid]
        cur = disk
    cur = {"sessions": cur.get("sessions", {}), "updated": now_utc()}
    write_json(str(cursor_path(root)), cur)
    return cur


# ---------------------------------------------------------------- parsing

def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def prompt_text(content):
    """The text a user typed, or None for rows that are not a prompt (tool results, injected notices)."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    else:
        return None
    # Injected notices can share a row with the real prompt (often before it): drop the notice, keep the prompt.
    parts = [INJECTED_RE.sub("", p).strip() for p in parts]
    t = "\n".join(p for p in parts if p).strip()
    if not t or t.lower().startswith(SKIP_PREFIXES):
        return None
    m = TAG_RE.match(t)
    if m and m.group(1).lower().replace("-", "_") not in HUMAN_WRAPPERS:
        t = f"[auto:{m.group(1)}]\n{t}"
    return t


def clip(text, cap):
    if len(text) <= cap:
        return text
    keep = text[:cap].rstrip()
    return f"{keep}\n[... {len(text) - len(keep)} more chars cut]"


def parse_transcript(path):
    """One pass over a transcript. Returns a dict with events, files, tools, first/last timestamps."""
    events = []                       # (kind, ts, text) with kind in {"prompt", "outcome"}
    files, tools = [], {}
    first = last = None
    run, run_ts = [], None            # text blocks since the last tool call in the current turn
    prev, prev_ts = [], None          # the last non-empty run before a tool call

    def end_turn():
        nonlocal run, run_ts, prev, prev_ts
        body, ts = (run, run_ts) if run else (prev, prev_ts)
        if body:
            events.append(("outcome", ts, "\n\n".join(body).strip()))
        run, run_ts, prev, prev_ts = [], None, [], None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain") or rec.get("isMeta"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            if ts:
                first = first or ts
                last = ts
            rtype = rec.get("type")
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            content = msg.get("content")
            if rtype == "system" and rec.get("subtype") == "turn_duration":
                end_turn()
            elif rtype == "user" and not rec.get("isCompactSummary"):
                t = prompt_text(content)
                if t:
                    end_turn()
                    events.append(("prompt", ts, t))
            elif rtype == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                        run.append(b["text"].strip())
                        run_ts = run_ts or ts
                    elif b.get("type") == "tool_use":
                        name = str(b.get("name") or "?")
                        tools[name] = tools.get(name, 0) + 1
                        if run:
                            prev, prev_ts = run, run_ts
                            run, run_ts = [], None
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        if name in WRITE_TOOLS:
                            for k in PATH_KEYS:
                                fp = inp.get(k)
                                if isinstance(fp, str) and fp and fp not in files:
                                    files.append(fp)
    end_turn()
    return {"events": events, "files": files, "tools": tools, "first": first, "last": last}


def sanitize(text):
    """Git-safe text: valid UTF-8, LF only, no control characters but tab, no trailing whitespace,
    no conflict-marker-shaped line, exactly one trailing newline."""
    text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_RE.sub("", text)
    out = []
    for ln in text.split("\n"):
        ln = ln.rstrip()
        if ln.startswith(MARKER_PREFIXES):
            ln = " " + ln
        out.append(ln)
    return "\n".join(out).strip("\n") + "\n"


def _body(text, cap):
    """A prompt/outcome body: capped, and with any line that imitates a digest heading indented one space."""
    lines = clip(text, cap).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(" " + ln if RESERVED_RE.match(ln) else ln for ln in lines)


def render_digest(path, parsed=None):
    """Returns (digest text, prompt count, local start datetime)."""
    path = Path(path)
    p = parsed or parse_transcript(path)
    st = path.stat()
    fallback = datetime.fromtimestamp(st.st_mtime).astimezone()
    start, end = p["first"] or fallback, p["last"] or p["first"] or fallback
    end_s = end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%Y-%m-%d %H:%M")
    prompts = sum(1 for k, _, _ in p["events"] if k == "prompt")
    hhmm = lambda ts: ts.strftime("%H:%M") if ts else "??:??"  # noqa: E731
    out = [f"# Session digest: {path.stem}",
           f"- file: {path.name} ({st.st_size / 1e6:.2f} MB)",
           f"- span (local): {start:%Y-%m-%d %H:%M} -> {end_s}",
           f"- prompts: {prompts} | tool calls: {sum(p['tools'].values())}",
           ""]
    for kind, ts, text in p["events"]:
        if kind == "prompt":
            out += [f"## [{hhmm(ts)}] PROMPT", _body(text, MAX_PROMPT_CHARS), ""]
        else:
            out += [f"### [{hhmm(ts)}] outcome", _body(text, MAX_OUTCOME_CHARS), ""]
    if p["files"]:
        out += ["## FILES WRITTEN/EDITED"] + [f"- {f}" for f in p["files"]] + [""]
    if p["tools"]:
        tally = ", ".join(f"{k}:{v}" for k, v in sorted(p["tools"].items(), key=lambda kv: (-kv[1], kv[0])))
        out += ["## TOOL TALLY", tally, ""]
    return sanitize("\n".join(out)), prompts, start


def digest_filename(session_id, start):
    return f"digest_{start:%Y%m%d_%H%M}_{session_id[:8]}.md"


def check_digest_text(text):
    """Problems that would break git sync or the header parsers; [] means clean."""
    issues = []
    if CONTROL_RE.search(text) or "\r" in text:
        issues.append("control character")
    lines = text.split("\n")
    if any(ln != ln.rstrip() for ln in lines):
        issues.append("trailing whitespace")
    if any(ln.startswith(MARKER_PREFIXES) for ln in lines):
        issues.append("conflict-marker line")
    if not text.endswith("\n") or text.endswith("\n\n"):
        issues.append("file must end with exactly one newline")
    heads = [r"^# Session digest: \S+$", r"^- file: .+ \([0-9.]+ MB\)$",
             r"^- span \(local\): \d{4}-\d{2}-\d{2} \d{2}:\d{2} -> (\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}$",
             r"^- prompts: \d+ \| tool calls: \d+$", r"^$"]
    for i, pat in enumerate(heads):
        if len(lines) <= i or not re.match(pat, lines[i]):
            issues.append(f"header line {i + 1} malformed")
            break
    return issues


# ---------------------------------------------------------------- commands

def cmd_run(root, tdir, since=None, min_kb=DEFAULT_MIN_KB, prefixes=None, explicit=True):
    root = Path(root)
    print(f"transcripts: {tdir}")
    if not tdir.is_dir():
        if not explicit:                # a fresh install before its first session: a normal state, not a failure
            print(f"no sessions yet: {tdir} does not exist. Open a Claude Code session in {root}; "
                  f"the next run digests it. Nothing to do.")
            return 0
        print(f"ERROR transcript dir not found: {tdir} (open a Claude Code session in {root} first, "
              f"or pass --transcripts)")
        return 1
    since_dt = datetime.fromisoformat(since).astimezone() if since else None
    cur = load_cursor(root)
    sessions = cur["sessions"]
    out_dir = digests_dir(root)
    touched = []
    scanned = small = unchanged = errors = 0
    for f in sorted(tdir.glob("*.jsonl"), key=lambda p: p.name):
        scanned += 1
        try:
            st = f.stat()
        except OSError:
            errors += 1
            continue
        sid = f.stem
        if prefixes and not any(sid.startswith(p) for p in prefixes):
            continue
        if since_dt and datetime.fromtimestamp(st.st_mtime).astimezone() < since_dt:
            continue
        if st.st_size < min_kb * 1024:
            small += 1
            continue
        rec = sessions.get(sid)
        if rec and rec.get("mtime") == st.st_mtime and rec.get("size") == st.st_size:
            unchanged += 1
            continue
        try:
            text, prompts, start = render_digest(f)
            name = digest_filename(sid, start)
            target = out_dir / name
            write_text(str(target), text)
            if target.stat().st_size == 0:
                raise OSError("digest wrote 0 bytes")
        except Exception as e:  # one bad transcript never stops the run
            print(f"ERROR {f.name}: {e}")
            errors += 1
            continue
        # A size change means new content: queue it for the weave again. An mtime-only touch keeps the flag.
        woven = bool(rec and rec.get("woven") and rec.get("size") == st.st_size)
        sessions[sid] = {"digest": name, "mtime": st.st_mtime, "size": st.st_size, "prompts": prompts,
                         "woven": woven, "digested_at": now_utc()}
        touched.append(sid)
        state = "new" if rec is None else ("re-digested" if woven else "re-digested, queued for weave")
        print(f"{name}  prompts={prompts}  {st.st_size / 1e6:.2f} MB  ({state})")
    if touched:
        cur = save_cursor(root, cur, touched)
    pending = sum(1 for r in cur["sessions"].values() if not r.get("woven"))
    print(f"scanned={scanned} digested={len(touched)} unchanged={unchanged} small-skip={small} "
          f"errors={errors} pending-weave={pending}")
    return 1 if errors else 0


def cmd_pending(root):
    cur = load_cursor(root)
    rows = sorted(((s, r) for s, r in cur["sessions"].items() if not r.get("woven")),
                  key=lambda kv: (kv[1].get("digested_at", ""), kv[0]))
    holes = 0
    for sid, r in rows:
        name = r.get("digest") or ""
        if not name or not (digests_dir(root) / name).is_file():
            holes += 1
            print(f"MISSING  {name or '(none)'}  session={sid[:8]}  cursor lists it but the file is absent; "
                  f"re-run: python3 .continuity/kit/continuity/digest.py --run --session {sid[:8]}")
            continue
        print(f"{name}  prompts={r.get('prompts')}  session={sid[:8]}")
    print(f"{len(rows)} digest(s) pending weave" + (f"; {holes} MISSING on disk" if holes else "")
          if rows else "weave queue empty")
    return 2 if holes else 0


def cmd_mark_woven(root, prefixes):
    prefixes = [p for p in prefixes if p]
    if not prefixes:
        print("ERROR --mark-woven needs at least one non-empty session id prefix")
        return 2
    cur = load_cursor(root)
    flipped, refused = [], 0
    for sid, r in sorted(cur["sessions"].items()):
        if not any(sid.startswith(p) for p in prefixes) or r.get("woven"):
            continue
        name = r.get("digest") or ""
        if not name or not (digests_dir(root) / name).is_file():
            refused += 1
            print(f"REFUSED {sid[:8]}: digest {name or '(none)'} is missing; a session is never marked "
                  f"woven unread")
            continue
        r["woven"] = True
        flipped.append(sid)
    if flipped:
        save_cursor(root, cur, flipped)
    print(f"marked {len(flipped)} session(s) woven" + (f"; {refused} refused" if refused else ""))
    return 2 if refused else 0


# ---------------------------------------------------------------- selftest

def selftest():
    import shutil
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="continuity_digest_"))
    fails, checks = [], 0

    def ok(cond, what):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(what)
        return cond

    env = dict(os.environ)
    fake_home = tmp / "home"
    env.update(HOME=str(fake_home), USERPROFILE=str(fake_home), CLAUDE_CONFIG_DIR=str(fake_home / ".claude"),
               PYTHONIOENCODING="utf-8")
    root, tdir = tmp / "project", tmp / "transcripts"
    root.mkdir()
    tdir.mkdir()

    def cli(*args):
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args, "--root", str(root),
                            "--transcripts", str(tdir)], capture_output=True, env=env, timeout=120)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def row(**kw):
        return json.dumps(kw, ensure_ascii=False) + "\n"

    sid = "0a1b2c3d-1111-4222-8333-444455556666"
    tiny = "ffffeeee-1111-4222-8333-444455556666"
    t = ["2026-01-05T14:00:00.000Z", "2026-01-05T14:01:00.000Z", "2026-01-05T14:02:00.000Z",
         "2026-01-05T14:03:00.000Z", "2026-01-05T15:10:00.000Z", "2026-01-05T15:11:00.000Z",
         "2026-01-06T09:00:00.000Z", "2026-01-06T09:01:00.000Z"]
    long_prompt = "second ask\x07 with a bell\n>>>>>>> theirs\n" + "x" * 7000
    lines = [
        row(type="user", timestamp=t[0], message={"role": "user", "content": "first ask: write the notes file"}),
        row(type="assistant", timestamp=t[1], message={"content": [
            {"type": "text", "text": "Looking at it."},
            {"type": "tool_use", "id": "a", "name": "Write",
             "input": {"file_path": "docs/notes.md", "content": "hi"}}]}),
        row(type="user", timestamp=t[1], message={"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok " + "p" * 60000}]}),
        row(type="assistant", timestamp=t[2], message={"content": [
            {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "docs/notes.md"}}]}),
        row(type="user", timestamp=t[2], message={"content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "hi"}]}),
        row(type="assistant", timestamp=t[3], message={"content": [
            {"type": "text", "text": "Wrote docs/notes.md.   "}]}),
        row(type="system", subtype="turn_duration", timestamp=t[3], durationMs=1000),
        row(type="user", timestamp=t[4], message={"content": [{"type": "text", "text": long_prompt}]}),
        row(type="assistant", timestamp=t[5], message={"content": [
            {"type": "tool_use", "id": "c", "name": "Edit",
             "input": {"file_path": "docs/notes.md", "old_string": "a", "new_string": "b"}},
            {"type": "text", "text": "Done with the edit."}]}),
        row(type="system", subtype="turn_duration", timestamp=t[5], durationMs=1000),
        row(type="user", timestamp=t[5], message={"content": [
            {"type": "text", "text": "<system-reminder>injected notice</system-reminder>"}]}),
        row(type="user", timestamp=t[5], message={"content": [
            {"type": "text", "text": "<system-reminder>\nproject notes\n</system-reminder>"},
            {"type": "text", "text": "real ask after a notice"}]}),
    ]
    main_tx = tdir / f"{sid}.jsonl"
    main_tx.write_text("".join(lines), encoding="utf-8")
    (tdir / f"{tiny}.jsonl").write_text(lines[0], encoding="utf-8")
    local = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()  # noqa: E731
    start = local(t[0])
    dname = digest_filename(sid, start)
    dpath = digests_dir(root) / dname
    cpath = cursor_path(root)

    try:
        # the slug rule, both ways
        ok(project_slug(r"C:\Users\x\proj") == "C--Users-x-proj", "slug of a Windows path")
        ok(project_slug("/home/a.b/my proj") == "-home-a-b-my-proj", "slug of a POSIX path")

        # a fresh project before its first session exits 0 and writes nothing; a mistyped --transcripts still fails
        fresh = tmp / "fresh_project"
        fresh.mkdir()
        me = str(Path(__file__).resolve())
        pr = subprocess.run([sys.executable, me, "--run", "--root", str(fresh)], capture_output=True, env=env, timeout=120)
        out = (pr.stdout + pr.stderr).decode("utf-8", "replace")
        ok(pr.returncode == 0 and "no sessions yet" in out and not (fresh / ".continuity").exists(),
           f"fresh project, no transcript dir yet: exit 0, nothing written (rc={pr.returncode})")
        pr = subprocess.run([sys.executable, me, "--run", "--root", str(fresh), "--transcripts", str(tmp / "no_such_dir")],
                            capture_output=True, env=env, timeout=120)
        out = (pr.stdout + pr.stderr).decode("utf-8", "replace")
        ok(pr.returncode == 1 and "ERROR transcript dir not found" in out,
           f"explicit missing --transcripts still exits 1 (rc={pr.returncode})")

        # the checker catches planted defects and passes clean text
        good = ("# Session digest: abc\n- file: abc.jsonl (0.10 MB)\n- span (local): 2026-01-01 10:00 -> 11:00\n"
                "- prompts: 1 | tool calls: 0\n\n## [10:00] PROMPT\nhi\n")
        ok(check_digest_text(good) == [], "checker passes a clean digest")
        ok("control character" in check_digest_text(good + "bell\x07\n"), "checker catches a control char")
        ok("conflict-marker line" in check_digest_text(good + "=======\n"), "checker catches a marker line")
        ok("trailing whitespace" in check_digest_text(good + "x \n"), "checker catches trailing whitespace")
        ok(not check_digest_text(sanitize(good + "bell\x07\n=======\nx \r\n")), "sanitize cleans all three")

        # first run: the digest exists, header exact, the tiny transcript skipped
        rc, out = cli("--run")
        ok(rc == 0, f"first --run exit {rc}: {out[-300:]}")
        ok(f"transcripts: {tdir}" in out, "--run prints the resolved transcript dir")
        ok(dpath.is_file(), f"digest {dname} written")
        text = dpath.read_text(encoding="utf-8") if dpath.is_file() else ""
        end = local(t[5])
        span_end = end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%Y-%m-%d %H:%M")
        head = text.split("\n")[:5]
        want = [f"# Session digest: {sid}",
                f"- file: {main_tx.name} ({main_tx.stat().st_size / 1e6:.2f} MB)",
                f"- span (local): {start:%Y-%m-%d %H:%M} -> {span_end}",
                "- prompts: 3 | tool calls: 3", ""]
        ok(head == want, f"header lines exact: {head!r}")
        ok(check_digest_text(text) == [], f"digest passes the checker: {check_digest_text(text)}")
        ok(text.count("] PROMPT\n") == 3, "exactly 3 prompts (tool_result-only and notice-only rows are not prompts)")
        ok("real ask after a notice" in text and "injected notice" not in text and "project notes" not in text,
           "a notice sharing a row with a prompt is dropped and the prompt kept")
        ok("## FILES WRITTEN/EDITED\n- docs/notes.md\n\n## TOOL TALLY" in text, "file list has the path once")
        ok("## TOOL TALLY\nEdit:1, Read:1, Write:1" in text, "tool tally")
        ok("\n >>>>>>> theirs\n" in text, "conflict-marker line prefixed with a space")
        ok("\x07" not in text and "second ask with a bell" in text, "control character stripped")
        ok("more chars cut]" in text, "long prompt cut and the cut noted")
        ok("Wrote docs/notes.md." in text and "Looking at it." not in text, "outcome is the turn's final text")
        ok("p" * 100 not in text, "tool_result content kept out of the digest")
        ok(not list(digests_dir(root).glob(f"*_{tiny[:8]}.md")), "tiny transcript under --min-kb skipped")
        cur = json.loads(cpath.read_text(encoding="utf-8")) if cpath.is_file() else {"sessions": {}}
        rec = cur["sessions"].get(sid, {})
        ok(set(rec) == {"digest", "mtime", "size", "prompts", "woven", "digested_at"}, f"cursor keys {set(rec)}")
        ok(rec.get("digest") == dname and rec.get("prompts") == 3 and rec.get("woven") is False,
           "cursor record for the new session")
        ok(tiny not in cur["sessions"], "tiny session not in the cursor")

        # second run: nothing written, cursor byte-identical
        before, dmtime = cpath.read_bytes(), dpath.stat().st_mtime_ns
        rc, out = cli("--run")
        ok(rc == 0 and "digested=0" in out, f"second --run digests nothing: {out[-200:]}")
        ok(cpath.read_bytes() == before, "cursor byte-identical after a no-op run")
        ok(dpath.stat().st_mtime_ns == dmtime, "digest not rewritten on a no-op run")

        # pending lists it; mark-woven flips it; pending is then empty
        rc, out = cli("--pending")
        ok(rc == 0 and dname in out and "1 digest(s) pending" in out, f"--pending lists the session: {out}")
        rc, out = cli("--mark-woven", sid[:8])
        ok(rc == 0 and "marked 1" in out, f"--mark-woven: {out}")
        ok(json.loads(cpath.read_text(encoding="utf-8"))["sessions"][sid]["woven"] is True, "woven flipped true")
        rc, out = cli("--pending")
        ok(rc == 0 and "weave queue empty" in out, f"--pending empty after mark-woven: {out}")

        # an mtime-only touch re-digests but keeps woven
        st = main_tx.stat()
        os.utime(main_tx, (st.st_atime, st.st_mtime + 5))
        rc, out = cli("--run")
        ok(rc == 0 and "digested=1" in out, "mtime change re-digests")
        ok(json.loads(cpath.read_text(encoding="utf-8"))["sessions"][sid]["woven"] is True,
           "mtime-only touch keeps woven=true")

        # growth re-digests and flips woven back to false
        with open(main_tx, "a", encoding="utf-8") as fh:
            fh.write(row(type="user", timestamp=t[6], message={"content": "third ask, next day"}))
            fh.write(row(type="assistant", timestamp=t[7], message={"content": [
                {"type": "text", "text": "Answered the third ask."}]}))
        rc, out = cli("--run")
        rec = json.loads(cpath.read_text(encoding="utf-8"))["sessions"][sid]
        ok(rc == 0 and rec["woven"] is False and rec["prompts"] == 4, f"growth re-digests, woven=false: {rec}")
        text = dpath.read_text(encoding="utf-8")
        end = local(t[7])
        ok(f"- span (local): {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}\n" in text
           or (end.date() == start.date() and f"-> {end:%H:%M}\n" in text), "span carries the second date")
        ok("Answered the third ask." in text, "grown tail is in the digest")
        rc, out = cli("--pending")
        ok(rc == 0 and dname in out, "--pending lists the grown session again")

        # a missing digest is a loud hole: pending exits 2, mark-woven refuses
        hidden = dpath.with_name(dname + ".hidden")
        dpath.rename(hidden)
        rc, out = cli("--pending")
        ok(rc == 2 and "MISSING" in out, f"--pending exits 2 on a missing digest: rc={rc}")
        rc, out = cli("--mark-woven", sid[:8])
        ok(rc == 2 and "REFUSED" in out, "--mark-woven refuses a missing digest")
        ok(json.loads(cpath.read_text(encoding="utf-8"))["sessions"][sid]["woven"] is False,
           "refused session stays unwoven")
        hidden.rename(dpath)

        ok(not (fake_home / ".claude").exists(), "selftest never created a ~/.claude")
    except Exception as e:  # a crash inside the selftest is a failure, not a pass
        fails.append(f"selftest crashed: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("SELFTEST FAIL  digest: " + f)
    print("SELFTEST " + (f"PASS (digest: {checks} checks; header, prompts, files, markers, control chars, "
                         f"min-kb skip, no-op run, growth re-weave, mark-woven, pending holes, fresh project)"
                         if not fails else f"{len(fails)} FAILURE(S)"))
    return 1 if fails else 0


# ---------------------------------------------------------------- main

def main(argv=None):
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="Digest Claude Code transcripts into per-session markdown.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="digest new or changed transcripts")
    mode.add_argument("--pending", action="store_true", help="list digests not yet woven")
    mode.add_argument("--mark-woven", nargs="+", metavar="SID8", help="mark these session id prefixes woven")
    mode.add_argument("--selftest", action="store_true", help="run fixtures in a temp dir")
    ap.add_argument("--root", default=os.getcwd(), help="project root (default: current directory)")
    ap.add_argument("--transcripts", help="transcript directory (default: $CLAUDE_CONFIG_DIR or ~/.claude, then projects/<slug>)")
    ap.add_argument("--since", help="only transcripts modified on or after this local date (YYYY-MM-DD)")
    ap.add_argument("--min-kb", type=float, default=DEFAULT_MIN_KB, help="skip transcripts smaller than this")
    ap.add_argument("--session", nargs="+", metavar="PREFIX", help="only these session id prefixes")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    root = Path(os.path.abspath(args.root))
    if args.pending:
        return cmd_pending(root)
    if args.mark_woven:
        return cmd_mark_woven(root, args.mark_woven)
    if args.since:
        try:
            datetime.fromisoformat(args.since)
        except ValueError:
            ap.error("--since must be YYYY-MM-DD")
    tdir = Path(args.transcripts) if args.transcripts else default_transcript_dir(root)
    return cmd_run(root, tdir, args.since, args.min_kb, args.session, explicit=bool(args.transcripts))


if __name__ == "__main__":
    raise SystemExit(main())
