r"""weave.py -- turn session digests into one dated, greppable line per session in CONTINUITY_LOG.md.

WHAT
  Reads the digests that digest.py leaves in <root>/.continuity/digests/ and appends, to <root>/CONTINUITY_LOG.md,
  one block per session date:

      ## YYYY-MM-DD — sessions (auto-weave, run YYYY-MM-DD HH:MM)
      - **HH:MM–HH:MM · sid8** — ask (+N prompts) → outcome · wrote N: a.py, b.md, c.txt +k
      - **HH:MM–HH:MM · sid8** (cont., prompts 3-4) — ask → outcome
      - short sessions xN (HH:MM sid8 "ask" → outcome; ...)
      - automated: <label> xN (sid8 HH:MM, ...)

  One header per SESSION date per run, oldest date first. Inside a date: full entries by start time, then the
  short-session lines, then the automated groups by label.

WHY
  A session that is not in the log by id is invisible to the next one. The weave is the zero-model floor that makes
  every session greppable by its 8-character id within one scheduled tick, without ever rewriting what is already
  there. brief.py reads the tail of this log at session start.

USAGE
  python continuity/weave.py --run [--root DIR] [--transcripts DIR] [--live-min 30]
  python continuity/weave.py --dry-run [--root DIR] ...   counts, bytes, max line bytes, 3 sample lines; writes nothing
  python continuity/weave.py --audit [--root DIR]         digests total / present in the log / absent
  python continuity/weave.py --selftest                   both-ways fixtures in a temp tree
  Exit 0 = ok, 2 = refused (nothing written). The selftest exits 0 on pass, 1 on fail.

RULES (each one is asserted by the selftest)
  * Presence. A session is PRESENT in the log iff its sid8 occurs bounded by non-hex characters on both sides,
    regex (?<![0-9a-f])sid8(?![0-9a-f]). A plain substring test would let an 8-hex id match inside a longer commit
    hash. The full 36-character session id and the digest filename both bound the id correctly.
  * Population. Every digest whose sid8 is absent from the log is woven, whatever the cursor's `woven` flag says.
    The flag is a cursor for other tools, not the source of truth.
  * Live deferral. A session whose transcript was modified in the last LIVE_MIN minutes is still running. It is
    DEFERRED: not woven and not flagged. Weaving it now would produce a stub followed by a continuation.
  * Continuation. A woven session whose digest later carries MORE non-automated prompts than the state recorded gets
    a tail-only entry marked (cont., prompts a-b) under its start-date header, never a second full entry. The live
    deferral applies to continuations too.
  * Holes. A cursor entry that names a digest file which is missing on disk refuses the whole run (exit 2, nothing
    written): never weave past a hole. If you prune old digests, remove their cursor entries at the same time.
    A missing or unreadable cursor also refuses (a blank cursor would flag nothing and report a clean zero).
  * Empty digests. A 0-byte or prompt-less digest is never woven and never flagged.
  * Duplicates. Two digest files sharing a sid8 weave once (the file the cursor names wins, else the first by name).
  * Line safety. Every rendered line is at most 400 bytes of UTF-8 (the outcome shrinks first, then the ask, then a
    file list over 120 bytes, or one that still does not fit, falls back to `wrote N`, then a final byte-safe cut); it is one physical line with no trailing
    whitespace, no control characters, and never starts with a merge-conflict marker. The log always ends with
    exactly one newline, and every header is preceded by a blank line.
  * Order of writes. The state file (prompts per woven sid8) is saved BEFORE the append; the append goes through
    atomic.append_text; then the cursor is re-read from disk and `woven: true` is set for EVERY cursor session whose
    sid8 is now present in the log, except deferred sessions and empty digests. Flipping everything present (not
    only what this run wrote) is deliberate: it heals a crash between the append and the flip on the next run.
  * A second run over unchanged input appends 0 bytes and leaves the state and cursor byte-identical.

INPUT CONTRACT (written by digest.py)
  digests/digest_<YYYYMMDD>_<HHMM>_<sid8>.md with header lines `# Session digest: <id>`, `- file: ...`,
  `- span (local): YYYY-MM-DD HH:MM -> [YYYY-MM-DD ]HH:MM`, `- prompts: N | tool calls: M`, then `## [HH:MM] PROMPT`
  and `### [HH:MM] outcome` blocks, `## FILES WRITTEN/EDITED` with `- path` lines, `## TOOL TALLY`.
  cursor.json: {"sessions": {"<36-char id>": {"digest": "<filename>", "mtime": float, "size": int, "prompts": int,
  "woven": bool, ...}}}. Transcripts: --transcripts DIR, else $CLAUDE_CONFIG_DIR (or ~/.claude)/projects/<slug>/<id>.jsonl
  where slug is the absolute root with every character outside [A-Za-z0-9] replaced by '-' (paths.py).

Standard library only, Python 3.9+. All writes go through continuity/atomic.py (append_text, write_json).
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from atomic import append_text, write_json  # noqa: E402  (the kit's only writers)
from paths import transcripts_dir  # noqa: E402  (one transcript-dir rule for every module)

LIVE_MIN = 30              # a transcript modified this recently belongs to a running session
ASK_CHARS = 120
OUT_CHARS = 200
LINE_BYTES = 400           # per rendered line, UTF-8, newline excluded
SUBSTANTIVE = 80           # the outcome shown is the last one at least this long, else the last one
WROTE_BYTES = 120          # a file list longer than this is a LONG list and renders as the bare `wrote N`
SHORT_MAX_CALLS = 2        # short session: <=1 prompt, no files written, <= this many tool calls
SHORT_PER_LINE = 3
IDS_PER_LINE = 20
LOG_NAME = "CONTINUITY_LOG.md"
LOG_H1 = "# Continuity log"

AUTOMATED_TAGS = ("<scheduled-task", "<task-notification", "<system-reminder", "<local-command")
MARKER_HEADS = ("<<<<<<<", ">>>>>>>", "=======", "|||||||")
SID_RE = re.compile(r"_([0-9a-f]{8})\.md$")
HEX8_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
PROMPT_RE = re.compile(r"^## \[([0-9?]{2}:[0-9?]{2})\] PROMPT\s*$")      # digest.py writes ??:?? for an unknown time
OUTCOME_RE = re.compile(r"^### \[([0-9?]{2}:[0-9?]{2})\] outcome\s*$")
SECTION_END_RE = re.compile(r"^## (FILES WRITTEN/EDITED|TOOL TALLY)\s*$")
SPAN_RE = re.compile(r"^- span \(local\): (\d{4}-\d\d-\d\d) (\d\d:\d\d) -> (?:(\d{4}-\d\d-\d\d) )?(\d\d:\d\d)", re.M)
CALLS_RE = re.compile(r"^- prompts: \d+ \| tool calls: (\d+)", re.M)
ID_RE = re.compile(r"^# Session digest: (\S+)", re.M)
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PASTED_RE = re.compile(r"</?pasted[_-]content[^>]*>")
TAG_RE = re.compile(r"</?(?:scheduled-task|task-notification|system-reminder|local-command[\w-]*|command-[\w-]+)[^>]*>")
NAME_ATTR_RE = re.compile(r'^\s*<(?:scheduled-task|task-notification|system-reminder|local-command[\w-]*)\b[^>]*?'
                          r'\bname="([^"]+)"')


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ----------------------------------------------------------------------------- paths
def paths_for(root):
    root = Path(root).resolve()
    cdir = root / ".continuity"
    return {"root": root, "log": root / LOG_NAME, "digests": cdir / "digests", "cursor": cdir / "cursor.json",
            "state": cdir / "weave_state.json"}


def default_transcripts(root):
    return transcripts_dir(root)


# ----------------------------------------------------------------------------- pure helpers
def sid8_of(name):
    m = SID_RE.search(name)
    return m.group(1) if m else None


def present_ids(log_text):
    """Every 8-hex run bounded by non-hex on both sides, found in one pass."""
    return set(HEX8_RE.findall(log_text))


def clean(t, n):
    """Digest text -> one sync-safe run of <= n characters: reminder blocks, pasted-content wrappers and automation
    tags removed, markdown emphasis and heading marks removed, control characters removed, whitespace collapsed,
    cut on a word boundary with an ellipsis."""
    t = REMINDER_RE.sub(" ", t or "")
    t = re.sub(r"^\s*\[auto:[\w-]+\]\s*", "", t)     # the digest's automation marker is metadata, not the ask
    t = PASTED_RE.sub(" ", t)
    t = TAG_RE.sub(" ", t)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"(?m)^\s*#+\s+", "", t)
    t = "".join(ch for ch in t if ord(ch) >= 32 and not (127 <= ord(ch) < 160) and ch not in "\u2028\u2029")
    t = " ".join(t.split())
    if len(t) > n:
        cut = t[: max(1, n - 1)]
        sp = cut.rsplit(" ", 1)[0] if " " in cut else cut
        if len(sp) < n // 2:
            sp = cut
        t = sp.rstrip(" ,;:-(") + "\u2026"
    return t


def is_automated(prompt):
    """A prompt is automated when, after removing reminder blocks the harness prepends, it is empty or starts with a
    recognizable automation tag. A prompt that is ONLY reminder blocks is automated; reminder blocks followed by a
    person's words are not."""
    raw = (prompt or "").lstrip()
    if not raw:
        return True
    rest = REMINDER_RE.sub(" ", raw).strip()
    if not rest:
        return True
    return rest.lower().startswith(AUTOMATED_TAGS)


