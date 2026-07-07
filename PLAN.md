# applyr — Phase 0 Plan

Single-user, local-first job-application tracker. Natural-language capture, email-driven
self-maintenance, event-sourced pipeline, analytics that say what's working.
Nothing leaves this machine except opt-in calls to a frontier LLM.

Target machine: Windows 11, Python 3.12+, `uv`, Ollama local. No WSL, no Docker.

---

## 1. Repository layout

```
applyr/
├── pyproject.toml               # uv-managed; ruff + mypy(strict) + pytest config here
├── alembic.ini
├── migrations/                  # Alembic, from day one (render_as_batch=True for SQLite)
│   └── versions/
├── src/applyr/
│   ├── __init__.py
│   ├── config.py                # ~/.applyr/config.toml loader, paths, SLA defaults
│   ├── core/                    # knows nothing about LLMs or MCP
│   │   ├── enums.py             # Status, EventType, EmailClass, ProposalStatus, ...
│   │   ├── models.py            # SQLModel tables (schema in §3)
│   │   ├── db.py                # engine/session factory; sqlite-vec extension loading
│   │   ├── events.py            # append_event(), derived_status(), days_in_stage()
│   │   ├── sla.py               # ghosted/stale derivation from config SLAs
│   │   ├── actions.py           # Action models (Pydantic discriminated union) — §5
│   │   ├── proposals.py         # propose → diff → confirm → commit pipeline — §5
│   │   ├── diff.py              # human-readable diff rendering for any Action
│   │   ├── analytics.py         # Phase 4 funnel / response-rate / time-to-apply queries
│   │   └── repos/               # one module per aggregate: companies, jobs, documents,
│   │       └── ...              #   applications, contacts, interviews, tasks, emails, proposals
│   ├── llm/
│   │   ├── provider.py          # LLMProvider protocol: chat(), chat_with_tools(), embed()
│   │   ├── ollama.py            # httpx against localhost:11434 (/api/chat, /api/embed)
│   │   ├── anthropic_provider.py# thin; used only when config routes a task to frontier
│   │   ├── tools.py             # single source of truth for tool schemas (§6) —
│   │   │                        #   consumed by BOTH the MCP server and Ollama tool-calling
│   │   ├── resolution.py        # deterministic entity resolution (§7). No LLM inside.
│   │   └── router.py            # task → provider routing (classify=local, jd_parse=configurable)
│   ├── ingest/
│   │   ├── jd_capture.py        # paste or plain httpx GET → markdown snapshot (§8)
│   │   ├── imap_poller.py       # Phase 3: read-only IMAP, UID checkpointing, keyring creds
│   │   ├── classifier.py        # Phase 3: local LLM → EmailClass + confidence
│   │   └── linker.py            # Phase 3: email → application (domain, thread, subject)
│   ├── mcp/
│   │   └── server.py            # FastMCP; registers tools from llm/tools.py verbatim
│   └── cli/
│       ├── main.py              # Typer app
│       └── commands/            # add, status, list, show, review, brief, doc, prep, say
└── tests/
    ├── core/                    # events, derived status, proposals, SLA, analytics
    ├── llm/                     # resolution (the big one), action validation, router
    └── ingest/                  # classifier prompt contract, linker heuristics (fixtures)
```

Key decision: **`llm/tools.py` is the one place tool schemas live.** The MCP server exposes
them to Claude Code; the Ollama provider passes the same schemas for local tool-calling
(`applyr say "..."`). Two front doors, one contract, one test suite.

Dependencies (runtime): `sqlmodel`, `alembic`, `pydantic>=2`, `fastmcp`, `typer`, `rich`,
`httpx`, `sqlite-vec`, `rapidfuzz`, `keyring`, `imap-tools`, `markdownify`, `pypdf`
(resume text extraction), `anthropic` as optional extra. Dev: `pytest`, `ruff`, `mypy`.

---

## 2. Architecture invariants

1. The LLM never writes to the DB. Every write is: LLM emits `Action` → Pydantic validation
   → entity resolution → `Proposal(pending)` → rendered diff → human confirm → commit +
   append event(s). Enforced structurally: repos expose writes only through
   `proposals.commit(proposal_id)`; tool handlers can only create proposals.
2. `application_events` is append-only. No UPDATE/DELETE on it, ever. Status,
   days-in-stage, funnel — all derived. Mistakes are fixed by appending a correcting
   `status_change` event (noted as a correction), not by editing history.
3. Resolution is deterministic code (`llm/resolution.py`), not model output. The model
   supplies strings ("Stripe"); code decides what they refer to, or refuses.
