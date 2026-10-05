# claude-continuity — design

**The problem.** Claude Code loads your `CLAUDE.md` and its own auto memory into every session, but auto memory keeps only
what the model decides is worth saving. The rest of what you told the last session, what it did, what it wrote and what
it got wrong lives in a transcript nobody reads, and transcripts are deleted after 30 days by default.

**The product.** A zero-LLM continuity layer for Claude Code projects. Three deterministic pieces, wired by one install
command, all local, no accounts, no API keys, no model calls:

| Piece | Runs when | Reads | Writes |
|---|---|---|---|
| `digest` | on a schedule or on demand | `~/.claude/projects/<slug>/*.jsonl` (the transcripts Claude Code already keeps) | `.continuity/digests/digest_<date>_<sid8>.md` — prompts, outcomes, files written, tool tally; plus a cursor so nothing is digested twice |
| `weave` | after `digest` | the digests + `CONTINUITY_LOG.md` | one dated, ≤400-byte line per session appended to `CONTINUITY_LOG.md`; idempotent; never rewrites |
| `brief` | at every session start (a `SessionStart` hook) | `NOW.md`, `CORRECTIONS.md`, the log tail, unwoven digests | a ≤9,600-byte context block the new session boots with, plus `--verify` which proves from the transcripts that the hook actually delivered |

**What compounds for the user.** A greppable log of every session by id, a NOW file that stays short by rule, a corrections
file the model sees every boot, and a proof that the hook fired. After a month the project has a memory; after a quarter the
corrections file is the team's operating manual.

## Non-negotiables (from the system this is extracted from, which has run since July 2026)

1. **Zero model cost.** Everything is parsing and rendering. If a step would need an LLM, it is not in this kit.
2. **Append-only where it matters.** The log is never read-modify-written; the brief is generated, never stored.
3. **Fail silent at boot, loud on request.** A hook that throws blanks the session's context; `brief` catches everything and
   exits 0. `brief --verify` is the loud path: it reads the newest transcripts and says whether a brief was delivered.
4. **Budgets are rules, not hopes.** The brief has a byte cap and trims from the tail with a visible notice naming what it
   dropped; NOW.md has a line/byte budget the `brief` reports on; log lines have a byte cap.
5. **Every module proves it can fail.** `--selftest` builds fixtures in a temp directory and asserts both directions
   (a clean input passes, a planted defect is caught). `selftest.py` runs them all and a fire drill.
6. **No secrets, no telemetry, no network.**

## Install

`python3 ../claude-continuity/install.py` (from the project root; `python` on Windows):

1. Backs up `.claude/settings.json` to `.claude/settings.json.bak-continuity`, then adds the `SessionStart` hook
   (the interpreter that ran the installer + `.continuity/kit/hooks/session_start.py`) and keeps every other key
   (re-serialized; the original stays byte-for-byte in the backup). Idempotent.
2. Creates `.continuity/` (digests, cursor, state), `CONTINUITY_LOG.md`, `NOW.md`, `CORRECTIONS.md` if absent.
3. Runs `continuity/selftest.py`; refuses to finish if any selftest fails.
4. Prints the two commands to schedule (`digest` + `weave`, every 2h) for Windows Task Scheduler and cron.

`python3 ../claude-continuity/install.py --uninstall` removes only the hook it added and leaves every file.

## Transcript facts the parser relies on (Claude Code, verified 2026-09)

- One JSON object per line. `type` ∈ {user, assistant, system, attachment, ...}; `message.content` is a string or a list of
  blocks (`text`, `tool_use`, `tool_result`); `timestamp` is ISO-8601 UTC; `system` rows with `subtype: turn_duration` mark
  the end of a turn; `Write`/`Edit`/`NotebookEdit` tool inputs carry the file path.
- A session id is the file stem (36-char UUID); the first 8 hex characters are unique enough to grep for.
- Automated prompts (scheduled tasks, launcher prompts) start with recognizable tags; the digest keeps them but marks them.
- Where the transcripts live: `<config>/projects/<slug>/`, where `<config>` is `$CLAUDE_CONFIG_DIR` or `~/.claude` and
  `<slug>` is the absolute project path with every non-alphanumeric character turned into `-`. That rule has been checked
  against real folders on Windows only (26 of 26 held, one of them only up to letter case, across spaces, `.`, `_`, `~`,
  `-` and a non-ASCII dash). So `paths.py` does not depend on it: when no folder carries the slug, exact or
  case-insensitive, it reads the first `cwd` each folder's transcripts record (at most 5 files, 256 KB read from each)
  and takes the folder whose cwd is the project.

## What the paid tiers add (see PRICING.md)

Setup ($600): install on the buyer's projects, schedule the jobs, tune the NOW/corrections files to their workflow, one
review call. Customization ($2,500): their own sections in the brief (their tracker, their ticket system), their lane
conventions, a corrections lexicon tuned to how their lead actually gives feedback, delivered as a fork with tests.