def automated_label(prompt):
    """The tag's name="..." attribute, else the prompt's first five words, else the tag itself."""
    raw = REMINDER_RE.sub(" ", prompt or "").strip() or (prompt or "").strip()
    m = NAME_ATTR_RE.match(raw) or NAME_ATTR_RE.match(prompt or "")
    if m:
        return clean(m.group(1), 60) or "unnamed"
    words = clean(raw, 400).split()[:5]
    if words:
        return clean(" ".join(words), 60)
    tag = re.match(r"\s*<([\w-]+)", prompt or "")
    return tag.group(1) if tag else "unnamed"


def parse_digest(text):
    """Digest markdown -> dict. Sections break ONLY on the exact block headers, so a prompt that contains its own
    markdown headings is kept whole. Each outcome attaches to the prompt block it follows."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    prompts, files = [], []
    cur, buf, mode = None, [], None               # mode: 'p' prompt text, 'o' outcome text, 'f' files, None

    def flush():
        nonlocal buf
        body = "\n".join(buf).strip()
        if mode == "p" and cur is not None:
            cur["text"] = body
        elif mode == "o" and cur is not None:
            cur["outs"][-1] = (cur["outs"][-1][0], body)
        buf = []

    for ln in text.split("\n"):
        mp, mo, me = PROMPT_RE.match(ln), OUTCOME_RE.match(ln), SECTION_END_RE.match(ln)
        if mp or mo or me:
            flush()
            if mp:
                cur = {"hh": mp.group(1), "text": "", "outs": []}
                prompts.append(cur)
                mode = "p"
            elif mo:
                if cur is None:                   # an outcome before any prompt has nothing to attach to
                    mode = None
                    continue
                cur["outs"].append((mo.group(1), ""))
                mode = "o"
            else:
                mode = "f" if me.group(1).startswith("FILES") else None
            continue
        if mode == "f":
            s = ln.strip()
            if s.startswith("- ") and len(s) > 2:
                files.append(s[2:].strip())
        elif mode in ("p", "o"):
            buf.append(ln)
    flush()
    sp = SPAN_RE.search(text)
    span = (sp.group(1), sp.group(2), sp.group(3) or sp.group(1), sp.group(4)) if sp else None
    mc = CALLS_RE.search(text)
    mid = ID_RE.search(text)
    for p in prompts:
        p["auto"] = is_automated(p["text"])
    return {"prompts": prompts, "files": files, "span": span, "calls": int(mc.group(1)) if mc else 0,
            "id": mid.group(1) if mid else None}


def _line_safe(line):
    line = " ".join("".join(ch for ch in line if ord(ch) >= 32 and ord(ch) != 127).split())
    if line.startswith(MARKER_HEADS):
        line = "- " + line
    return line


def _byte_cut(line, limit=LINE_BYTES):
    """Final guarantee: cut the line to <= limit bytes on a character boundary."""
    if len(line.encode("utf-8")) <= limit:
        return line
    out = line
    while out and len((out + "\u2026").encode("utf-8")) > limit:
        out = out[:-1]
    return out.rstrip() + "\u2026"


def _nbytes(s):
    return len(s.encode("utf-8"))


def render_entry(sid8, span_hh, parsed, start_human=0, cont=False):
    """One full line for a session, or its tail from the start_human-th non-automated prompt when cont is True."""
    hh0, hh1 = span_hh
    prompts = parsed["prompts"]
    human_pos = [i for i, p in enumerate(prompts) if not p["auto"]]
    n_human = len(human_pos)
    tail_pos = human_pos[start_human:] if start_human < n_human else human_pos[-1:]
    first_pos = tail_pos[0] if tail_pos else 0
    ask_src = prompts[tail_pos[0]]["text"] if tail_pos else ""
    ask = clean(ask_src, ASK_CHARS) or "(no prompt text)"
    n_more = max(0, len(tail_pos) - 1)
    outs = [o for p in prompts[first_pos:] for _, o in p["outs"] if o.strip()]
    if not outs:
        outs = [o for p in prompts for _, o in p["outs"] if o.strip()]
    cleaned = [clean(o, 100000) for o in outs]
    pick = next((c for c in reversed(cleaned) if len(c) >= SUBSTANTIVE), cleaned[-1] if cleaned else "")
    outcome = clean(pick, OUT_CHARS) if pick else "(no outcome captured)"
    marker = f" (cont., prompts {start_human + 1}-{n_human})" if cont else ""
    more = f" (+{n_more} prompt{'s' if n_more != 1 else ''})" if n_more else ""
    files = parsed["files"]
    names = [clean(Path(f.replace("\\", "/")).name, 80) for f in files]
    wrote_full = ""
    if files:
        wrote_full = f" \u00b7 wrote {len(files)}: " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    wrote_bare = f" \u00b7 wrote {len(files)}" if files else ""
    if _nbytes(wrote_full) > WROTE_BYTES:
        wrote_full = wrote_bare

    def build(a, o, w):
        return _line_safe(f"- **{hh0}\u2013{hh1} \u00b7 {sid8}**{marker} \u2014 {a}{more} \u2192 {o}{w}")

    a_n, o_n, wrote = len(ask), len(outcome), wrote_full
    line = build(ask, outcome, wrote)
    for floor in (40, 10):
        while _nbytes(line) > LINE_BYTES and (o_n > floor or a_n > floor):
            if o_n > floor:
                o_n = max(floor, int(o_n * 0.8))
                outcome = clean(pick or outcome, o_n)
            else:
                a_n = max(floor, int(a_n * 0.8))
                ask = clean(ask_src, a_n) or ask
            line = build(ask, outcome, wrote)
        if _nbytes(line) > LINE_BYTES and wrote != wrote_bare:
            wrote = wrote_bare                    # long file names: keep the count, drop the list
            a_n, o_n = len(ask), len(outcome)
            line = build(ask, outcome, wrote)
        if _nbytes(line) <= LINE_BYTES:
            break
    return _byte_cut(line)


def _pack(items, line_for, max_items):
    """Greedy byte-aware wrap: items join a line while it stays <= LINE_BYTES and <= max_items."""
    lines, chunk = [], []
    for it in items:
        cand = chunk + [it]
        if chunk and (len(cand) > max_items or _nbytes(line_for(cand)) > LINE_BYTES):
            lines.append(line_for(chunk))
            chunk = [it]
        else:
            chunk = cand
    if chunk:
        lines.append(line_for(chunk))
    return lines


def render_short(items):
    """items: [(hhmm, sid8, ask, outcome)] -> '- short sessions xN (...)' lines."""
    def line_for(chunk, a_n=40, o_n=60):
        body = "; ".join(f"{hh} {sid} \"{clean(a, a_n) or '(no prompt text)'}\" \u2192 {clean(o, o_n) or '(no outcome)'}"
                         for hh, sid, a, o in chunk)
        return _line_safe(f"- short sessions x{len(chunk)} ({body})")

    out = []
    for line in _pack(items, line_for, SHORT_PER_LINE):
        m = re.match(r"- short sessions x1 \((\d\d:\d\d|\?\?:\?\?) ([0-9a-f]{8}) ", line)
        if m and _nbytes(line) > LINE_BYTES:
            src = next(((a, o) for hh, s, a, o in items if s == m.group(2)), None)
            if src:
                a_n, o_n = 40, 60
                line = line_for([(m.group(1), m.group(2), src[0], src[1])], a_n, o_n)
                while _nbytes(line) > LINE_BYTES and (a_n > 8 or o_n > 8):
                    a_n, o_n = max(8, int(a_n * 0.7)), max(8, int(o_n * 0.7))
                    line = line_for([(m.group(1), m.group(2), src[0], src[1])], a_n, o_n)
        out.append(_byte_cut(line))
    return out


def render_automated(label, items):
    """items: [(hhmm, sid8)] -> '- automated: <label> xN (sid8 HH:MM, ...)' lines."""
    def line_for(chunk):
        ids = ", ".join(f"{sid} {hh}" for hh, sid in chunk)
        return _line_safe(f"- automated: {label} x{len(chunk)} ({ids})")
    return [_byte_cut(ln) for ln in _pack(items, line_for, IDS_PER_LINE)]


def header(date, stamp):
    return f"## {date} \u2014 sessions (auto-weave, run {stamp})"


# ----------------------------------------------------------------------------- io helpers
def _read(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def load_cursor(p):
    """(cursor_dict, error_or_None). Missing, unreadable, or wrong-shaped cursors are errors."""
    try:
        cur = json.loads(_read(p["cursor"]))
    except FileNotFoundError:
        return None, f"cursor missing: {p['cursor']}"
    except (OSError, ValueError) as e:
        return None, f"cursor unreadable: {p['cursor']} ({type(e).__name__})"
    if not isinstance(cur, dict) or not isinstance(cur.get("sessions"), dict):
        return None, f"cursor has no 'sessions' object: {p['cursor']}"
    return cur, None


def load_state(p):
    try:
        st = json.loads(_read(p["state"]))
        if isinstance(st, dict) and isinstance(st.get("sessions"), dict):
            return st, None
        return {"sessions": {}}, "state file malformed; starting from an empty state"
    except FileNotFoundError:
        return {"sessions": {}}, None
    except (OSError, ValueError):
        return {"sessions": {}}, "state file unreadable; starting from an empty state"


def _digest_files(p):
    try:
        return sorted(x for x in p["digests"].glob("digest_*.md") if x.is_file())
    except OSError:
        return []


# ----------------------------------------------------------------------------- the weave
def run(root=".", transcripts=None, live_min=LIVE_MIN, dry_run=False, now=None, quiet=False):
    """Returns (exit_code, summary). Exit 2 = refused, nothing written."""
    say = (lambda *a, **k: None) if quiet else print
    p = paths_for(root)
    now = now or datetime.now()
    now_ts = now.timestamp()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    tdir = Path(transcripts) if transcripts else default_transcripts(p["root"])

    cur, err = load_cursor(p)
    if err and err.startswith("cursor missing") and not _digest_files(p):
        # a fresh install before its first digest: nothing can be lost or duplicated, so this is not a refusal
        say(f"nothing to weave yet: no cursor and no digests (digest.py creates both after the first session). "
            f"{LOG_NAME} untouched.")
        return 0, {"nothing": "fresh"}
    if err:
        say(f"WEAVE REFUSED: {err}. Run digest.py first or repair the cursor. {LOG_NAME} untouched.")
        return 2, {"refused": "cursor"}
    sessions = cur["sessions"]
    key_by_sid8 = {}
    for k in sessions:
        key_by_sid8.setdefault(str(k)[:8], k)

    holes = [(k, (r or {}).get("digest")) for k, r in sessions.items()
             if isinstance(r, dict) and r.get("digest") and not (p["digests"] / str(r["digest"])).is_file()]
    if holes:
        for k, name in holes:
            say(f"MISSING-ON-DISK  {name}  session={str(k)[:8]} (named by the cursor, absent from {p['digests']})")
        say(f"WEAVE REFUSED: {len(holes)} digest(s) missing on disk; regenerate them (or drop their cursor entries). "
            f"{LOG_NAME} untouched.")
        return 2, {"refused": "holes", "holes": len(holes)}

    log_exists = p["log"].exists()
    try:
        log_text = _read(p["log"]) if log_exists else ""
    except OSError as e:
        say(f"WEAVE REFUSED: cannot read {p['log']} ({type(e).__name__}).")
        return 2, {"refused": "log"}
    present = present_ids(log_text)

    state, warn = load_state(p)
    if warn:
        say("WARNING: " + warn)
    st = state["sessions"]
    state_before = json.dumps(state, sort_keys=True)

    def age_min(sid8, full_id):
        key = key_by_sid8.get(sid8) or full_id
        mt = None
        if key:
            try:
                mt = (tdir / f"{key}.jsonl").stat().st_mtime
            except OSError:
                mt = (sessions.get(key) or {}).get("mtime") if isinstance(sessions.get(key), dict) else None
        try:
            return None if mt is None else (now_ts - float(mt)) / 60.0
        except (TypeError, ValueError):
            return None

    # group digest files by sid8; the cursor-named one wins, else the first by name
    by_sid = defaultdict(list)
    for path in _digest_files(p):
        s = sid8_of(path.name)
        if s:
            by_sid[s].append(path)
    chosen = []
    for s, lst in by_sid.items():
        named = (sessions.get(key_by_sid8.get(s)) or {}).get("digest") if key_by_sid8.get(s) else None
        pick = next((x for x in lst if x.name == named), lst[0])
        chosen.append((pick.name, s, pick))
    chosen.sort()

    groups = defaultdict(lambda: {"full": [], "short": [], "auto": defaultdict(list)})
    counts = defaultdict(int)
    deferred, no_flip = [], set()

    for name, sid8, path in chosen:
        try:
            text = _read(path)
        except OSError:
            counts["unreadable"] += 1
            no_flip.add(sid8)
            continue
        parsed = parse_digest(text)
        prompts = parsed["prompts"]
        if not text.strip() or not prompts:
            counts["empty"] += 1
            no_flip.add(sid8)
            continue
        human = [x for x in prompts if not x["auto"]]
        n_human = len(human)
        sp = parsed["span"]
        fm = re.match(r"digest_(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)_", name)
        if sp:
            date, hh0, hh1 = sp[0], sp[1], sp[3]
        elif fm:
            date, hh0, hh1 = f"{fm.group(1)}-{fm.group(2)}-{fm.group(3)}", f"{fm.group(4)}:{fm.group(5)}", "??:??"
        else:
            date, hh0, hh1 = "unknown-date", "??:??", "??:??"
        prev = st.get(sid8)

        if sid8 in present:
            prev_n = int((prev or {}).get("prompts", 0) or 0)
            if prev is not None and prev_n < n_human:
                a = age_min(sid8, parsed["id"])
                if a is not None and a < live_min:
                    deferred.append(sid8)
                    continue
                groups[date]["full"].append((hh0, render_entry(sid8, (hh0, hh1), parsed, start_human=prev_n, cont=True)))
                counts["cont"] += 1
                st[sid8] = {"prompts": n_human, "digest": name, "woven_at": prev.get("woven_at") or stamp,
                            "cont_at": stamp}
            elif prev is None or prev.get("prompts") != n_human or prev.get("digest") != name:
                st[sid8] = {"prompts": max(n_human, prev_n), "digest": name,
                            "woven_at": (prev or {}).get("woven_at") or stamp}
                counts["present"] += 1
            else:
                counts["present"] += 1
            continue

        a = age_min(sid8, parsed["id"])
        if a is not None and a < live_min:
            deferred.append(sid8)
            continue
        if not human:
            label = automated_label(prompts[0]["text"])
            groups[date]["auto"][label].append((hh0, sid8))
            counts["automated"] += 1
        elif n_human <= 1 and not parsed["files"] and parsed["calls"] <= SHORT_MAX_CALLS:
            outs = [o for x in prompts for _, o in x["outs"] if o.strip()]
            groups[date]["short"].append((hh0, sid8, human[0]["text"], outs[-1] if outs else ""))
            counts["short"] += 1
        else:
            groups[date]["full"].append((hh0, render_entry(sid8, (hh0, hh1), parsed)))
            counts["full"] += 1
        st[sid8] = {"prompts": n_human, "digest": name, "woven_at": stamp}

    # ---- compose: oldest session date first; full entries by time, then shorts, then automated groups by label
    lines, samples = [], []
    for date in sorted(groups):
        g = groups[date]
        block = [e for _, e in sorted(g["full"], key=lambda x: x[0])]
        if g["short"]:
            block += render_short(sorted(g["short"], key=lambda x: x[0]))
        for label in sorted(g["auto"]):
            block += render_automated(label, sorted(g["auto"][label], key=lambda x: x[0]))
        if not block:
            continue
        lines += ["", header(date, stamp)] + block
        samples += block
    body = "\n".join(lines) + "\n" if lines else ""
    max_line = max([_nbytes(x) for x in lines] + [0])
    n_woven = counts["full"] + counts["short"] + counts["automated"] + counts["cont"]
    summary = {"bytes": _nbytes(body), "headers": sum(1 for x in lines if x.startswith("## ")), "full": counts["full"],
               "short": counts["short"], "automated": counts["automated"], "cont": counts["cont"],
               "present": counts["present"], "deferred": sorted(deferred), "empty": counts["empty"],
               "unreadable": counts["unreadable"], "max_line": max_line, "sessions": n_woven}

    for x in lines:                               # belt and braces: the renderers already guarantee this
        if x != x.rstrip() or _nbytes(x) > LINE_BYTES or x.startswith(MARKER_HEADS) or any(ord(c) < 32 for c in x):
            say(f"WEAVE REFUSED: internal render check failed on line: {x[:120]!r}")
            return 2, dict(summary, refused="render")

    if dry_run:
        say(f"DRY-RUN weave: would append {summary['bytes']} B under {summary['headers']} date header(s): "
            f"{counts['full']} full + {counts['short']} short + {counts['automated']} automated + {counts['cont']} "
            f"continuation(s); max line {max_line} B; {counts['present']} already present; "
            f"{len(deferred)} deferred (live) {sorted(deferred)}; {counts['empty']} empty")
        for s in samples[:3]:
            say("  " + s)
        return 0, summary

    # ---- 1. state before the append
    if json.dumps(state, sort_keys=True) != state_before:
        write_json(p["state"], {"sessions": dict(sorted(st.items()))})
    # ---- 2. append (create the log with its H1 when absent)
    text_out = body
    if not log_exists:
        text_out = LOG_H1 + "\n" + body
    appended = 0
    if text_out:
        appended = append_text(p["log"], text_out)
    summary["appended"] = appended
    # ---- 3. flip the cursor flag for every present session except deferred and empty ones
    try:
        present_now = present_ids(_read(p["log"])) if p["log"].exists() else set()
    except OSError:
        present_now = present | {s for s in st}
    fresh, err = load_cursor(p)                   # re-read: digest.py may have written it meanwhile
    if err:
        say(f"WEAVE PARTIAL: appended {appended} B but {err}; the flag flip heals on the next run.")
        return 2, dict(summary, refused="cursor-after-append")
    skip = set(deferred) | no_flip
    flip = [k for k, r in fresh["sessions"].items()
            if isinstance(r, dict) and str(k)[:8] in present_now and str(k)[:8] not in skip and not r.get("woven")]
    for k in flip:
        fresh["sessions"][k]["woven"] = True
    if flip:
        write_json(p["cursor"], fresh)
    pending = sum(1 for r in fresh["sessions"].values() if isinstance(r, dict) and not r.get("woven"))
    summary.update({"flipped": len(flip), "pending": pending})
    say(f"weave: appended {appended} B ({counts['full']} full, {counts['short']} short, {counts['automated']} automated, "
        f"{counts['cont']} cont.) under {summary['headers']} date header(s); max line {max_line} B; "
        f"{counts['present']} already present; deferred {len(deferred)} {sorted(deferred)}; "
        f"flagged woven {len(flip)}; cursor pending {pending}")
    return 0, summary


def audit(root="."):
    p = paths_for(root)
    try:
        present = present_ids(_read(p["log"])) if p["log"].exists() else set()
    except OSError:
        present = set()
    ids = sorted({s for s in (sid8_of(x.name) for x in _digest_files(p)) if s})
    absent = [s for s in ids if s not in present]
    print(f"audit: {len(ids)} digest session(s), {len(ids) - len(absent)} present in {LOG_NAME}, {len(absent)} absent"
          + (f" (first: {', '.join(absent[:5])})" if absent else ""))
    return 0


# ----------------------------------------------------------------------------- selftest (temp tree only)
def selftest():
    import shutil
    import tempfile
    import time
    _utf8_stdout()
    fails = []

    def chk(what, cond, detail=""):
        if not cond:
            fails.append(what + (f" [{str(detail)[:240]}]" if detail else ""))

    tmp = Path(tempfile.mkdtemp(prefix="weave_selftest_"))
    try:
        root = tmp / "project"
        tdir = tmp / "transcripts"             # always passed explicitly: the real ~/.claude is never computed
        p = paths_for(root)
        p["digests"].mkdir(parents=True)
        tdir.mkdir()
        from atomic import write_text

        def fid(s):
            return f"{s}-0000-4000-8000-000000000000"

        def digest(s, date, hh0, hh1, blocks, files=(), calls=10, name=None):
            out = [f"# Session digest: {fid(s)}", f"- file: {fid(s)}.jsonl (0.1 MB)",
                   f"- span (local): {date} {hh0} -> {hh1}", f"- prompts: {len(blocks)} | tool calls: {calls}", ""]
            for ph, pt, oh, ot in blocks:
                out += [f"## [{ph}] PROMPT", pt, "", f"### [{oh}] outcome", ot, ""]
            if files:
                out += ["## FILES WRITTEN/EDITED"] + [f"- {f}" for f in files] + [""]
            out += ["## TOOL TALLY", "Bash: 3", ""]
            name = name or f"digest_{date.replace('-', '')}_{hh0.replace(':', '')}_{s}.md"
            write_text(p["digests"] / name, "\n".join(out))
            return name

        def transcript(s, age):
            f = tdir / f"{fid(s)}.jsonl"
            write_text(f, "{}\n")
            t = time.time() - age * 60
            os.utime(f, (t, t))

        cursor = {"sessions": {}}

        def reg(s, name, woven=False):
            cursor["sessions"][fid(s)] = {"digest": name, "mtime": time.time() - 3 * 3600, "size": 100,
                                          "prompts": 1, "woven": woven}

        def save_cursor():
            write_json(p["cursor"], cursor)

        def go(**kw):
            return run(root=root, transcripts=tdir, quiet=True, **kw)

        def raw(path):
            return path.read_bytes() if path.exists() else b""

        def log():
            return raw(p["log"]).decode("utf-8")

        # the pre-existing log: a 9-hex commit hash that embeds a real sid8, and one session already present by full id
        N, PING, A1, A2, LIVE, PRES, BIG, MARK, EMPTY = ("a1b2c3d4", "b0b0b0b1", "c1c1c1c1", "c2c2c2c2", "d4d4d4d4",
                                                          "e5e5e5e5", "f6f6f6f6", "a7a7a7a7", "abababab")
        write_text(p["log"], f"# Continuity log\n\n## 2026-09-01 \u2014 notes\n- commit 0{N} fixed the parser\n"
                             f"- session {fid(PRES)} set up the repo\n")
        long_out = ("Refactored the parser into three functions, added a regression test for the empty-file case, "
                    "and confirmed the full suite passes on Linux and Windows.")
        reg(N, digest(N, "2026-09-25", "13:26", "15:40", [
            ("13:26", '<pasted_content id="1">\n<system-reminder>ignore this</system-reminder>\nPlease refactor the '
                      'digest parser so it handles empty files.\n</pasted_content>', "13:30", long_out),
            ("15:27", "Also bump the version.", "15:28", "Done."),
        ], files=["src/parser.py", "tests/test_parser.py", "CHANGELOG.md", "pyproject.toml"], calls=40))
        transcript(N, 180)
        reg(PING, digest(PING, "2026-09-25", "09:05", "09:06", [("09:05", "ping", "09:05", "pong")], calls=0))
        digest(PING, "2026-09-25", "09:07", "09:08", [("09:07", "ping", "09:07", "pong")], calls=0,
               name=f"digest_20260925_0907_{PING}.md")                      # a second file sharing the sid8
        for s, hh in ((A1, "03:00"), (A2, "04:00")):
            reg(s, digest(s, "2026-09-25", hh, hh[:3] + "09", [
                ("%s" % hh, '<scheduled-task name="nightly-report">\nBuild the nightly report.\n</scheduled-task>',
                 hh, "Report built and saved.")], files=["reports/nightly.md"], calls=12))
        reg(LIVE, digest(LIVE, "2026-09-26", "11:00", "11:40", [
            ("11:00", "Write the release notes for the next tag.", "11:05", long_out)], calls=9))
        transcript(LIVE, 5)
        reg(PRES, digest(PRES, "2026-09-24", "08:00", "08:30", [
            ("08:00", "Set up the repo.", "08:10", "Initialized the repo with a license and a README.")], calls=5))
        big_files = [f"src/very/deep/package/module_with_a_really_long_descriptive_name_number_{i:02d}.py"
                     for i in range(12)]
        reg(BIG, digest(BIG, "2026-09-25", "16:00", "17:30", [
            ("16:00", ("Rework the whole ingestion layer " * 100)[:3000], "17:25", long_out * 3)],
            files=big_files, calls=90))
        reg(MARK, digest(MARK, "2026-09-25", "18:00", "18:20", [
            ("18:00", "<<<<<<< HEAD\nresolve this conflict \x07 please\n=======\ntheirs\n>>>>>>> branch",
             "18:10", "=======\nResolved the conflict by keeping both changes and re-running the tests.\x07")],
            files=["merge.txt"], calls=6))
        write_text(p["digests"] / f"digest_20260925_1900_{EMPTY}.md", "")
        reg(EMPTY, f"digest_20260925_1900_{EMPTY}.md")
        save_cursor()

        def woven(s):
            return bool(json.loads(raw(p["cursor"]))["sessions"][fid(s)].get("woven"))

        # dry run writes nothing
        before = (raw(p["log"]), raw(p["cursor"]), raw(p["state"]))
        rc, sm = go(dry_run=True)
        chk("dry-run exit", rc == 0, rc)
        chk("dry-run wrote nothing", (raw(p["log"]), raw(p["cursor"]), raw(p["state"])) == before)

        # ---- run 1
        rc, sm = go()
        L = log()
        chk("run1 exit", rc == 0, sm)
        chk("run1 counts", (sm.get("full"), sm.get("short"), sm.get("automated"), sm.get("present"), sm.get("empty"))
            == (3, 1, 2, 1, 1), sm)
        chk("run1 deferred live", sm.get("deferred") == [LIVE], sm.get("deferred"))
        chk("hash-embedded sid8 not counted present", f"\u00b7 {N}**" in L, L[-600:])
        chk("normal entry format", re.search(r"^- \*\*13:26\u201315:40 \u00b7 a1b2c3d4\*\* \u2014 Please refactor the "
                                             r"digest parser so it handles empty files\. \(\+1 prompt\) \u2192 Refactored"
                                             r".* \u00b7 wrote 4: parser\.py, test_parser\.py, CHANGELOG\.md \+1$", L, re.M),
            [x for x in L.splitlines() if N in x])
        chk("ask stripped of wrappers", "ignore this" not in L and "pasted_content" not in L)
        chk("short line", re.search(rf'^- short sessions x1 \(09:05 {PING} "ping" \u2192 pong\)$', L, re.M),
            [x for x in L.splitlines() if "short" in x])
        chk("duplicate sid8 woven once", len(re.findall(rf"(?<![0-9a-f]){PING}(?![0-9a-f])", L)) == 1)
        chk("automated group", re.search(rf"^- automated: nightly-report x2 \({A1} 03:00, {A2} 04:00\)$", L, re.M),
            [x for x in L.splitlines() if "automated" in x])
        chk("live session not woven", LIVE not in L)
        chk("live session not flipped", not woven(LIVE))
        chk("present session not re-woven", len(re.findall(PRES, L)) == 1)
        chk("present session flipped", woven(PRES))
        chk("woven sessions flipped", all(woven(s) for s in (N, PING, A1, A2, BIG, MARK)))
        chk("empty digest not woven", EMPTY not in L)
        chk("empty digest not flipped", not woven(EMPTY))
        big = [x for x in L.splitlines() if BIG in x]
        chk("big entry <= 400 B with bare wrote 12", len(big) == 1 and _nbytes(big[0]) <= LINE_BYTES
            and big[0].endswith("\u00b7 wrote 12"), [(_nbytes(x), x[-40:]) for x in big])
        mk = [x for x in L.splitlines() if MARK in x]
        chk("conflict-marker digest rendered safe", len(mk) == 1 and "\x07" not in L and "<<<<<<< HEAD" in mk[0], mk)
        chk("one header per session date", re.findall(r"^## (\d{4}-\d\d-\d\d) \u2014 sessions", L, re.M) == ["2026-09-25"],
            re.findall(r"^## .*", L, re.M))

        def shape_ok(text):
            ls = text.split("\n")
            bad = [x for x in ls if x != x.rstrip() or _nbytes(x) > LINE_BYTES or x.startswith(MARKER_HEADS)
                   or any(ord(c) < 32 for c in x)]
            hdr = all(ls[i - 1] == "" for i, x in enumerate(ls) if x.startswith("## ") and i > 0)
            return text.endswith("\n") and not text.endswith("\n\n") and not bad and hdr, bad[:2]

        ok, bad = shape_ok(L)
        chk("run1 log shape", ok, bad)

        # ---- run 2: idempotent
        snap = (raw(p["log"]), raw(p["state"]), raw(p["cursor"]))
        rc, sm = go()
        chk("run2 exit", rc == 0, sm)
        chk("run2 appends 0 bytes", sm.get("appended") == 0 and raw(p["log"]) == snap[0], sm)
        chk("run2 state byte-identical", raw(p["state"]) == snap[1])
        chk("run2 cursor byte-identical", raw(p["cursor"]) == snap[2])

        # ---- run 3: a grown digest gives a tail; a grown-but-live digest is deferred and not flipped
        new_out = ("Published the new version to the package index and verified that a clean install imports "
                   "without warnings.")
        digest(N, "2026-09-25", "13:26", "16:10", [
            ("13:26", "Please refactor the digest parser so it handles empty files.", "13:30", long_out),
            ("15:27", "Also bump the version.", "15:28", "Done."),
            ("16:00", "Now publish it.", "16:05", new_out)],
            files=["src/parser.py", "tests/test_parser.py", "CHANGELOG.md", "pyproject.toml"], calls=48)
        transcript(N, 45)
        cursor = json.loads(raw(p["cursor"]))
        cursor["sessions"][fid(N)]["woven"] = False                         # digest.py re-digested it
        digest(MARK, "2026-09-25", "18:00", "18:50", [
            ("18:00", "resolve this conflict please", "18:10", "Resolved the conflict."),
            ("18:40", "and rebase onto main", "18:45", "Rebased onto main and pushed the branch to the remote.")],
            files=["merge.txt"], calls=9)
        transcript(MARK, 3)
        cursor["sessions"][fid(MARK)]["woven"] = False                      # makes the not-flipped check real
        save_cursor()
        before = log()
        rc, sm = go()
        L = log()
        added = L[len(before):]
        cont = [x for x in added.splitlines() if N in x]
        chk("run3 exit", rc == 0, sm)
        chk("cont tail", len(cont) == 1 and "(cont., prompts 3-3)" in cont[0] and "Published the new version" in cont[0]
            and "Refactored" not in cont[0] and cont[0].startswith(f"- **13:26\u201316:10 \u00b7 {N}** (cont."), cont)
        chk("cont under start-date header", "## 2026-09-25 \u2014 sessions" in added, added[:200])
        chk("grown-but-live deferred", MARK not in added and MARK in sm.get("deferred", []), sm.get("deferred"))
        chk("grown-but-live not flipped", not woven(MARK))
        chk("grown flipped after tail", woven(N))
        ok, bad = shape_ok(L)
        chk("run3 log shape", ok, bad)

        # ---- a hole: a cursor-named digest missing on disk refuses and leaves the log untouched
        snap_log = raw(p["log"])
        cursor = json.loads(raw(p["cursor"]))
        good_cursor = raw(p["cursor"])
        cursor["sessions"][fid("0f0f0f0f")] = {"digest": "digest_20260926_0100_0f0f0f0f.md", "mtime": 0.0,
                                               "size": 1, "prompts": 1, "woven": False}
        save_cursor()
        rc, sm = go()
        chk("hole exit 2", rc == 2, (rc, sm))
        chk("hole log untouched", raw(p["log"]) == snap_log)

        # ---- a corrupt cursor refuses too; a missing cursor as well
        write_text(p["cursor"], '{"sessions": {"x": ')
        rc, sm = go()
        chk("corrupt cursor exit 2", rc == 2, (rc, sm))
        chk("corrupt cursor log untouched", raw(p["log"]) == snap_log)
        p["cursor"].unlink()
        rc, sm = go()
        chk("missing cursor exit 2", rc == 2, (rc, sm))

        # ---- the deferred sessions weave once their transcripts are old
        write_text(p["cursor"], good_cursor.decode("utf-8"))
        transcript(LIVE, 90)
        transcript(MARK, 90)
        before = log()
        rc, sm = go()
        L = log()
        added = L[len(before):]
        chk("late exit", rc == 0, sm)
        chk("live session woven once old", len(re.findall(rf"(?m)^- \*\*11:00\u201311:40 \u00b7 {LIVE}\*\* \u2014 Write "
                                                          r"the release notes", added)) == 1, added[-400:])
        chk("late header for its date", "## 2026-09-26 \u2014 sessions" in added)
        chk("grown-live tail once old", re.search(rf"(?m)^- \*\*18:00\u201318:50 \u00b7 {MARK}\*\* \(cont\., prompts 2-2\)"
                                                  r" \u2014 and rebase onto main \u2192 Rebased", added), added[-400:])
        chk("late flags flipped", woven(LIVE) and woven(MARK) and not woven(EMPTY))
        ok, bad = shape_ok(L)
        chk("final log shape", ok, bad)
        chk("sid8 presence both ways", present_ids("x 0a1b2c3d4 y") == set() and present_ids(f"({N})") == {N})

        # ---- a log that does not exist yet is created with its H1
        root2 = tmp / "fresh"
        p2 = paths_for(root2)
        p2["digests"].mkdir(parents=True)
        nm = "digest_20260925_1000_9e9e9e9e.md"
        write_text(p2["digests"] / nm, (p["digests"] / f"digest_20260925_1326_{N}.md").read_text(encoding="utf-8"))
        write_json(p2["cursor"], {"sessions": {fid("9e9e9e9e"): {"digest": nm, "woven": False}}})
        rc, sm = run(root=root2, transcripts=tdir, quiet=True)
        t2 = raw(p2["log"]).decode("utf-8")
        chk("new log created with H1", rc == 0 and t2.startswith(LOG_H1 + "\n\n## ") and shape_ok(t2)[0], t2[:120])

        # ---- a fresh project (no cursor, no digests) is a no-op, not a refusal; with digests the refusal above stands
        root3 = tmp / "fresh3"
        root3.mkdir()
        rc, sm = run(root=root3, transcripts=tdir, quiet=True)
        chk("fresh project exit 0, log not created", rc == 0 and sm.get("nothing") == "fresh"
            and not paths_for(root3)["log"].exists(), (rc, sm))
    except Exception as e:                          # a crash inside the selftest is a failure, never a pass
        import traceback
        fails.append(f"exception {type(e).__name__}: {e} @ {traceback.format_exc().strip().splitlines()[-3][:160]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for f in fails:
        print("SELFTEST FAIL " + f)
    if not fails:
        print("SELFTEST PASS (weave: hex-bounded presence, dry-run no-write, full/short/automated lines, duplicate sid8, "
              "live deferral + no flip, present flip, empty digest skipped, 400 B cap, marker/bell safety, idempotent "
              "rerun, cont. tail, grown-live deferral, hole + corrupt/missing cursor refusal, late weave, log shape, "
              "fresh-project no-op)")
    return 1 if fails else 0


def main(argv=None):
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="Append one line per session from .continuity/digests to CONTINUITY_LOG.md.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="weave now")
    mode.add_argument("--dry-run", action="store_true", help="report what would be appended; write nothing")
    mode.add_argument("--audit", action="store_true", help="count digests present / absent in the log")
    mode.add_argument("--selftest", action="store_true", help="run the both-ways fixtures in a temp tree")
    ap.add_argument("--root", default=".", help="project root (default: current directory)")
    ap.add_argument("--transcripts", default=None, help="transcript directory (default: $CLAUDE_CONFIG_DIR or ~/.claude, then projects/<slug>)")
    ap.add_argument("--live-min", type=float, default=LIVE_MIN, help="minutes a transcript counts as live (default 30)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.audit:
        return audit(a.root)
    rc, _ = run(root=a.root, transcripts=a.transcripts, live_min=a.live_min, dry_run=a.dry_run)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
