"""install.py -- wire claude-continuity into a Claude Code project. Run from the project root:

    python <path-to-claude-continuity>/install.py            install (idempotent)
    python <path-to-claude-continuity>/install.py --uninstall  remove only the hook this installer added
    python <path-to-claude-continuity>/install.py --dry-run    show what would change, change nothing

What it does, in order:
  1. Backs up .claude/settings.json to .claude/settings.json.bak-continuity (first install only), then adds ONE
     SessionStart hook entry that runs the kit's brief. Every other key is kept (the file is re-serialized with
     2-space indent; the original stays byte-for-byte in the backup).
  2. Creates .continuity/ (digests, cursor, state), CONTINUITY_LOG.md, NOW.md and CORRECTIONS.md if absent.
  3. Copies the kit (continuity/ + hooks/) into <project>/.continuity/kit/ so the project is self-contained.
  4. Runs the kit's self-tests and refuses to finish if any fail.
  5. Prints the two commands to schedule every 2 hours (digest, then weave).

Standard library only. No network. Nothing is deleted.
"""
import argparse
import json
import ntpath
import os
import posixpath
import shlex
import shutil
import subprocess
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
HOOK_MARK = "claude-continuity"


def load_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def hook_entry(python, script):
    return {"hooks": [{"type": "command", "command": python, "args": [script], "timeout": 10,
                       "statusMessage": "continuity brief", "_kit": HOOK_MARK}]}


def has_our_hook(settings):
    for grp in (settings.get("hooks", {}) or {}).get("SessionStart", []) or []:
        for h in grp.get("hooks", []) or []:
            if h.get("_kit") == HOOK_MARK:
                return True
    return False


def add_hook(settings, python, script):
    hooks = settings.setdefault("hooks", {})
    groups = hooks.setdefault("SessionStart", [])
    groups.append(hook_entry(python, script))
    return settings


def remove_hook(settings):
    groups = (settings.get("hooks", {}) or {}).get("SessionStart", []) or []
    kept = []
    for grp in groups:
        inner = [h for h in (grp.get("hooks", []) or []) if h.get("_kit") != HOOK_MARK]
        if inner:
            grp = dict(grp); grp["hooks"] = inner; kept.append(grp)
    if "hooks" in settings:
        if kept:
            settings["hooks"]["SessionStart"] = kept
        else:
            settings["hooks"].pop("SessionStart", None)
            if not settings["hooks"]:
                settings.pop("hooks")
    return settings


def write_settings(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    os.replace(tmp, path)


def schedule_commands(py, kit_dir, root, posix):
    """The two lines to schedule (digest, then weave), quoted for sh/cron when posix is true, else for Windows."""
    join = posixpath.join if posix else ntpath.join
    quote = (lambda argv: " ".join(shlex.quote(x) for x in argv)) if posix else subprocess.list2cmdline
    return [quote([py, join(kit_dir, "continuity", s), "--run", "--root", root]) for s in ("digest.py", "weave.py")]


def ensure_file(path, text, dry):
    if os.path.exists(path):
        return "kept   " + path
    if not dry:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return "created " + path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="interpreter the hook runs with (default: this one)")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    settings_path = os.path.join(root, ".claude", "settings.json")
    kit_dir = os.path.join(root, ".continuity", "kit")
    hook_script = os.path.join(kit_dir, "hooks", "session_start.py")
    print(f"project: {root}")

    settings = load_settings(settings_path)
    if a.uninstall:
        if not has_our_hook(settings):
            print("no claude-continuity hook found; nothing to do"); return 0
        if not a.dry_run:
            write_settings(settings_path, remove_hook(settings))
        print("removed the SessionStart hook this installer added; every file kept")
        return 0

    # 1. hook
    if has_our_hook(settings):
        print("hook   already present in .claude/settings.json")
    else:
        if not a.dry_run:
            if os.path.exists(settings_path) and not os.path.exists(settings_path + ".bak-continuity"):
                shutil.copy2(settings_path, settings_path + ".bak-continuity")
            write_settings(settings_path, add_hook(settings, a.python, hook_script))
        print(f"hook   added SessionStart -> {hook_script} (backup: settings.json.bak-continuity)")

    # 2. files
    for line in (
        ensure_file(os.path.join(root, "CONTINUITY_LOG.md"), "# Continuity log\n\n_One line per session, appended by continuity/weave.py. Append-only._\n", a.dry_run),
        ensure_file(os.path.join(root, "NOW.md"), "# NOW - what is live this week\n\n_Keep it under 20 lines / 2,400 bytes; the brief tells you when it is over. History goes in the log, not here._\n\n- \n", a.dry_run),
        ensure_file(os.path.join(root, "CORRECTIONS.md"), "# Corrections - feedback that must not be repeated\n\n_Newest at the bottom; one bullet per correction, with the date and the rule it became._\n\n- \n", a.dry_run),
        ensure_file(os.path.join(root, ".continuity", "digests", ".keep"), "", a.dry_run),
    ):
        print(line)

    # 3. copy the kit into the project
    if not a.dry_run:
        for sub in ("continuity", "hooks"):
            src, dst = os.path.join(KIT, sub), os.path.join(kit_dir, sub)
            os.makedirs(dst, exist_ok=True)
            for name in os.listdir(src):
                if name.endswith(".py"):
                    shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
    print(f"kit    copied to {kit_dir}")

    # 4. self-tests
    if a.dry_run:
        print("dry-run: self-tests skipped"); return 0
    rc = subprocess.call([a.python, os.path.join(kit_dir, "continuity", "selftest.py")])
    if rc != 0:
        print("INSTALL REFUSED: a self-test failed (see above). The hook was added; run --uninstall to remove it.")
        return 1

    # 5. schedule hint
    print("\nSchedule these two every 2 hours (digest, then weave):")
    for line in schedule_commands(a.python, kit_dir, root, os.name != "nt"):
        print("  " + line)
    print("Windows: schtasks /Create /SC HOURLY /MO 2 /TN claude-continuity /TR \"<the two commands joined with &>\"")
    print("cron:    0 */2 * * * <the two commands joined with &&>")
    print("\nOpen a new Claude Code session in this project: its context starts with CONTINUITY BRIEF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
