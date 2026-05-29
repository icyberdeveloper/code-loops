# Research: codebase

## Q1: Trace artifact generation → `check_spelling` → LLM judge → `MessageSender`; confirm single validator entry; find where `caller=` is stamped.

**Finding:** All auto-generated artifact paths funnel through exactly one helper — `run_claude_with_validation` — which in turn calls `runner.run_claude` then `validator.validate`. Each caller stamps its own `caller=` string as a kwarg at the call site (no inheritance, no central registry). The validated text is then delivered exclusively via `OutboxQueue.put(OutboxMessage(...))`. The four callers in the postmortem are confirmed; there are in fact **seven** call sites in production code (postmortem missed three coaching callers).

Sequence inside the chokepoint: `runner.py:68` calls `runner.run_claude(...)` → on non-empty response `runner.py:83` calls `validator.validate(response=..., caller=caller, ...)` → `validator.py:160` builds `EntityExtractor` (populates `_people_cache`) → `validator.py:163-177` runs **Step 1.5** `check_spelling(response, self._people_cache)` deterministically → `validator.py:213` calls `runner.run_lightweight` for Opus judge → `validator.py:231-252` merges spelling fixes (priority over LLM fixes) → `runner.py:89-95` returns `result.fixed_response` to the artifact caller, which immediately enqueues to outbox.

**References:**
- `app/core/validator/runner.py:29-105` — single entry `run_claude_with_validation(runner, *, message, system_prompt, validator, caller, tg_id, ...)`. Best-effort: if validator crashes the original response passes through (line 102-105).
- `app/core/validator/validator.py:113-265` — `ResponseValidator.validate(..., *, caller="unknown")`. `caller` is keyword-only (line 120). Logged 8× in `journalctl` as `Validator caller=%s ...` (lines 171, 180, 198, 221, 256, 262, 267, 273).
- `app/core/validator/validator.py:163-177` — Step 1.5 calls `check_spelling`, builds `spelling_issues`, applies `apply_fixes` for `spelling_fixed_response`.
- `app/core/validator/validator.py:231-252` — merges spelling fixes into LLM result; sets `result.valid=False` even if LLM said valid; **spelling fixes always win** (`base = result.fixed_response or spelling_fixed_response or response; result.fixed_response = apply_fixes(base, spelling_issues)`).
- `app/core/validator/spelling_check.py:166-223` — `check_spelling(response, people)` — pure function; the bug lives at line 131-133 (multi-word filter).
- Caller→`caller=` map (where the string is stamped):
  - `app/core/intelligence/meeting_briefing.py:145-154` — `caller="meeting_briefing"`. Delivery: `_format_and_send` → `app/core/intelligence/meeting_briefing.py:713` `self._outbox.put(OutboxMessage(type="text", ...))`.
  - `app/core/plaud/card_generator.py:81-90` — `caller="plaud_card"`. NB the postmortem says "`meeting_card.py`": that file (`app/core/plaud/meeting_card.py:22-29`) is a re-export façade — actual call lives in `card_generator.py`. Delivery: `_deliver_card_and_approvals` → `app/core/plaud/card_generator.py:263` outbox.put.
  - `app/core/coaching/_play.py:179-191` — `caller="deputy_play"`. Delivery: `app/core/coaching/_play.py:198` outbox.put.
  - `app/core/digest/digest_service.py:261-270` — `caller=f"digest_{label}"` where `label ∈ {"morning","evening","weekly"}` (see `generate(tg_id, label)` at `digest_service.py:302-308`). Delivery: `app/core/digest/digest_service.py:397` outbox.put.
  - Three additional callers (postmortem does not cover): `app/core/coaching/competing_commitments.py:421-429` (`caller="competing_commitments"`), `app/core/coaching/decision_journal.py:356-364` (`caller="decision_journal_analysis"`), `app/core/coaching/_retro.py:185-193` (`caller="deputy_retro"`).
- `app/core/validator/runner.py:79` — `validator=None` short-circuits validation (graceful degradation when bootstrap forgot to wire). Means a caller's text reaches outbox unvalidated if `validator=None`.

