# jobtrack

A local-first, single-user job-application tracker that maintains itself.

Two bets, against every tracker that rots after two weeks:

1. **Capture by saying things.** Natural language in, structured records out —
   `jobtrack say "I heard back from Stripe, phone screen Friday"`. It also reads
   your email (read-only) and proposes updates, so the tracker keeps itself current.
2. **Tell you what's working.** Not a pretty kanban board — funnel conversion,
   response rate by résumé version, by source, and by how fast you applied.

Everything runs on your machine. No cloud, no accounts, nothing leaves the laptop
unless you explicitly route a task to a frontier model.

## The one rule that matters

The LLM never writes to the database. Every change is:

```
natural language → LLM emits a structured Action → validate → resolve entities
                 → render a human-readable diff → you confirm → commit + append event
```

Small local models hallucinate; a tracker full of hallucinated data is worse than
none. So every write becomes a **pending proposal** you approve. Status changes and
new applications always confirm; only notes and interactions can be auto-approved
(and only if you opt in). The pipeline is identical whether the request comes from
the CLI, `say`, or Claude over MCP.

`application_events` is an append-only log. Current status, days-in-stage, and all
analytics are **derived** from it — never stored. Mistakes are corrected by
appending, not editing.

## Requirements

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) with a tool-calling chat model and an embedding model:
  ```
  ollama pull qwen3:8b
  ollama pull nomic-embed-text
  ```
  (Defaults; change them in `~/.jobtrack/config.toml`.)

## Install

```bash
uv sync
uv run jobtrack init        # creates ~/.jobtrack/{jobtrack.db,config.toml,snapshots,prep}
```

`JOBTRACK_HOME` overrides the data directory (used by the tests).

## Everyday use

### Capture (natural language)

```bash
uv run jobtrack say "Applied to the Stripe backend role with resume-v3, found it on LinkedIn"
uv run jobtrack say "Recruiter from Datadog reached out about a platform role"
uv run jobtrack say "Phone screen with Stripe went well, they're advancing me"
```

`say` picks one tool and fills its arguments; everything after that — resolving
"Stripe" to a specific application, the diff, your confirmation — is deterministic
code. Ambiguous references ("Stripe" when you have two Stripe applications) prompt
you to choose; they are never guessed.

### Capture (by hand)

```bash
# Register the exact résumé you send, pinned by content hash
uv run jobtrack add doc ./resumes/backend-v3.pdf --label resume-v3-backend

# Capture a job — the JD is archived immutably at capture time
uv run jobtrack add job --company Stripe --title "Backend Engineer" \
    --jd-file ./stripe-jd.txt --source linkedin --posted 2026-07-01
#   or: --jd-url https://…   (a single plain GET; paste the text if it's bot-walled)

uv run jobtrack apply "stripe backend" --resume resume-v3-backend --source referral
uv run jobtrack status "stripe backend" screening --date 2026-07-08
uv run jobtrack note "stripe backend" "Recruiter mentioned a take-home next"
uv run jobtrack add contact "Jana Müller" --company Stripe --relationship recruiter
```

References accept an explicit id (`app#3`, `job#7`, `co#1`) or free text
("the Stripe backend role") resolved the same way `say` resolves them.

### See what's going on

```bash
uv run jobtrack list [--status screening] [--stale] [--ghosted]
uv run jobtrack show app#3          # timeline, interviews, pinned résumé, notes, emails
uv run jobtrack events app#3        # raw event log
uv run jobtrack search "kubernetes" # fuzzy + semantic (after `reindex`)
uv run jobtrack brief               # interviews ≤48h, tasks due, stale apps, pending proposals
```

`ghosted` and `stale` aren't statuses — they're derived from days-since-activity
against the per-stage SLAs in `config.toml`.

### Email ingestion (the anti-rot feature)

