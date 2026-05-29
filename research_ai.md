# Research: ai

## Q7: Eval coverage for the validator + minimal replay harness for false-correction rate

**Finding.** Coverage today is shallow-happy-path unit-test only — **none of the 8 inversion classes from §1–§10 are tested**, no `pytest.mark.eval` validator suite, no baseline JSON, no persisted before/after capture, no replay harness. The existing test file `tests/unit/test_validator_spelling.py` ships 24 cases (340 LoC, mtime `2026-04-30 09:42 UTC` — same week as `spelling_check.py`'s `mtime 2026-04-30 12:49:35 UTC`, confirmed by `stat`). All positive cases use **single-word `name` persons** (`_make_person("Антон Митрикас")`) — Bug A's exact gap. The lone alias test (`test_alias_correct_passes`, line 150-155) uses Latin `aliases=["Mitrikas"]` (single-word, `" " not in alias` true), so even the alias branch never exercises the multi-word `'Игорь Маслов'` shape the production index actually stores. Zero tests cover: multi-word cyrillic aliases (Bug A), stub-pollution with `facts=0` competitors (Bug B), non-person entities in the people collection (Bug C), cross-script Latin-name → Cyrillic-token resolution (Bug B' from §10.3), instrumental-case personal names (`Дмитрием`/`Лебедем` §8.2/§9.2), ordinary capitalized Russian words (`Серия`/`Защита`/`Аналитик`/`Вчера` §9.6/§10.6), or Latin ASR artefacts (`Krykov`/`Dmitry` §8.2). The lesson 08.05 smoke-test (`check_spelling('Роман Лебедь обсуждает с Лебедев', [Roman Lebed alias='Роман Лебедь', Aleksandr Лебедев]) == []`) is missing — `grep -n "Лебед\|lebed"` in the test file returns 0 matches. The `eval` marker is registered in `pyproject.toml:36-39` (`markers = ["eval: opt-in RAG quality evaluation..."]`, `addopts = "-m 'not eval'"`) and used **only** by `tests/integration/test_rag_quality.py` (L983 and L1042); no validator eval. The closest schema analog for a validator baseline is `tests/integration/rag_baseline.json` (188 lines: `recorded_at`, aggregate metrics block, per-question `{hit, rank, top_score}` dict). The current production capture surface is **journalctl only**: **114 `found N surname misspelling(s)` warning lines over 14 days (30.04 → 13.05)**, parseable via the stable format `Validator caller=<X> found <N> surname misspelling(s): word→correct (d=N); …` (`validator.py:170-177`). No DB row, no JSONL, no diff snapshot. **Conclusion:** the replay harness that closes the gap must (a) seed a golden corpus from journalctl + manual `is_inversion` labels, (b) replay through `check_spelling` only (no Opus judge — §5.1/§10.4 prove the judge rubber-stamps everything pre-check predicts, so a judge-replay measures nothing new), (c) write a `spelling_baseline.json` shaped after `rag_baseline.json`, (d) gate via `@pytest.mark.eval` so default `pytest` stays fast.

**Harness shape (15–20 LoC python core):**
```python
# tests/integration/test_validator_spelling_eval.py
@pytest.mark.eval
def test_spelling_false_correction_rate():
    corpus = load_jsonl("tests/fixtures/spelling_replay.jsonl")  # 14d journalctl + labels
    people = load_people_snapshot("tests/fixtures/people_2026_05_13.jsonl")
    baseline = json.load(open("tests/integration/spelling_baseline.json"))
    inverted = 0; missed = 0; total_flags = 0
    for entry in corpus:                  # entry: response, expected_real_typos, expected_no_flag
        issues = check_spelling(entry["response"], people)
        flags = {(i.word, i.correct) for i in issues}
        inverted += len(flags & entry["expected_no_flag"])   # canonical → stub flips
        missed  += len(entry["expected_real_typos"] - flags) # real Митрикас-style typos lost
        total_flags += len(flags)
    fcr = inverted / max(total_flags, 1)
    assert fcr <= baseline["false_correction_rate"] + 0.02  # 2% headroom
    assert missed <= baseline["missed_real_typos"]
```

**Code refs.**
- `app/core/validator/spelling_check.py:166` — `check_spelling(response, people) -> list[SpellingIssue]`: pure, zero-LLM, deterministic. Replay primary input.
- `app/core/validator/spelling_check.py:226` — `apply_fixes(response, issues) -> str`: pure regex `\b…\b`.
- `app/core/validator/spelling_check.py:109-140` — `_collect_surnames`: Bug A locus (L131-133, `" " not in alias`).
- `app/core/validator/validator.py:163-177` — Step 1.5 wiring + log emission (`Validator caller=X found N surname misspelling(s)`). **This is the replay seed line.**
- `app/core/validator/validator.py:231-252` — Step 4.5 merge; replay must NOT cross this boundary (judge isn't worth replaying).
- `app/core/validator/runner.py:82-95` — all 8 callers route here (`meeting_briefing`, `digest_{morning,evening,weekly}`, `deputy_play`, `deputy_retro`, `decision_journal_analysis`, `competing_commitments`, `plaud_card`).
- `tests/unit/test_validator_spelling.py:24-28` — `_make_person`: must extend with `facts_count` + `plaud_appearances` to model stub-pollution.
- `tests/unit/test_validator_spelling.py:35-89` — `TestCatchesTypos` (6 single-word fixtures, all the coverage we have).

**Eval / metrics refs.**
- `tests/integration/rag_baseline.json` — schema model: `{recorded_at, corpus_size, metrics:{...}, per_question:{...}}` — mirror as `spelling_baseline.json` with `metrics:{false_correction_rate, missed_real_typo_rate, per_caller_breakdown:{meeting_briefing: 0.X, plaud_card: 0.Y, deputy_play: …}}`.
- `tests/integration/test_rag_quality.py:983,1042` — `@pytest.mark.eval` pattern + `--rag-baseline=save` flag-driven refresh; mirror as `--spelling-baseline=save`.
- `pyproject.toml:36-39` — eval marker registration; no change needed, just reuse.
- `journalctl -u assistant.service | grep "surname misspelling"` — **114 events 30.04→13.05** (currently the **only** ground-truth source). Sample: `2026-05-13 06:00:36 [WARNING] … Validator caller=digest_morning found 3 surname misspelling(s): Лебедь→Лебедев (d=2); Арина→Марина (d=1); Лебедем→Лебедев (d=1)`.
- (none) — no validator `eval` test, no baseline JSON, no replay harness. **Regression bench is missing entirely.**

**Cost / latency impact.** Replay harness is **\$0 / call**: `check_spelling` is pure Levenshtein over a ~750-entry index (<50 ms for the full 114-event corpus on dev laptop, single-threaded). Loading the people snapshot is one local SQLite read (~10 ms). Adding a `validator_replay_log` JSONL append at `validator.py:177` (one row per pre-check finding: `caller, word, correct, person_id, distance, facts_count_target, facts_count_source, response_excerpt[400]`) costs <1 ms per validator call, ~50 rows/day at 12.05–13.05 cadence, ~50 KB/yr compressed. Contrast with the production judge it audits: 13.05 journalctl shows **41 validator runs / 17 with surname findings**, output 2,048–7,252 tokens (P50 ≈ 3,400, P95 ≈ 6,200), context ~26,775–28,150 prompt tokens (`Validator context caller=… ~28150 tokens`), wall 28.6–81.3 s (P50 ≈ 45 s, P95 ≈ 78 s). At Opus 4.7 pricing (\$15/MTok in, \$75/MTok out): ≈ **\$0.67 uncached / \$0.35 with cached prefix** per call → **\$14–27/day → \$5–10K/yr** — every dollar of which is currently spent rubber-stamping pre-check findings the harness is designed to grade.

**Gaps.**
1. **No ground-truth labels.** 114 journalctl events have no `is_inversion: true/false` annotation. Bootstrapping the baseline = one human pass (~30 min) labelling the 11 dominant inversion pairs from §9.5+§10.5 (Лебедь, Маслов, Арина, Курганов, Иванов, Серия→Мария, Защита→защиты, Аналитик→аналитики, Krykov, Артём, Дмитрием — ~28/30 events fall in this cluster).
2. **Person-snapshot drift.** Replaying against today's index (749 persons / 676 stubs) is more pessimistic than the 30.04 state (~500 / ~430). Either accept this as a regression floor (today's index, today's inversion rate) or capture daily `people_snapshot.{date}.jsonl` (~700 rows × 14 days × ~500 bytes = 5 MB total) for historically-faithful replay.
3. **True-correction rate (recall) is unmeasured.** Without a positive set (the 5 originals — Митрикас, Кощиенко, Крюков, Гадарь, Мерец — plus any real typos harvested from journalctl marked `is_real_typo:true` like 13.05 06:01 `Меретц→Мерец`), a "more conservative" fix could silently zero out the inversion rate AND lose real catches. Baseline must include both `false_correction_rate` (precision) **and** `missed_real_typo_rate` (recall) — see harness code above.
4. **Per-caller breakdown is missing from current logs.** §8.6 shows 3 code paths affected (`meeting_briefing`, `plaud_card`, `deputy_play`); harness should bucket FCR per caller to expose which auto-pipeline regresses fastest.
5. **Cache-read instrumentation absent.** `claude_runner.py` `run_lightweight` does not log `cache_read_input_tokens`, so the cached-prefix cost estimate above is bounded only by tier pricing — actual savings are unmeasurable until the runner is extended. Pre-requisite for any "downgrade judge to Sonnet or gate by pre-check confidence" RFC arm.

---

## AI impact summary

- **New LLM calls introduced:** **zero**. Harness replays `check_spelling` + `apply_fixes` (pure functions). The Opus judge is skipped because §5.1/§6/§7.6/§10.4 prove it confirms every pre-check finding (0-of-30+ rejections on canonical→stub flips), so judge-replay would only re-measure a known-zero precision.
- **Validator coverage:** all 8 caller paths share one `ResponseValidator` routed through `run_claude_with_validation` (`runner.py:82`). Harness covers all of them in one shot because `check_spelling` is caller-agnostic — replay `(response_text, people_index)` pairs reproduce the bug independently of which auto-pipeline emitted the text.
- **Eval gap:** **no validator eval today.** `tests/unit/test_validator_spelling.py` is unit-scope (mock store, no real index, no journalctl replay) and covers only the 5 original happy-path typos. The 8 regression classes from §1–§10 are invisible to CI — that is why CI stayed green while 30+ user-visible inversions shipped to Telegram over 14 days. Proposed: `tests/integration/test_validator_spelling_eval.py` + `tests/integration/spelling_baseline.json` + `tests/fixtures/spelling_replay.jsonl` (15–20 labelled pairs minimum, mirrors EDD-principle `pass^3 = 100%` for money-touching paths).
- **Cost projection at scale:** harness is **\$0/month at any volume** (no API call). The judge it audits, in contrast, scales linearly: at observed 41/day → **\$5–10K/yr**; at 10× volume → \$50–100K/yr. Harness is a precondition for any cost-reduction RFC (judge-tier downgrade, gated invocation) — without an FCR baseline, "we saved \$X" is unfalsifiable.
- **Tier recommendation:** harness itself is zero-tier. It **enables** two downstream tier moves: (a) downgrade the judge from Opus 4.7 to Sonnet 4.6 once FCR is measured (P95 28 s vs 78 s, ~5× cheaper) — re-run the same baseline on Sonnet, ship if FCR ≤ Opus's; (b) gate the judge entirely with `should_call_judge = pre_check_confidence < threshold` once enriched, halving call count. Both are out-of-scope for this Q but predicated on the harness landing first.
- **Cost-per-successful-output:** the production judge's empirical precision on canonical→stub flips is **0%** (postmortem §10.4: "Опус подтвердил все 3 «фикса»"; §7.5/§8.4/§9.2 same — 0-of-30+ rejections). Multiplying judge cost by `1 / precision` puts cost-per-correct-decision at **∞** on the inversion subset. Bare per-call price of \$0.67 is a misleading floor. Harness gives the metric needed to make this number finite and trackable.
- **Latency budget:** harness is offline (CI / pre-merge), no hot-path impact. Production validator P95 = 78 s (13.05 sample) but runs on `bg_warm_pool` semaphore=4 (project brief); 41 calls/day × 78 s = 3,200 s/day = <5% pool utilisation. No contention risk. Harness measures, does not introduce.

## AI-related risks

1. **Judge consensus theatre (HIGH).** §5.1, §6/§7.6, §10.4 all show Opus confirming every pre-check finding as `severity=high` despite having 167–169 lessons in context, including the exact `Арина не Марина` lesson 30.04. Without the harness's FCR metric, there is no signal the LLM-judge is non-functional as a gate — it is pure cost (\$5–10K/yr). Mitigation: harness MUST emit per-call diff (pre-check finding vs. ground-truth label) so future judge-prompt edits can be measured against this baseline rather than reasoned about.
2. **Eval-set contamination by stub drift (MEDIUM).** 114 events were generated against the people index as it stood on each event's date (stubs grew 430→676 in 14 days, §9.7/§10.9). Replaying all 114 against today's snapshot will produce a higher FCR than was historically observed. Accept as regression floor OR add daily `people_snapshot.{date}.jsonl` (~5 MB total) for date-aligned replay.
3. **Positive-set absence hides over-correction regression (MEDIUM).** A naïve fix ("skip any candidate with `facts=0`") would zero out FCR AND silently drop the real typo `Меретц→Мерец` (13.05 06:01, person `83225fa9` Артём Мерец, 96 facts) because pre-check would no longer consider Мерец if any adjacent stub Мерец-shaped person had `facts=0`. Baseline must carry both `false_correction_rate` (precision) and `missed_real_typo_rate` (recall) — single-metric harness is half-blind.
