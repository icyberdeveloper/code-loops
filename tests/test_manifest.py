"""Tests for Manifest + ArtifactWriter (Phase 1 of Step 9.40).

Covers schema/lifecycle/scoped-write contracts. Stage handler refactors
in Phase 2 will exercise the same helpers in real pipeline flow.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_loops.artifact_writer import ArtifactWriter
from code_loops.manifest import SCHEMA_VERSION, Manifest


def _new_manifest(tmp_path: Path) -> Manifest:
    m = Manifest(tmp_path / "manifest.json")
    m.init_task("0001_test", mode="feature")
    return m


# ---- Manifest lifecycle ----


def test_init_task_writes_schema(tmp_path):
    m = _new_manifest(tmp_path)
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert saved["schema_version"] == SCHEMA_VERSION
    assert saved["task_id"] == "0001_test"
    assert saved["status"] == "in_progress"
    assert saved["total_cost_usd"] == 0.0
    assert saved["stages"] == {}


def test_stage_lifecycle(tmp_path):
    m = _new_manifest(tmp_path)
    m.stage_started("prd")
    assert m.is_stage_done("prd") is False
    m.stage_completed("prd", cost_usd=0.5, duration_s=10)
    assert m.is_stage_done("prd") is True
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert saved["stages"]["prd"]["cost_usd"] == 0.5
    assert saved["total_cost_usd"] == 0.5


def test_total_cost_accumulates_across_stages(tmp_path):
    m = _new_manifest(tmp_path)
    m.stage_started("prd")
    m.stage_completed("prd", cost_usd=0.5)
    m.stage_started("research")
    m.stage_completed("research", cost_usd=2.3)
    assert m.data["total_cost_usd"] == 2.8


def test_reset_stage_preserves_attempts_count(tmp_path):
    m = _new_manifest(tmp_path)
    m.stage_started("design")
    m.stage_entry("design")["attempts_count"] = 5
    m.stage_completed("design", cost_usd=10)
    m.reset_stage("design")
    assert m.data["stages"]["design"] == {"attempts_count": 5}


# ---- Pass-level (redesign loops) ----


def test_record_pass_appends_and_counts(tmp_path):
    m = _new_manifest(tmp_path)
    m.record_pass(
        "design",
        1,
        cost_usd=15.15,
        rounds=5,
        converged=True,
        final_artifact="design/pass_1/final.md",
    )
    m.record_pass(
        "design",
        2,
        cost_usd=18.19,
        rounds=5,
        converged=False,
        max_rounds_reached=True,
        final_artifact="design/pass_2/final.md",
    )
    st = m.data["stages"]["design"]
    assert st["passes_count"] == 2
    assert st["passes"][0]["pass_n"] == 1
    assert st["passes"][0]["converged"] is True
    assert st["passes"][1]["max_rounds_reached"] is True


def test_record_pass_with_review_verdict(tmp_path):
    m = _new_manifest(tmp_path)
    m.record_pass(
        "design_review",
        1,
        cost_usd=3.29,
        verdict="redesign_needed",
        theme="domain_field_presence_drift",
    )
    p = m.data["stages"]["design_review"]["passes"][0]
    assert p["verdict"] == "redesign_needed"
    assert p["theme"] == "domain_field_presence_drift"


# ---- Attempt-level ----


def test_record_attempt_with_failure_reason(tmp_path):
    m = _new_manifest(tmp_path)
    m.record_attempt(
        "impl_plan",
        1,
        outcome="schema_failed",
        reason="subtasks[0].id must be snake_case",
    )
    m.record_attempt("impl_plan", 2, outcome="ok", cost_usd=0.5)
    st = m.data["stages"]["impl_plan"]
    assert st["attempts_count"] == 2
    assert st["attempts"][0]["outcome"] == "schema_failed"
    assert st["attempts"][0]["reason"].startswith("subtasks[0]")
    assert st["attempts"][1]["outcome"] == "ok"


# ---- Subtask attempt-level (implementation) ----


def test_record_subtask_attempt(tmp_path):
    m = _new_manifest(tmp_path)
    m.record_subtask_attempt(
        "00_baseline",
        attempt_n=1,
        roles={
            "test_writer": {"cost_usd": 0.2, "outcome": "ok"},
            "coder": {"cost_usd": 0.4, "outcome": "fail-test"},
            "triage": {"action": "retry-coder"},
        },
    )
    m.record_subtask_attempt(
        "00_baseline",
        attempt_n=2,
        roles={"coder": {"cost_usd": 0.35, "outcome": "ok"}, "reviewer": {"verdict": "approved"}},
    )
    m.set_subtask_final("00_baseline", outcome="shipped", files_changed=["src/foo.py"])
    sub = m.data["stages"]["implementation"]["subtasks"]["00_baseline"]
    assert sub["attempts_count"] == 2
    assert sub["outcome"] == "shipped"
    assert sub["files_changed"] == ["src/foo.py"]


# ---- Latest pointer ----


def test_set_latest(tmp_path):
    m = _new_manifest(tmp_path)
    m.set_latest("design", "design/final.md")
    assert m.data["stages"]["design"]["latest"] == "design/final.md"


# ---- Persistence (load existing) ----


def test_manifest_round_trips(tmp_path):
    m = _new_manifest(tmp_path)
    m.stage_started("prd")
    m.stage_completed("prd", cost_usd=0.5)
    m.record_pass("design", 1, cost_usd=15.15, converged=True)
    m2 = Manifest(tmp_path / "manifest.json")
    assert m2.data["task_id"] == "0001_test"
    assert m2.is_stage_done("prd") is True
    assert m2.data["stages"]["design"]["passes_count"] == 1


def test_manifest_load_corrupt_file_starts_fresh(tmp_path):
    (tmp_path / "manifest.json").write_text("not valid json {{{")
    m = Manifest(tmp_path / "manifest.json")
    assert m.data == {}


# ---- ArtifactWriter ----


def test_write_simple_writes_and_records_latest(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    p = aw.write_simple("prd", "prd.md", "# PRD")
    assert p == tmp_path / "prd" / "prd.md"
    assert p.read_text() == "# PRD"
    assert m.data["stages"]["prd"]["latest"] == "prd/prd.md"


def test_write_attempt_scopes_and_copies_latest(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    p1 = aw.write_attempt("impl_plan", 1, "subtasks.yaml", "v1")
    assert p1 == tmp_path / "impl_plan" / "attempts" / "attempt_1" / "subtasks.yaml"
    assert (tmp_path / "impl_plan" / "subtasks.yaml").read_text() == "v1"
    # second attempt overwrites latest, prior attempt preserved
    aw.write_attempt("impl_plan", 2, "subtasks.yaml", "v2")
    assert (tmp_path / "impl_plan" / "attempts" / "attempt_1" / "subtasks.yaml").read_text() == "v1"
    assert (tmp_path / "impl_plan" / "attempts" / "attempt_2" / "subtasks.yaml").read_text() == "v2"
    assert (tmp_path / "impl_plan" / "subtasks.yaml").read_text() == "v2"


def test_write_pass_scopes_and_copies_latest(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    aw.write_pass("design", 1, "final.md", "RFC v1")
    aw.write_pass("design", 2, "final.md", "RFC v2 (after redesign)")
    assert (tmp_path / "design" / "pass_1" / "final.md").read_text() == "RFC v1"
    assert (tmp_path / "design" / "pass_2" / "final.md").read_text() == "RFC v2 (after redesign)"
    assert (tmp_path / "design" / "final.md").read_text() == "RFC v2 (after redesign)"


def test_write_round_no_latest_copy(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    aw.write_round("design", pass_n=1, round_n=2, name="critic_safety.md", content="x")
    assert (tmp_path / "design" / "pass_1" / "round_2" / "critic_safety.md").read_text() == "x"
    # No latest copy at design/critic_safety.md
    assert not (tmp_path / "design" / "critic_safety.md").exists()


def test_write_subtask_attempt_scopes_correctly(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    p = aw.write_subtask_attempt("01_foo", attempt_n=2, role="coder", content="diff")
    assert (
        p
        == tmp_path
        / "implementation"
        / "subtasks"
        / "01_foo"
        / "attempts"
        / "attempt_2"
        / "coder.md"
    )


def test_append_log_concatenates(tmp_path):
    m = _new_manifest(tmp_path)
    aw = ArtifactWriter(tmp_path, m)
    aw.append_log("design/pass_1/debate.md", "Round 1 — perspectives", "concerns A B C")
    aw.append_log("design/pass_1/debate.md", "Round 1 — facilitator", "continue")
    body = (tmp_path / "design" / "pass_1" / "debate.md").read_text()
    assert "Round 1 — perspectives" in body
    assert "Round 1 — facilitator" in body
    assert body.index("perspectives") < body.index("facilitator")