```bash
uv run jobtrack email setup     # stores IMAP creds in the OS keyring (Gmail: app password)
uv run jobtrack email poll      # read-only fetch → local classify → queued proposals
uv run jobtrack review          # approve/reject the queue: y / n / s / a(ll) / q
```

`poll` never marks read, moves, deletes, or sends. Each email is classified locally
(`rejection | interview_invite | assessment | recruiter_outreach | offer |
scheduling | irrelevant`), linked to an application by sender domain → thread →
subject, and — if confident and linked — turned into a **pending** proposal.
Nothing auto-commits.

### Insights

```bash
uv run jobtrack stats            # funnel + response rates (resume / source / timing)
uv run jobtrack stats timing     # just posted→applied buckets
uv run jobtrack skills --extract # extract skills from JDs, then map gaps vs your résumé
uv run jobtrack questions        # your interview question bank, built from debriefs
```

- **Funnel**: of applications that reached stage *i*, the fraction that reached any
  later stage, with median days between.
- **Response rate**: employer reacted at all (incl. rejection). **Positive** =
  progressed past `applied`. Reported per résumé version, per source, and per
  time-to-apply bucket.
- **Skill gap**: honest framing — "Kubernetes appears in 71% of the roles you're
  targeting and is not evidenced in your active résumé." Not an ATS score.

### Interview prep & debrief

```bash
uv run jobtrack prep app#3       # one-page dossier: JD + résumé sent + notes + past debriefs
uv run jobtrack debrief app#3    # record how it went + the questions asked (feeds `questions`)
```

### Drive it from Claude (MCP)

```bash
uv run jobtrack mcp              # stdio MCP server
```

Register with Claude Desktop / Claude Code (adjust the path):

```json
{
  "mcpServers": {
    "jobtrack": { "command": "uv", "args": ["run", "jobtrack", "mcp"],
                  "cwd": "C:/Projects/jobtrack" }
  }
}
```

Over MCP there's no terminal to type "y" into, so confirmation is conversational:
write tools return a diff + proposal id, Claude shows it to you, and your "yes"
becomes a `confirm_proposal` call. Same pipeline, different surface.

## Configuration

`~/.jobtrack/config.toml` (created by `init`, fully local by default). Notable knobs:

- `[llm] chat_model / embed_model` — Ollama model names.
- `[llm.tasks]` — route individual tasks (`classify`, `jd_parse`, `prep`, `draft`,
  `say`) to `local` or `anthropic`. Anthropic needs `uv sync --extra anthropic` and
  `ANTHROPIC_API_KEY`; embeddings always stay local.
- `[sla_days]` — per-stage staleness thresholds.
- `[email]` — IMAP host/folder and the classification confidence threshold.
- `[approvals] auto` — which action kinds may skip confirmation (`add_note`,
  `log_interaction` only; the floor is enforced in code).

## Architecture

```
core/    models, event sourcing, derived status, proposals, analytics, prep  (no LLM/MCP)
llm/     provider abstraction (Ollama|Anthropic), the tool registry, entity resolution
ingest/  JD capture, IMAP poller, classifier, linker
mcp/     FastMCP server (thin wrappers over the tool registry)
cli/     Typer commands
```

`llm/tools.py` is the single source of truth for tool schemas — the MCP server and
the local `say` model consume the same registry, so there is one contract and one
test suite for both front doors.

## Development

```bash
uv run pytest          # 86 tests
uv run ruff check .
uv run mypy            # --strict, configured in pyproject.toml
```

Migrations are managed with Alembic (`src/jobtrack/migrations/`) and applied
automatically on startup. The semantic index (sqlite-vec) is rebuilt on demand
with `jobtrack reindex`; if sqlite-vec or Ollama isn't available, fuzzy search
still works and semantic search tells you why it's skipped.

## Non-goals

No LinkedIn/Indeed scraper (paste the JD or point at a URL). No web UI. No auth,
no Docker, no multi-tenancy. No "ATS keyword score" — résumé/JD matching is framed
honestly as missing evidence, not a number to game.
