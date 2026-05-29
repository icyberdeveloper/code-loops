# Research: data

## Q5: Person schema, live maturity distribution, and existing `entity_type` / `is_stub` markers

**Finding:** `Person` has **no** `is_stub`, `entity_type`, `is_canonical` field — grep across
`app/` returns zero matches. All 760 records are JSON blobs in `documents WHERE
collection='people'`; only `author_id, title, category, status` are indexed columns. No
`facts_count` is persisted — both `extracted_facts` and `plaud_appearances` are arrays inside
the blob, counted via `len()` at read. A maturity property already exists in code at
`app/domain/person.py:370-384` (`Person.confidence_level`: `🟢/🟡/🔴` from
`appearances + facts*0.5`, thresholds `>=10` / `>=4`) — but it's a `@property`, invisible to SQL.

Live distribution (snapshot 2026-05-22 08:25 UTC, `assistant-data/assistant.db`, 27 MB):

| Bucket | Count | % |
|---|---|---|
| Full stubs (0 facts AND 0 plaud_apps) | **687 / 760** | **90.4%** |
| 1–9 facts | 36 | 4.7% |
| 10–49 facts | 24 | 3.2% |
| 50+ facts (real canonical set) | **13** | **1.7%** |
| 0 plaud_appearances | 725 | 95.4% |
| `relationship` set | 36 | 4.7% |
| `archetype` set | 34 | 4.5% |
| `track_attention=True` | 4 | 0.5% |

Stubs outnumber 50+-fact canonicals **53×** → every fuzzy lookup has ~90% chance of landing
on a stub. Postmortem-cited stubs verified 0/0/None/None (`09844776 Вадим Месилов`, `49da54e7
Александр Лебедев`, `ead9fdb5 Марина Ляшенко`, `1ab42518 Мария Жаколкина`, `07d4bb93
Управление…аналитики_все`); canonicals verified — `669f9e69 Roman Lebed` 274/23/direct_report,
`859ab4f9 Igor Maslov` 79/2, `746d4f0b Arina Sergunina` 64/7, `3bffd24f Dmitriy Kulikov` 77/11,
`dac0d739 Anton Kryukov` 85/10.

**Schema / code refs:** `app/domain/person.py:289-336` (Person dataclass, ~40 fields, no
type/stub field); `:370-384` (`confidence_level` already-coded maturity signal);
`app/infra/storage/sqlite_store.py:65-87` (`documents` schema + indexes, no person-type index).

**Live data refs:** `/home/neondelph/assistant-data/assistant.db` (27 MB): `people` 760,
notes 239, events 197, tasks 126, lessons 190 (97 architecture / 63 rule / 28 correction / 2
context), extraction_state 225. `chroma/` 215 MB; `graph_state.json` 3.5 MB.
`app/core/validator/spelling_check.py` **mtime 2026-04-30 12:49 UTC** = 22 days untouched.

**Migration / backfill needed:** **None required.** Maturity computable from existing blob at
validator time. Optional additive `facts_count INTEGER` column (NULL-able) backfillable over
760 rows in seconds, no LLM cost. Hard `is_stub` / `entity_type` discriminator is additive but
needs a classifier (e.g. `_все`-suffix → mailing-list) — recommend at `person_handlers` write
boundary, not retroactive. **Gaps:** none.

## Q6: Validator audit / decision-log table — does one exist, and where should it live?

**Finding:** **No** validator audit table exists. Grep for `validator_decisions`,
`validator_audit`, `spelling_audit`, `validation_log` → zero matches. Today's persistence:
(1) **journalctl log lines only** — lossy, not queryable;
(2) **`lessons` collection** (190 rows) is **human-curated guidance**, not pipeline audit —
LLM-judge already loads all 169–190 lessons into its system prompt; adding 15–30 decision
rows/day would dilute judge context (**wrong home**);
(3) **`extraction_state`** (225 rows, 99% success) is the closest per-decision precedent but
lives as a `documents` collection keyed by `note_id` — good for idempotency, weak for operator
queries (no `(word, correct)` index, JSON-scan only).

Four precedent sibling stores already share `SqliteStore.connection` + `write_lock` with
relational tables — the pattern that satisfies the success criterion:

