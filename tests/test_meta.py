"""Tests for MetaStore."""

from __future__ import annotations

from code_loops.meta import MetaStore


def test_init_task_creates_yaml(tmp_path):
    meta = MetaStore(tmp_path / "meta.yaml")
    meta.init_task("0001_test", mode="feature")
    assert (tmp_path / "meta.yaml").exists()
    assert meta.data["task_id"] == "0001_test"
    assert meta.data["mode"] == "feature"
    assert meta.data["status"] == "in_progress"
    assert meta.data["stages"] == {}


def test_stage_lifecycle(tmp_path):
    meta = MetaStore(tmp_path / "meta.yaml")
    meta.init_task("0001_test", mode="feature")
    meta.stage_started("prd")
    assert meta.data["current_stage"] == "prd"
    assert meta.data["stages"]["prd"]["status"] == "running"
    assert meta.data["stages"]["prd"]["attempts"] == 1

    meta.stage_completed("prd", cost_usd=0.04, duration_s=12.3)
    assert meta.data["stages"]["prd"]["status"] == "done"
    assert meta.data["stages"]["prd"]["cost_usd"] == 0.04
    assert meta.data["stages"]["prd"]["duration_s"] == 12.3
    assert meta.data["cost_usd"] == 0.04


def test_round_trip_persists(tmp_path):
    meta1 = MetaStore(tmp_path / "meta.yaml")
    meta1.init_task("0001_test", mode="feature")
    meta1.stage_started("prd")
    meta1.stage_completed("prd", cost_usd=0.04)

    meta2 = MetaStore(tmp_path / "meta.yaml")
    assert meta2.is_stage_done("prd")
    assert meta2.data["task_id"] == "0001_test"


def test_stage_failed_records_reason(tmp_path):
    meta = MetaStore(tmp_path / "meta.yaml")
    meta.init_task("0001_test", mode="feature")
    meta.stage_started("prd")
    meta.stage_failed("prd", "claude exited non-zero")
    assert meta.data["stages"]["prd"]["status"] == "failed"
    assert meta.data["stages"]["prd"]["failure_reason"] == "claude exited non-zero"


def test_stage_attempts_increment(tmp_path):
    meta = MetaStore(tmp_path / "meta.yaml")
    meta.init_task("0001_test", mode="feature")
    meta.stage_started("prd")
    meta.stage_failed("prd", "fail 1")
    meta.stage_started("prd")
    assert meta.data["stages"]["prd"]["attempts"] == 2