**Existing primitives:** `app/core/validator/runner.py:run_claude_with_validation` is the established chokepoint — no need to introduce a new one. `app/core/outbox.OutboxQueue` is the single Telegram delivery path. For caller-string consistency, a `Literal[...]` type alias or an enum in `app/core/validator/runner.py` would centralise the seven known caller names without changing topology.

**Gaps:** No caller registry / enum — typos in `caller=` are silently accepted (logged as-is into journalctl). `digest_weekly` not in postmortem regression table but reaches same code path; absence in journalctl may reflect rarity of capitalised proper nouns in those outputs, not absence of risk.

---

## Q2: Sources populating `Person.aliases`; multi-word vs single-word distribution; how many real contacts would be re-admitted if the `" " not in alias` filter were removed.

**Finding:** `Person.aliases` is a **flat `list[str]` with no provenance metadata** (`app/domain/person.py:294`). It is NOT the structured `AliasObservation` from `app/domain/extraction.py:152-178` — those live in `GraphState.entity_meta`, a separate store. `Person.aliases` is written from **four** code paths, three of which produce predominantly multi-word entries and one (LLM action) that produces single-word entries. Because of `migrate_primary_to_latin.py` (sets `name` to Latin and pushes the Cyrillic form into `aliases`), **the Cyrillic canonical of essentially every real contact lives only as a multi-word alias** — i.e. it is filtered out of `_collect_surnames`.

Distribution snapshot (postmortem §10.9, 13.05 SQL): 749 persons total, 676 stubs (90.3%), ~73 canonical. The canonical sample shown in §4.3, §7.5, §8.4, §10.2 — Roman Lebed, Igor Maslov, Arina Sergunina, Nikita Kurganov, Mikhail (Miша) Ivanov, Anton Kryukov, Dmitriy Kulikov — **100% have Cyrillic alias multi-word, name Latin multi-word** (`name='Roman Lebed', aliases=['Роман Лебедь', 'Lebed Roman']` pattern). The single canonical the postmortem shows that *has* a single-word alias is Roman Lebed (via Latin parts of `name`), which is why `'Lebed'` ends up in the index but `'Лебедь'` does not. (Live DB sandbox copy at `/home/neondelph/assistant/data/assistant.db` is empty — could not run a fresh `python sqlite3` count here; relying on postmortem SQL outputs.)

**References:**
- `app/domain/person.py:294` — `aliases: list[str] = field(default_factory=list)`. No category/source field.
- `app/domain/extraction.py:85-95` (`VALID_ALIAS_SOURCES`) and `app/domain/extraction.py:152-178` (`AliasObservation`) — the typed provenance schema exists but lives in `GraphState.entity_meta`, **not** wired into `Person.aliases`.
- Writers of `Person.aliases`:
  1. `app/core/actions/person_handlers.py:130-135` — LLM `create_person`/`update_person` action: `existing_aliases.update(new_aliases); d["aliases"] = sorted(existing_aliases)`. Prompt example (`app/prompts/llm_extraction.md:79`) shows short single-word values `["Рома", "Lebed"]`. **Source-class:** single-word, runtime, LLM-driven.
  2. `app/core/scheduling/calendar_sync.py:160-165` — iOS auto-create stub: `Person(... name=name, aliases=[name])` where `name` is a multi-word Latin calendar participant ("Седа Курбанова", "Александр Лебедев"). Gate: `" " not in name_stripped: continue` at line 148 forces multi-word. **Source-class:** multi-word, runtime, stub-generator.
  3. `scripts/migrate_aliases.py:159-179` (`generate_base_aliases`) — adds multi-word title-cased Latin transliteration `"Artem Merets"` + reversed `"Merets Artem"`; lines 210-219 also add matched multi-word calendar participant strings. **Source-class:** multi-word, one-shot migration.
  4. `scripts/migrate_primary_to_latin.py:1-32` — flips primary `name` to Latin; pushes old Cyrillic full name → `aliases` (multi-word like `'Роман Лебедь'`). **Source-class:** multi-word, one-shot migration.
- `_collect_surnames` filter at issue: `app/core/validator/spelling_check.py:131-133` — `if alias and len(alias) >= _MIN_LEN and " " not in alias: candidates.append(alias)`. `mtime` 2026-04-30 12:49 UTC, unchanged 22 days.
- Postmortem SQL evidence (canonical multi-word Cyrillic + multi-word reversed Latin): §4.3 (Lebed/Maslov/Sergunina), §7.5 (Kurganov/Ivanov), §8.4 (Kulikov/Kryukov), §10.2 (Sergunina again).