| Sibling | DDL location |
|---|---|
| `MergeAuditStore` | `app/infra/storage/merge_audit_store.py:130-249` (4 tables) |
| `AlertStore` | `app/infra/storage/alert_store.py` (`alert_history`) |
| `BenchClassifierStore` | `app/infra/storage/bench_classifier_store.py` |
| `DeepRadarStore` | `app/infra/storage/deep_radar_store.py` |

All share the shape: constructor `(conn, lock)`, `_setup` with `CREATE TABLE IF NOT EXISTS`,
sync + async wrappers, 180-day retention sweep. **`MergeAuditStore` is the closest semantic
match** — "merger proposed / applied / confidence / snapshot to roll back" maps 1:1 onto
"validator proposed / applied / judge verdict".

**Recommendation:** new `ValidatorAuditStore` sibling on shared connection. Suggested
columns: `(id INTEGER PK, recorded_at TEXT, caller TEXT, word TEXT, correct TEXT, distance
INTEGER, person_id TEXT, target_facts INTEGER, target_apps INTEGER, judge_severity TEXT,
judge_verdict TEXT, applied INTEGER, reason TEXT)`. Witness queries justifying indexes:
- `idx_recorded_at` — weekly digest "decisions last 7d".
- `idx_word_correct` — operator drill-down "all `Лебедь→Лебедев` decisions".
- `idx_caller` — telemetry "rejection rate per caller".
- `idx_person_id` — "all decisions targeting this stub" before bulk-deleting a problem stub.

**Schema / code refs:**
- `app/infra/storage/merge_audit_store.py:93-251` — sibling-store pattern (lock injection,
  `_setup`, async wrappers, `cleanup_old_history` at line 407).
- `app/infra/storage/sqlite_store.py:46-54` — `SqliteStore.connection` + `.write_lock`
  wiring point.
- `app/core/validator/runner.py` — `run_claude_with_validation(caller=...)` is the natural
  audit-write call site (already receives `caller`).

**Live data refs:**
- `assistant.db` already hosts 7 audit/log tables (`merge_history`, `merge_blacklist`,
  `alias_change_history`, `canonical_history`, `alert_history`, `bench_classifier_results`,
  `deep_radar_results`). An 8th adds ~0 op cost at ≤15 decisions/day (postmortem §9.5).

**Migration / backfill needed:** None. `CREATE TABLE IF NOT EXISTS` in `_setup` bootstraps.
Forward-only — history recoverable only from journalctl (lossy). **Gaps:** none — pattern,
wiring point, and DDL all directly cribbable from `MergeAuditStore`.

## Data impact summary
- **Schema changes:** new sibling table `validator_decisions` on shared `SqliteStore` conn. No
  changes to `documents` / `people`; no new column on Person.
- **Backfill needed:** none. Optional `facts_count` precompute over 760 rows feasible
  (~seconds, no LLM) but unnecessary if validator computes at lookup time.
- **Migration script:** none — `CREATE TABLE IF NOT EXISTS` in sibling's `_setup`
  (pattern: `merge_audit_store.py:130`).
- **Lifecycle change:** 180-day retention sweep mirroring `cleanup_old_history` (line 407).

## Data-related risks
1. **Stub-pollution grows linearly with calendar sync** — 90.4% stub ratio (687/760);
   postmortem §9.7 shows +220/+24/+2/day. Maturity filter must use low threshold (`facts ≥ 1
   OR apps ≥ 1`) — strict ≥50 excludes `Nikita Kurganov` (5 facts, real victim). Witness:
   Maslov 79 (in), Kurganov 5 (out under strict cutoff).
2. **JSON-blob scan cost** — `json.loads` × 760 ≈ <1 s per call. Acceptable. Escape hatch:
   additive `facts_count INTEGER` column maintained on `person_handlers` writes
   (~tens of UPDATEs/day).
3. **Audit write contention** — `SqliteStore.write_lock` already serialises 7 tables. At ≤15
   decisions/day (§9.5) contention <1 ms. Risk emerges only if validator is invoked per
   Telegram message; current `run_claude_with_validation` routing keeps it bounded.
