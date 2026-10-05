"""atomic.py -- the only writers the kit uses. Temp file + os.replace, so a crash never leaves a half-written file.

append_text keeps the target's own newline style and refuses control characters (a bell in a log line breaks sync tools);
write_json round-trips before it replaces. Standard library only.
"""
import json
import os
import tempfile

CONTROL = {chr(c) for c in range(32)} - {"\n", "\t"}


def _replace(path, data):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_text(path, text, newline="\n"):
    _replace(path, text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8"))


def write_json(path, obj):
    data = json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True)
    json.loads(data)                       # round-trip before anything touches the disk
    _replace(path, (data + "\n").encode("utf-8"))


def append_text(path, text):
    """Append, matching the file's newline style; the file always ends with exactly one newline."""
    bad = [ch for ch in text if ch in CONTROL or ch == "\r"]
    if bad:
        raise ValueError("control characters in appended text: %r" % bad[:5])
    old = b""
    if os.path.exists(path):
        with open(path, "rb") as fh:
            old = fh.read()
    nl = "\r\n" if b"\r\n" in old else "\n"
    body = text.replace("\n", nl).encode("utf-8")
    old = old.rstrip(b"\r\n") + (nl.encode() if old else b"")
    _replace(path, old + body.rstrip(nl.encode()) + nl.encode())
    return len(body)


def selftest():
    import shutil
    tmp = tempfile.mkdtemp(prefix="atomic_")
    fails = []
    try:
        p = os.path.join(tmp, "a.md")
        write_text(p, "one\ntwo\n")
        append_text(p, "three\n")
        assert open(p, "rb").read() == b"one\ntwo\nthree\n", "append LF"
        c = os.path.join(tmp, "c.md")
        write_text(c, "x\r\ny\r\n", newline="\r\n")
        append_text(c, "z\n")
        assert open(c, "rb").read() == b"x\r\ny\r\nz\r\n", "append matches CRLF"
        try:
            append_text(p, "bad\x07\n"); fails.append("control char accepted")
        except ValueError:
            pass
        j = os.path.join(tmp, "s.json")
        write_json(j, {"b": 1, "a": [1, 2]})
        assert json.load(open(j, encoding="utf-8")) == {"a": [1, 2], "b": 1}, "json round trip"
        assert not [f for f in os.listdir(tmp) if f.startswith(".tmp-")], "temp file left behind"
    except AssertionError as e:
        fails.append(str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("SELFTEST FAIL  atomic: " + f)
    print("SELFTEST " + ("PASS (atomic: LF/CRLF append, control-char refusal, json round-trip, no temp residue)" if not fails else f"{len(fails)} FAILURE(S)"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