**Re-admission estimate (if `" " not in alias` is dropped and aliases are `.split()` like `name`):**
- Per canonical contact: re-admits **both Cyrillic name parts** (first name + surname, e.g. `'Роман'`, `'Лебедь'`) and adds nothing new for the reversed Latin alias `'Lebed Roman'` (already covered by name parts).
- Scaling to the postmortem snapshot (~73 canonical contacts where all show the `name=Latin / aliases=[Cyrillic, Lat-reversed]` shape): **~100% of canonical contacts gain Cyrillic-surname coverage** they currently lack. ~146 new (firstname, surname) Cyrillic tokens enter the index. Exact count requires live DB; sandbox copy is empty.
- Side-effect: stubs that today produce indexed single-word tokens (`'Месилов'`, `'Лебедев'`, `'Курбанова'`, `'Жаколкина'`) **remain indexed unchanged** — their `name` is multi-word and the existing `.split()` on `name` (line 124-130) already pushes both Cyrillic parts in. Removing the alias filter therefore **levels the playing field** (canonical regains the exact-match shortcut at `spelling_check.py:190-191`: `if word_lower in surname_lower_set: continue`) but does **not** by itself stop stub-pollution-driven inversion when the LLM writes a less-common but still-canonical surname.

**Existing primitives:**
- `app/domain/extraction.AliasObservation` (with `source ∈ {l1_extraction, dict_person, dict_product, dict_team, dict_abbrev, merge_absorbed, legacy}` and `category ∈ {verified, inferred, shared, unknown}`) already gives structured provenance — but is wired to `entity_meta`, not `Person.aliases`. Could be reused if `Person.aliases` is refactored to typed form.
- `app/core/merger/alias_categorizer.py` already runs a same-type guard, `shared`-alias detection, and an owners-index (`build_alias_owners_index`) over entities — same semantics that would catch Bug C (`'Аналитик'` as alias of a рассылка). Pure functions; testable.
- `app/core/extraction/known_entities.py:70-90` (`collect_known_persons`) builds an authoritative per-tg_id `{canonical_name: [aliases]}` map from Person store; already cached (60s TTL). Provides the exact data shape `_collect_surnames` needs.
- `app/core/name_resolver.resolve_name` (referenced from `calendar_sync.py:117, 151`) — existing single source of truth for matching a free-form name to a Person; uses `aliases` plus `name`. Could be inverted to provide canonical-only set for the spell-check index.
- Standard library: `unicodedata` (for cross-script normalisation, Bug B), `re.IGNORECASE` (already in `apply_fixes`); no third-party transliteration lib used in `spelling_check.py` — but `app/core/transliterate.cyr_to_lat` exists and is used by `migrate_aliases.py:18,101`.

**Gaps:** No `is_stub` / `entity_type` field on `Person` (postmortem §2.3, §7.4) — there is no in-domain signal to filter out рассылки like `'Управление…_все'` from `_collect_surnames`. The `facts_count` / `plaud_appearances` count is available on every Person (`extracted_facts`, `plaud_appearances` fields, `app/domain/person.py:320,322`) but is not consulted by `_collect_surnames`.

---

