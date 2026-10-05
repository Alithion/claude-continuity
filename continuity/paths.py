"""paths.py -- where Claude Code keeps a project's transcripts: ONE rule for digest, weave and brief.

    python continuity/paths.py --selftest

Claude Code writes <config>/projects/<slug>/<session id>.jsonl. <config> is $CLAUDE_CONFIG_DIR, or ~/.claude when it is
unset; <slug> is the absolute project path with every non-alphanumeric character turned into '-'. The path Claude Code
saw can differ from the one a scheduler passes here (a symlink such as macOS /var -> /private/var, a Windows junction or
8.3 short name, folder case), so the lookup tries the absolute path, then the real path, each exact and then
case-insensitive. If no folder carries either slug, it reads the working directory Claude Code recorded in each folder's
transcripts (the first "cwd" in at most 5 files, 256 KB read from each) and takes the folder whose cwd is this project,
so finding the transcripts does not rest on the slug rule holding on every platform and Claude Code version. In a
SessionStart hook the folder of the hook's own transcript_path wins outright.

Standard library only.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path


def project_slug(root):
    """How Claude Code names a project's transcript folder: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def claude_home():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


_CWD_RX = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_READ_CAP = 262144  # bytes read from each transcript when looking for its cwd
_FILES_PER_DIR = 5  # transcripts tried per folder


def _cwd_of(folder):
    """The first working directory Claude Code recorded in this folder's transcripts (bounded read), or None."""
    try:
        with os.scandir(str(folder)) as it:
            names = sorted(e.name for e in it if e.name.endswith(".jsonl") and e.is_file())
    except OSError:
        return None
    for name in names[:_FILES_PER_DIR]:
        try:
            with open(os.path.join(str(folder), name), "rb") as fh:
                raw = fh.read(_READ_CAP)
        except OSError:
            continue
        m = _CWD_RX.search(raw)
        if not m:
            continue
        try:
            cwd = json.loads(b'"' + m.group(1) + b'"')
        except ValueError:
            continue
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _same_path(a, b):
    """True when two strings name the same path: normalised, and case-folded where the OS folds case."""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _cwd_scan(projects, root):
    """The first transcript folder whose recorded cwd is this root (its absolute or real path), or None."""
    want = []
    for p in (os.path.abspath(str(root)), os.path.realpath(str(root))):
        if p not in want:
            want.append(p)
    try:
        with os.scandir(str(projects)) as it:
            names = sorted(e.name for e in it if e.is_dir())
    except OSError:
        return None
    for name in names:
        cwd = _cwd_of(Path(projects) / name)
        if cwd is None:
            continue
        seen = [cwd]
        if os.path.exists(cwd):
            seen.append(os.path.realpath(cwd))
        if any(_same_path(c, w) for c in seen for w in want):
            return Path(projects) / name
    return None


def transcripts_dir(root, transcript_path=None):
    """This project's transcript folder; the absolute-path slug under <config>/projects when none exists yet."""
    if isinstance(transcript_path, str) and transcript_path.strip():
        d = Path(transcript_path).parent
        if d.is_dir():
            return d
    projects = claude_home() / "projects"
    slugs = []
    for p in (os.path.abspath(str(root)), os.path.realpath(str(root))):
        if project_slug(p) not in slugs:
            slugs.append(project_slug(p))
    for s in slugs:
        if (projects / s).is_dir():
            return projects / s
    try:
        dirs = [d for d in sorted(projects.iterdir()) if d.is_dir()]
    except OSError:
        dirs = []
    for s in slugs:
        for d in dirs:
            if d.name.lower() == s.lower():
                return d
    hit = _cwd_scan(projects, root)
    if hit is not None:
        return hit
    return projects / slugs[0]


