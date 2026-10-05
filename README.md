# claude-continuity

**Deterministic memory for Claude Code projects: a plain-file record of every session, and a boot brief built from it. No model calls, no accounts, no network.**

Claude Code already loads your `CLAUDE.md` and its own auto memory into every session. Auto memory holds only what the
model chose to save, and the transcripts of past sessions are deleted after 30 days by default. This kit keeps a
plain-file record of every session and shows the recent ones to the next session, deterministically:

- **`digest`** turns the transcripts Claude Code already saves into short per-session digests (prompts, what was done, files written).
- **`weave`** appends one dated line per session to `CONTINUITY_LOG.md`, so every session is greppable by id. Idempotent.
- **`brief`** is a `SessionStart` hook that injects a small boot brief: what is live now (`NOW.md`), the corrections your lead
  has given (`CORRECTIONS.md`), the last sessions, and the sessions the log has not seen yet. It trims to a byte budget and
  names what it dropped. `brief --verify` proves from the transcripts that the hook actually delivered.

Everything is plain Python 3.9+, standard library only. The path rules cover Windows, macOS and Linux; the end-to-end
test (install, a real Claude Code session, digest, weave, uninstall) has so far been run on Windows, with Python 3.9,
3.10 and 3.12.

The commands below say `python3`. On Windows, type `python` (or `py`) instead.

## How this relates to Claude Code's built-in memory

Claude Code already loads `CLAUDE.md` and its own [auto memory](https://code.claude.com/docs/en/memory) into every session. This kit replaces neither. It adds what they do not keep:

| Kit piece | Built in today | What the kit adds |
|---|---|---|
| `digest` | session titles and summaries in the `/resume` picker, `/recap` for the current session, `/export` | one digest file per session in your repo, kept after the transcripts are deleted (30 days by default) |
| `weave` | the searchable `/resume` picker | an append-only log of every session in a plain file you can grep |
| `brief` | `CLAUDE.md` and auto memory load at start; auto memory holds what the model chose to save | the last sessions, and the ones the log has not seen, injected the same way every time within a byte budget |
| `brief --verify` | `/context` lists the memory files loaded in the current session | a check, from the transcripts, that the hook delivered in each recent session |

`digest` and `--verify` read the `.jsonl` transcripts. The [session docs](https://code.claude.com/docs/en/sessions) call that format internal and say it can change between releases, so a Claude Code update may require an update to those two commands.

## Install

```bash
git clone <this repo> claude-continuity
cd your-project
python3 ../claude-continuity/install.py
```

The installer backs up `.claude/settings.json`, adds one `SessionStart` hook, copies the kit into `.continuity/kit/`,
creates `CONTINUITY_LOG.md`, `NOW.md` and `CORRECTIONS.md`, runs the self-tests, and prints the two commands to schedule
every 2 hours, with absolute paths. From the project root they are:

```bash
python3 .continuity/kit/continuity/digest.py --run
python3 .continuity/kit/continuity/weave.py --run
```

Before your first session they report that there is nothing to do yet and exit cleanly. Open a new Claude Code session in
the project: the first thing in its context is `CONTINUITY BRIEF`.

## Self-tests

From the project root after installing (or `python3 continuity/selftest.py` inside the clone):

```bash
python3 .continuity/kit/continuity/selftest.py
```

Every module builds fixtures in a temp directory and asserts both directions (clean input passes, planted defect is caught),
then a fire drill proves the runner itself can fail.

## Files it creates in your project

| File | What it is | Who writes it |
|---|---|---|
| `.continuity/digests/` | one markdown digest per session | `digest` |
| `.continuity/cursor.json` | which transcripts were digested and woven | `digest`, `weave` |
| `CONTINUITY_LOG.md` | one line per session, dated headers, append-only | `weave` |
| `NOW.md` | what is live this week (you keep it under the budget; the brief tells you when it is over) | you |
| `CORRECTIONS.md` | the feedback your lead gave that must not be repeated | you |

## Uninstall

`python3 ../claude-continuity/install.py --uninstall` (from the project root) removes the hook it added and nothing else.
It rewrites `.claude/settings.json` with every other key kept; the original file stays byte-for-byte in
`.claude/settings.json.bak-continuity`.

## Where this comes from

Extracted from a continuity system that has run daily since July 2026 on a two-machine, multi-agent setup: session digests,
a boot brief, a deterministic weave, lane watchdogs and both-ways gates. This kit is the generic core of that system.

## Paid setup and customization

See `PRICING.md`. The kit is MIT-licensed and complete on its own; the paid tiers are for teams that want it installed,
scheduled and tuned to their workflow, or extended with their own brief sections.

*Made by an AI agent with a human reviewer. The code is deterministic; the prose was reviewed by a person.*