4. Timestamps stored as UTC ISO-8601; rendered local. The LLM converts relative dates
   ("yesterday") to absolute; a validator rejects dates in the future >1 day or before 2020.

---

## 3. Data model — your schema, plus four proposed additions

Tables exactly as specified: `companies`, `jobs`, `documents`, `applications` (no status
column), `application_events`, `contacts`, `interactions`, `interviews`, `tasks`, `emails`,
`proposals`. Status enum as given; terminal statuses = `accepted, rejected, withdrawn`.

Proposed additions — **flagging these per your rule, none are load-bearing if you say no:**

1. **`company_aliases (id, company_id, alias)`** — "Meta"/"Facebook", "Google"/"Alphabet",
   plus learned spellings. Entity resolution and email-domain linking both need this;
   stuffing aliases into `companies.notes` isn't queryable.
2. **`documents.extracted_text`** — plain text pulled at registration time (pypdf/plaintext).
   Needed in Phase 4 to answer "Kubernetes isn't evidenced in your active resume" and in
   Phase 5 for prep dossiers. Extracting from PDF on every query would be slow and flaky.
3. **`job_skills (id, job_id, skill, evidence)`** — Phase 4 skill-gap map needs extracted,
   normalized skill terms per JD with the JD sentence as evidence (honest framing, per
   your anti-goal). Populated by an LLM extraction pass, stored so it's aggregatable.
4. **`meta` key-value table** — schema/embedding bookkeeping: embedding model name and
   dimension, so we detect a model swap and know re-embedding is required.

Two semantic clarifications I'll implement unless you object:

- **`jobs.source` vs `applications.source`**: job.source = where the posting was *found*
  (linkedin, company_site, aggregator, referral-tip); application.source = the *channel of
  submission* (referral, direct, easy-apply, recruiter). Phase 4 response-rate-by-source
  uses `applications.source`.
- **Saved jobs are applications.** An application row is created when a job enters the
  pipeline, with a `saved` event and `applied_at = NULL`. `applied_at` is set when the
  `applied` event lands. This makes saved→applied conversion a real funnel stage instead
  of a special case. (Alternative — applications only exist once applied — loses that.)

JD snapshots: `jd_snapshot_path` stores the raw fetched HTML/text under
`~/.applyr/snapshots/<jd_hash>.html` (provenance); `jd_markdown` is the cleaned markdown
in the DB (what prep and embeddings use); `jd_hash` = sha256 of `jd_markdown`. Immutable
after capture — re-capturing an updated posting creates a new job version note, never an
overwrite.

Migrations: Alembic against `SQLModel.metadata`, `render_as_batch=True` (SQLite ALTER
limitations). sqlite-vec virtual tables are created idempotently at startup, outside
Alembic (virtual tables don't migrate).

---

## 4. Event sourcing & derived status

- `derived_status(app)` = `to_status` of the latest event ordered by
  `(occurred_at, id)` — insertion order never wins over occurred_at, so backfilling
  "actually, I applied last Tuesday" works.
- `days_in_stage(app)` = now − occurred_at of the latest status-changing event.
- **Transition policy: warn, don't forbid.** Real pipelines are messy (rejected → reopened
  happens). Unusual transitions (e.g. `rejected → interviewing`) render a warning in the
  proposal diff but commit if confirmed. Events record facts; they don't enforce workflow.
- **Ghosted / stale are queries, not states.**
  `last_activity(app)` = max(latest event, latest interaction, latest linked email).
  `ghosted` = current status in {applied, screening} AND now − last_activity > SLA[status].
  `stale` = same idea for any non-terminal status. Per-stage SLAs live in config:

  ```toml
  [sla_days]
  applied = 14
  screening = 10
  assessment = 7
  interviewing = 10
  final_round = 7
  offer = 5
  ```

Tests (Phase 1, the foundation): out-of-order event insertion; occurred_at ties broken by
id; days-in-stage across multiple transitions; ghosted flips exactly at SLA boundary;
correction events; empty-event application is invalid by construction (creation always
appends `saved` or `applied`).

---

## 5. Proposal pipeline

`Action` is a Pydantic discriminated union (`kind` field): `AddJob`, `LogApplication`,
`UpdateStatus`, `LogInteraction`, `LogInterview`, `LogDebrief`, `AddContact`, `AddNote` —
mirroring the write tools 1:1. A proposal may carry a **composite** action (e.g. `AddJob`
with an unknown company nests `AddCompany`); the diff shows every entity it will create.

Lifecycle:

```
propose(action)        → resolve refs (§7) → freeze RESOLVED ids into action_json
                         → render diff → INSERT proposals(status=pending) → show diff
confirm(proposal_id)   → re-validate against current DB (entity still exists?) → commit
                         in one transaction → append events → status=accepted
reject(proposal_id)    → status=rejected, nothing written
```