# ----------------------------------------------------------------------------- selftest (both ways)
def _link(link, target):
    """A directory symlink (POSIX) or junction (Windows, no admin needed); False when neither can be made."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return True
    except Exception:
        return False


def selftest():
    fails, ran = [], [0]

    def ok(cond, msg):
        ran[0] += 1
        if not cond:
            fails.append(msg)

    saved = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE")}
    try:
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            home, cfg, proj = t / "home", t / "cfg", t / "My Proj.v2"
            proj.mkdir()
            projects = home / ".claude" / "projects"
            slug = project_slug(os.path.abspath(str(proj)))
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
            ok(project_slug(r"C:\Users\x\proj") == "C--Users-x-proj", "slug of a Windows path")
            ok(project_slug("/home/a.b/my proj") == "-home-a-b-my-proj", "slug of a POSIX path")
            ok(transcripts_dir(proj) == projects / slug, "no folder yet: the absolute-path slug under ~/.claude")
            (projects / slug).mkdir(parents=True)
            ok(transcripts_dir(proj) == projects / slug, "default: ~/.claude/projects/<slug>")
            (cfg / "projects" / slug).mkdir(parents=True)
            os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
            ok(transcripts_dir(proj) == cfg / "projects" / slug, "CLAUDE_CONFIG_DIR wins over ~/.claude")
            here = os.path.dirname(os.path.abspath(__file__))  # the drift this module exists to end: all three agree
            if here not in sys.path:
                sys.path.insert(0, here)
            import brief
            import digest
            import weave
            want = cfg / "projects" / slug
            ok(Path(digest.default_transcript_dir(proj)) == want and Path(brief.default_transcripts(proj)) == want
               and Path(weave.default_transcripts(proj)) == want, "digest, brief and weave resolve the same folder")
            os.environ.pop("CLAUDE_CONFIG_DIR")
            upper = projects / slug.swapcase()
            (projects / slug).rename(upper)
            got = transcripts_dir(proj)
            ok(got.is_dir() and os.path.samefile(str(got), str(upper)), "a folder that differs only in case is found")
            try:                                            # only a case-sensitive filesystem can hold both
                (projects / slug).mkdir()
                ok(transcripts_dir(proj) == projects / slug, "an exact folder beats a case-insensitive one")
            except FileExistsError:
                pass
            real_only = t / "real_only"
            real_only.mkdir()
            link = t / "link to proj"
            if _link(link, real_only):                      # Claude Code saw the real path; the caller passes the link
                rp = projects / project_slug(os.path.realpath(str(link)))
                rp.mkdir()
                got = transcripts_dir(link)
                ok(got.is_dir() and os.path.samefile(str(got), str(rp)), "a symlinked root finds the real path's folder")
                lp = projects / project_slug(os.path.abspath(str(link)))
                lp.mkdir()
                ok(transcripts_dir(link) == lp, "the link path's own folder wins when both exist")
            tp = cfg / "elsewhere" / "abc.jsonl"
            tp.parent.mkdir(parents=True)
            ok(transcripts_dir(proj, str(tp)) == tp.parent, "the hook's transcript_path folder wins when it exists")
            base = transcripts_dir(proj)
            ok(transcripts_dir(proj, str(t / "missing" / "x.jsonl")) == base, "a missing transcript_path is ignored")
            ok(transcripts_dir(proj, "") == base, "an empty transcript_path is ignored")

            # the cwd fallback: when no folder carries the slug, the folder whose transcripts record this root is found
            def tx(folder, cwd, name="s1.jsonl", junk=b""):
                folder.mkdir(parents=True, exist_ok=True)
                rec = json.dumps({"type": "user", "cwd": cwd, "sessionId": "x"}).encode("utf-8")
                (folder / name).write_bytes(b'{"type":"summary"}\n' + junk + rec + b"\n")

            fb = t / "fbhome"
            os.environ["HOME"] = os.environ["USERPROFILE"] = str(fb)
            fbp = fb / ".claude" / "projects"
            renamed = fbp / "-renamed-by-another-rule"
            tx(renamed, os.path.abspath(str(proj)))
            ok(transcripts_dir(proj) == renamed, "no slug folder: the folder whose transcripts record this root is found")
            ok(Path(digest.default_transcript_dir(proj)) == renamed and Path(brief.default_transcripts(proj)) == renamed
               and Path(weave.default_transcripts(proj)) == renamed, "digest, brief and weave take the same cwd match")
            gone = t / "moved away"                         # not on disk: only normalisation can match the extra separators
            tx(fbp / "-moved-project", str(gone) + os.sep + os.sep)
            ok(transcripts_dir(gone) == fbp / "-moved-project", "a recorded cwd that differs only by trailing separators")
            other, lone = t / "Other Proj", t / "lone"
            other.mkdir()
            lone.mkdir()
            tx(fbp / "-someone-else", str(other))
            ok(transcripts_dir(lone) == fbp / project_slug(os.path.abspath(str(lone))),
               "a folder that records another path is never taken")
            both = t / "both"
            both.mkdir()
            exact = fbp / project_slug(os.path.abspath(str(both)))
            exact.mkdir(parents=True)
            tx(fbp / "-a-decoy", os.path.abspath(str(both)))
            ok(transcripts_dir(both) == exact, "the slug folder beats a cwd match")
            real2, link2 = t / "real2", t / "link2"
            real2.mkdir()
            if _link(link2, real2):                         # Claude Code recorded the real path; the caller passes the link
                tx(fbp / "-seen-via-real-path", os.path.realpath(str(real2)))
                ok(transcripts_dir(link2) == fbp / "-seen-via-real-path", "a linked root matches the real path recorded")
            tx(fbp / "-past-the-cap", os.path.abspath(str(lone)), junk=b"x" * (_READ_CAP + 1))
            ok(_cwd_of(fbp / "-past-the-cap") is None, "a cwd past the byte cap is not read")
            late = fbp / "-bad-then-good"
            late.mkdir(parents=True)
            (late / "a.jsonl").write_bytes(b'\xff\xfe\x00 not json "cwd": "\\uZZZZ"\n')
            (late / "b.jsonl").write_bytes(b'{"type":"summary"}\n')
            tx(late, "/somewhere/else", name="c.jsonl")
            ok(_cwd_of(late) == "/somewhere/else", "undecodable and cwd-less transcripts are skipped")
            os.environ["HOME"] = os.environ["USERPROFILE"] = str(t / "nohome")
            ok(transcripts_dir(lone) == t / "nohome" / ".claude" / "projects" / project_slug(os.path.abspath(str(lone))),
               "no projects folder at all: the slug default, no error")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if fails:
        print(f"SELFTEST {len(fails)} FAILURE(S): " + "; ".join(fails))
        return 1
    print(f"SELFTEST PASS (paths: {ran[0]} checks)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(transcripts_dir(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