## Files impact summary
- **Modify:** `app/core/validator/spelling_check.py` — `_collect_surnames` (drop `" " not in alias`, split multi-word aliases on whitespace; optionally filter by Person-quality signal); possibly `check_spelling` to accept a confidence/quality input.
- **Modify (less invasive option):** `app/core/validator/validator.py:163-177` — apply do-no-harm guard *after* `check_spelling` returns but *before* `apply_fixes` runs (Layer C).
- **Modify (data-shape fix):** `app/domain/person.py:289` — add `is_stub: bool | None = None` (or derived `@property`), populated by `app/core/scheduling/calendar_sync.py:_auto_create_people`.
- **Modify:** `app/core/scheduling/calendar_sync.py:160-165` — when auto-creating from calendar participant, set `is_stub=True` or never push the full participant into `aliases` if it equals `name`.
- **Create:** `app/core/validator/spelling_index.py` — pure builder that returns `{surname_lower: [(person_id, quality_score)]}` consumed by `check_spelling`; gives a clean test boundary.
- **Delete:** none — no dead code identified.
- **Test files affected:** `tests/unit/test_validator_spelling.py` — currently 0 hits for `lebed`/`Лебед` (per postmortem §11.2); add: (a) `Roman Lebed + alias 'Роман Лебедь'` → "Лебедь" passes (b) stub `'Александр Лебедев'` does NOT outrank canonical multi-word alias (c) cross-script: cyrillic "Арина" finds latin-name canonical "Arina Sergunina". `tests/unit/test_validator.py` and `tests/unit/test_validator_regression.py` may need updated mocks if `Person` schema changes.

## Pre-implementation reading (top 3)
1. `app/core/validator/spelling_check.py:109-140` (`_collect_surnames`) and `:166-223` (`check_spelling`) — the deterministic part you'll touch; understand `_MIN_LEN`, `_MIN_SHARED_AFFIX`, anchor logic, `_is_inflection` first.
2. `app/core/validator/validator.py:158-265` — the merge between deterministic pre-check and LLM judge; note that spelling fixes override LLM fixes (line 248, 252) and survive LLM exceptions (line 271-291). Any Layer C / D / E change interacts here.
3. `app/core/scheduling/calendar_sync.py:108-173` (`_auto_create_people`) and `scripts/migrate_primary_to_latin.py:1-100` — the two writers that produced today's stub-pollution pattern (`name=Latin / aliases=[Cyrillic_multiword]` for canonical, `name=Cyrillic_multiword / aliases=[name]` for stubs). Understand these before tampering with `Person.aliases` schema.

## Behavioral signal flow (palette of candidate fix locations)

For the `surname_spelling` inversion bug class, the fix could land at any of the following layers. Listing as a palette, not a recommendation:

- **Layer A: at symptom location** — patch `_collect_surnames` (`spelling_check.py:109-140`) to (i) split multi-word aliases like name, and/or (ii) score-rank candidates so stubs lose ties to canonical. Pros: one file, no schema change. Cons: every new bypass route (PERSON entity sneaking in via a different writer) needs another tweak; postmortem §11 shows 14 days of variant patches predicted by this layer.
- **Layer B: upstream of symptom** — at write-time of `Person.aliases`. Either (i) `calendar_sync._auto_create_people:160-165` stops pushing the full participant string when it equals `name` (no-op-alias suppression), or (ii) `Person` gains an `is_stub` / `entity_type` field set at creation. Pros: closes the false-candidate class at source; `_collect_surnames` becomes trivially safe. Cons: requires a backfill for the existing 676 stubs.
- **Layer C: downstream of symptom but inside validator** — between `check_spelling` returning `SpellingIssue`s and `apply_fixes` running. Insert a per-issue guard that compares `facts_count(target_person_id)` to `facts_count(best_alternative_for_word)` and drops the fix if `target` is a 0-fact stub. Pros: one chokepoint in `validator.py:163-177`; can't be bypassed by future writer variants. Cons: requires reading Person quality signal at validate-time (already in `_people_cache`).
- **Layer D: outside the pipeline / post-pipeline** — wrap `apply_fixes` in a "do-no-harm" diff filter: after substitution, re-run `check_spelling` on the fixed text; if it produces new issues *or* if any substitution lowers a name-anchor score, revert that substitution. Pros: structural — bug class becomes invariant-checkable. Cons: doubles deterministic cost; subtle interaction with LLM-emitted `fixed_response`.
- **Layer E: change the data, not the code** — backfill `is_stub=True` on the 676 stub Person rows; relocate non-person entities (`Управление…_все`, переговорки, `_все` distribution lists) out of `documents.collection='people'` into a separate collection. Pros: removes Bug C's preconditions; collapses pollution from 90.7% to single-digit %. Cons: data migration scope, follow-up coordination with `iOS Shortcuts` calendar sync to prevent re-population.

All five layers are viable for this bug class. Layer E is the only one that also fixes the orthogonal Bug C (рассылки in `people` collection); the others are silent on it.