- Resolution happens at **propose** time and the resolved ids are frozen in `action_json`
  (with the original utterance kept for audit). Confirm re-checks referential integrity.
- Confirming twice is an error (no double-commit). Rejected proposals stay for audit.
- **Auto-approve** (`--yes` / config `[approvals] auto = [...]`): only `add_note` and
  `log_interaction` are eligible. `update_status`, `log_application`, and anything that
  creates a company/job/contact always confirms. Hard-coded floor, not just config.
- In the CLI, confirm is an interactive y/n on the rendered diff. Over MCP there is no
  TTY: write tools return the diff + proposal id, Claude shows it to you in chat, and your
  "yes" becomes a `confirm_proposal` call. Same pipeline, different confirmation surface.

Diff rendering (`core/diff.py`), e.g.:

```
Proposal #41 — update_status (source: email)
  Application: Stripe — Backend Engineer (app#12, currently: applied, 9d in stage)
  applied → screening        occurred_at: 2026-07-13 09:12 (from email received time)
  note: "Recruiter Jana Müller replied, wants a 30-min call"
```

---

## 6. Tool schemas (exact)

Every tool returns one envelope type, so both Claude and the local model handle results
uniformly:

```python
class ToolResult(BaseModel):
    result: Literal["ok", "proposal_created", "needs_disambiguation", "error"]
    data: dict | None = None            # payload for "ok"
    proposal: ProposalView | None = None    # id + rendered diff, for writes
    candidates: list[Candidate] | None = None  # for disambiguation, see §7
    message: str | None = None          # human-readable summary or error
```

Entity references (`*_ref` params) accept either an explicit id (`app#12`, `job#7`,
`co#3`, `contact#5`, `interview#2`) or free text ("Stripe", "the Stripe backend role") —
resolved per §7.

### Write tools (all → `proposal_created` or `needs_disambiguation`)

```python
add_job(
    company: str,                        # name or co#id; unknown → nested AddCompany in proposal
    title: str,
    jd_text: str | None = None,         # pasted JD (preferred)
    jd_url: str | None = None,          # plain GET fetch; no scraping frameworks
    url: str | None = None,             # posting URL for the record
    source: str | None = None,          # where found: linkedin|company_site|aggregator|other
    location: str | None = None,
    remote_policy: Literal["onsite", "hybrid", "remote"] | None = None,
    level: str | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
    currency: str | None = None,        # ISO-4217
    posted_at: date | None = None,
    save: bool = True,                  # also create the application row at `saved`
)

log_application(
    job_ref: str,
    applied_at: date | None = None,     # default today
    resume: str | None = None,          # document label, doc#id, or a file path —
                                        #   unregistered path → nested AddDocument (hashed)
    cover_letter: str | None = None,    # same semantics
    source: str | None = None,          # channel: referral|direct|easy_apply|recruiter
    referral_contact: str | None = None,
    priority: int | None = None,
)

update_status(
    application_ref: str,
    to_status: Literal["saved","applying","applied","screening","assessment",
                       "interviewing","final_round","offer","accepted",
                       "rejected","withdrawn"],
    occurred_at: datetime | None = None,   # default now; relative dates resolved by caller
    note: str | None = None,
)

log_interaction(
    summary: str,
    application_ref: str | None = None,    # at least one of application_ref/contact_ref
    contact_ref: str | None = None,
    channel: Literal["email","linkedin","phone","in_person","other"] = "email",
    direction: Literal["inbound","outbound"] = "inbound",
    occurred_at: datetime | None = None,
)

log_interview(
    application_ref: str,
    scheduled_at: datetime,
    round: int | None = None,              # default: previous round + 1
    format: Literal["phone","video","onsite","take_home"] = "video",
    interviewers: list[str] = [],          # names; matched to contacts opportunistically
)

log_debrief(
    interview_ref: str,                    # interview#id or "the Stripe interview yesterday"
    notes: str,
    questions_asked: list[str] = [],       # feeds the personal question bank
    outcome: Literal["advanced","rejected","pending","unknown"] = "pending",
)

add_contact(
    name: str,
    company_ref: str | None = None,
    title: str | None = None,
    email: str | None = None,
    linkedin: str | None = None,
    relationship: str | None = None,       # recruiter|referrer|interviewer|peer|other
    notes: str | None = None,
)

add_note(application_ref: str, text: str)   # auto-approve eligible
```

### Read tools (→ `ok`)

