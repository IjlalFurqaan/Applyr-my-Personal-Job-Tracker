# Applyr — My Personal Job Tracker

A local-first, single-user job-application tracker that maintains itself.

Two bets, against every tracker that rots after two weeks:

1. **Capture by saying things.** Natural language in, structured records out —
   `applyr say "I heard back from Stripe, phone screen Friday"`. It also reads
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
  (Defaults; change them in `~/.applyr/config.toml`.)

## Install

```bash
uv sync
uv run applyr init        # creates ~/.applyr/{applyr.db,config.toml,snapshots,prep}
```

`APPLYR_HOME` overrides the data directory (used by the tests).

## Everyday use

### Capture (natural language)

```bash
uv run applyr say "Applied to the Stripe backend role with resume-v3, found it on LinkedIn"
uv run applyr say "Recruiter from Datadog reached out about a platform role"
uv run applyr say "Phone screen with Stripe went well, they're advancing me"
```

`say` picks one tool and fills its arguments; everything after that — resolving
"Stripe" to a specific application, the diff, your confirmation — is deterministic
code. Ambiguous references ("Stripe" when you have two Stripe applications) prompt
you to choose; they are never guessed.

### Capture (by hand)

```bash
# Register the exact résumé you send, pinned by content hash
uv run applyr add doc ./resumes/backend-v3.pdf --label resume-v3-backend

# Capture a job — the JD is archived immutably at capture time
uv run applyr add job --company Stripe --title "Backend Engineer" \
    --jd-file ./stripe-jd.txt --source linkedin --posted 2026-07-01
#   or: --jd-url https://…   (a single plain GET; paste the text if it's bot-walled)

uv run applyr apply "stripe backend" --resume resume-v3-backend --source referral
uv run applyr status "stripe backend" screening --date 2026-07-08
uv run applyr note "stripe backend" "Recruiter mentioned a take-home next"
uv run applyr add contact "Jana Müller" --company Stripe --relationship recruiter
```

References accept an explicit id (`app#3`, `job#7`, `co#1`) or free text
("the Stripe backend role") resolved the same way `say` resolves them.

### See what's going on

```bash
uv run applyr list [--status screening] [--stale] [--ghosted]
uv run applyr show app#3          # timeline, interviews, pinned résumé, notes, emails
uv run applyr events app#3        # raw event log
uv run applyr search "kubernetes" # fuzzy + semantic (after `reindex`)
uv run applyr brief               # interviews ≤48h, tasks due, stale apps, pending proposals
```

`ghosted` and `stale` aren't statuses — they're derived from days-since-activity
against the per-stage SLAs in `config.toml`.

### Email ingestion (the anti-rot feature)

```bash
uv run applyr email setup     # stores IMAP creds in the OS keyring (Gmail: app password)
uv run applyr email poll      # read-only fetch → local classify → queued proposals
uv run applyr review          # approve/reject the queue: y / n / s / a(ll) / q
```

`poll` never marks read, moves, deletes, or sends. Each email is classified locally
(`rejection | interview_invite | assessment | recruiter_outreach | offer |
scheduling | irrelevant`), linked to an application by sender domain → thread →
subject, and — if confident and linked — turned into a **pending** proposal.
Nothing auto-commits.

### Insights

```bash
uv run applyr stats            # funnel + response rates (resume / source / timing)
uv run applyr stats timing     # just posted→applied buckets
uv run applyr skills --extract # extract skills from JDs, then map gaps vs your résumé
uv run applyr questions        # your interview question bank, built from debriefs
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
uv run applyr prep app#3       # one-page dossier: JD + résumé sent + notes + past debriefs
uv run applyr debrief app#3    # record how it went + the questions asked (feeds `questions`)
```

### Web UI

```bash
uv run applyr ui               # serves http://127.0.0.1:8765 and opens the browser
```

A single-page app over the exact same tool registry as the CLI and MCP —
dashboard (brief + funnel), application list with timelines, a natural-language
capture box, and a **Review** queue where pending proposals show their diff and
commit only when you press Confirm. Localhost only, self-contained (no CDN, no
build step, works offline); the propose→confirm rule holds in the browser the
same as everywhere else.

### Drive it from Claude (MCP)

```bash
uv run applyr mcp              # stdio MCP server
```

Register with Claude Desktop / Claude Code (adjust the path):

```json
{
  "mcpServers": {
    "applyr": { "command": "uv", "args": ["run", "applyr", "mcp"],
                  "cwd": "C:/Projects/Applyr-my-Personal-Job-Tracker" }
  }
}
```

Over MCP there's no terminal to type "y" into, so confirmation is conversational:
write tools return a diff + proposal id, Claude shows it to you, and your "yes"
becomes a `confirm_proposal` call. Same pipeline, different surface.

## Configuration

`~/.applyr/config.toml` (created by `init`, fully local by default). Notable knobs:

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
web/     Starlette app + single-file SPA (thin wrappers over the same registry)
cli/     Typer commands
```

`llm/tools.py` is the single source of truth for tool schemas — the MCP server,
the web UI, and the local `say` model consume the same registry, so there is one
contract and one test suite for every front door.

## Development

```bash
uv run pytest          # 99 tests
uv run ruff check .
uv run mypy            # --strict, configured in pyproject.toml
```

Migrations are managed with Alembic (`src/applyr/migrations/`) and applied
automatically on startup. The semantic index (sqlite-vec) is rebuilt on demand
with `applyr reindex`; if sqlite-vec or Ollama isn't available, fuzzy search
still works and semantic search tells you why it's skipped.

## Non-goals

No LinkedIn/Indeed scraper (paste the JD or point at a URL). No auth, no Docker,
no multi-tenancy, no cloud — the web UI is a localhost-only page over the same
pipeline, not a hosted app. No "ATS keyword score" — résumé/JD matching is framed
honestly as missing evidence, not a number to game.