```python
search(
    query: str,
    scope: Literal["all","jobs","applications","contacts","emails","notes"] = "all",
    limit: int = 10,
)   # hybrid: exact/fuzzy on names+titles UNION semantic via sqlite-vec; typed hits
    # [{ref, type, headline, snippet, score}]

show(ref: str)          # full detail for any entity: application → job, company, events
                        # timeline, interviews, pinned docs, linked emails, tasks

get_briefing()          # {interviews_next_48h, followups_due, stale_past_sla,
                        #  pending_proposals} — the `applyr brief` screen as data

draft_followup(
    application_ref: str,
    kind: Literal["post_application_nudge","post_interview_thanks","check_in"] = "check_in",
)   # returns draft text + the context used. NEVER sends anything.
```

### Proposal management (the MCP confirmation surface)

```python
list_proposals(status: Literal["pending","accepted","rejected"] = "pending")
confirm_proposal(proposal_id: int)      # → "ok" with committed entity refs
reject_proposal(proposal_id: int, reason: str | None = None)
```

Additions beyond your list, flagged: `add_note`, `show`, and the three proposal tools.
The proposal tools are required for the confirm step to exist over MCP at all; `show` is
what makes "tell me about the Stripe application" a one-call answer.

---

## 7. Entity resolution strategy

The hard part, so here's the exact algorithm. All in `llm/resolution.py`, pure functions
over the DB, no LLM calls, fully unit-testable.

**Company resolution** (`resolve_company(text) -> Resolved | Ambiguous | NotFound`):

1. `co#id` form → direct lookup.
2. Normalize: casefold, strip punctuation and legal suffixes (Inc, GmbH, Ltd, LLC, AG, Co).
3. Exact match on normalized `companies.name` or `company_aliases.alias` → resolved (1.0).
4. Fuzzy: rapidfuzz `token_set_ratio` against names + aliases.
   - exactly one candidate ≥ 92 → resolved
   - anything in 70–91, or multiple ≥ 92 → **Ambiguous** with candidates
   - all < 70 → **NotFound** → tool returns a proposal that includes `AddCompany`
5. Never guess between two candidates, whatever the scores. "Meta" with both Meta and
   Metabase in the DB returns both.

**Application resolution** (`resolve_application(text) -> ...`) — "I heard back from Stripe":

1. `app#id` form → direct lookup.
2. Resolve the company (above). Ambiguous/NotFound propagates immediately.
3. Candidates = that company's non-archived applications.
4. Narrow by remaining tokens ("backend", "staff", "the Berlin one") matched against job
   title, level, and location.
5. After narrowing: exactly one candidate → resolved. Exactly one **non-terminal**
   candidate among several (rest rejected/withdrawn) → resolved to it, and the diff notes
   the assumption. Otherwise → **Ambiguous**, candidates ranked by last_activity.
6. Zero candidates but company exists → NotFound with a hint ("no applications at Stripe;
   3 saved jobs exist — did you mean one of those?").

**Disambiguation contract**: `needs_disambiguation` results carry
`candidates: [{ref, label, status, last_activity}]`. The caller (Claude or local model)
shows them to me and re-invokes the tool with the chosen explicit ref. The model never
picks silently.

**Contact and interview resolution**: same shape. Interviews additionally resolve by
recency phrases handled at the caller level ("yesterday's interview" → the caller passes
the date; code matches scheduled_at within that day; multiple → disambiguate).

**Test matrix (written before the resolver, Phase 2):** exact hit; case/suffix noise
("Stripe, Inc."); alias hit (Facebook→Meta); one-typo fuzzy ("Strpie"); two close companies
→ ambiguous; unknown company → create proposal; one company, two active applications →
ambiguous; one active + one rejected → resolves to active; token narrowing ("stripe
backend" vs two Stripe roles); archived-only applications → NotFound-with-hint.

---

## 8. Supporting decisions

**LLM provider abstraction** (`llm/provider.py`): a `Protocol` with `chat(messages) -> str`,
`chat_with_tools(messages, tools) -> ToolCall | str`, `embed(texts) -> list[vector]`.
Ollama implements all three via httpx (`/api/chat` with `tools=`, `/api/embed`).
`router.py` maps task → provider from config: `classification=local`,
`jd_parse=local|anthropic`, `prep=local|anthropic`. Anthropic use is explicit opt-in
config — the default config ships fully local.

**Embeddings & search**: one sqlite-vec virtual table `vec_items(embedding, item_type,
item_id, chunk_ix)` covering JD markdown, notes, and debriefs (email bodies added in
Phase 3). Default embedding model `nomic-embed-text` (768-dim) — open question below.
Model name + dim recorded in `meta`; on mismatch, refuse to search and offer `applyr
reindex`. Search = SQL exact/fuzzy UNION vector KNN, merged with a simple score.

**JD capture**: paste is primary. URL mode = single plain `httpx` GET with a desktop UA;
`markdownify` to markdown; raw response archived to the snapshot path. If a site returns
a bot-wall, say so and ask for a paste — no retries, no headless browser (anti-goal).

**Email ingestion (Phase 3)**: read-only IMAP via `imap-tools`; per-folder UID checkpoint
in `meta`; never marks read, never moves, never deletes, never sends. Credentials in
Windows Credential Manager via `keyring` (`applyr email setup` prompts once). Your
address is Gmail, so: IMAP + app password, folder default `INBOX`. Classifier runs
locally, always. Linking: sender domain → `companies.domain`/aliases → application; then
`In-Reply-To`/`References` thread matching against previously linked emails; then subject
heuristics. Classifications ≥ confidence threshold become pending `update_status` /
`log_interaction` proposals (source=`email`) in `applyr review`; below threshold they
sit unlinked and are shown in review too. **Nothing auto-commits, ever** — batch-approve
is one keystroke per proposal or `a` for approve-all-high-confidence.

**Analytics definitions (Phase 4)** — so the numbers mean something:
- Stage conversion: of applications that ever reached stage *i*, fraction that ever
  reached stage *i+1*; median days between the two events.
- Response rate: employer reacted at all (any inbound event/interaction incl. rejection).
  Positive-response rate: progressed past `applied`. Reported per resume version
  (`resume_document_id`), per `applications.source`, and per time-to-apply bucket
  (`applied_at − posted_at`: 0–1d, 2–3d, 4–7d, 8–14d, 15d+).
- Skill gap: `job_skills` aggregated over non-archived applications → "term X appears in
  N% of targeted JDs and is not evidenced in the active resume's extracted text."
  Framed as evidence, not an ATS score.

**CLI surface (Phase 1)**: `applyr add company|job|contact|doc`, `applyr apply`,
`applyr status <ref> <to_status>`, `applyr list [--status --stale --ghosted]`,
`applyr show <ref>`, `applyr events <ref>`. Phase 2 adds `applyr say "..."` (NL →
local LLM → proposal) and `applyr mcp` (serve). Phase 3 adds `applyr email
setup|poll|review`. Phase 4 adds `applyr brief`, `applyr stats`, `applyr skills`.
Phase 5 adds `applyr prep <ref>`, `applyr debrief <ref>`.

---

## 9. Phase deliverables & test focus

| Phase | Ships | Tests that matter |
|---|---|---|
| 1 | Schema + migrations, repos, event sourcing, derived status, SLA, proposals pipeline (manual actions), full CLI | events/derived-status matrix (§4), proposal lifecycle (double-confirm, reject-writes-nothing, auto-approve floor), SLA boundaries |
| 2 | Provider protocol + Ollama, tool registry, resolution, `say`, MCP server | resolution matrix (§7), action validation edge cases, disambiguation round-trip, tool envelope contract |
| 3 | IMAP poller, classifier, linker, review queue, keyring | linker heuristics on fixture emails, classifier output-contract (valid JSON, calibrated threshold), UID checkpoint resume, no-write-to-mailbox guarantee |
| 4 | analytics.py, brief, skills extraction + gap map | funnel math on synthetic event streams, response-rate attribution to resume version/source/bucket, gap map against a fixture resume |
| 5 | prep dossier, debrief flow, question bank | dossier assembles all five inputs; debrief persists to question bank |

Each phase ends: `pytest` green, `ruff` clean, `mypy --strict` clean, README updated,
one-paragraph report to you. (`mypy --strict` + SQLModel has known friction; if I need a
targeted override it'll be module-scoped, in `pyproject.toml`, and flagged in the report.)

---

## 10. Open questions (blocking Phase 2, not Phase 1)

1. **Ollama models** — your spec left `<fill in>`: which chat model should the local
   provider default to? It must do tool calling reliably; `qwen3:8b` or `llama3.1:8b` are
   the realistic candidates if you have ~8 GB VRAM. And for embeddings: `nomic-embed-text`
   (my default) or `mxbai-embed-large`?
2. **Saved-jobs modeling** — OK with saved jobs being applications with a `saved` event
   (§3)? It's the cleaner funnel; say no and I'll keep saved jobs application-less.
3. **Schema additions** — `company_aliases`, `documents.extracted_text`, `job_skills`,
   `meta` (§3): any objections?
4. **Gmail assumption** for Phase 3 (IMAP + app password on your Gmail account) — correct?
5. **Currency default** for comp fields — EUR, or leave always-explicit?
