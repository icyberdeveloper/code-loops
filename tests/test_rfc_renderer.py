"""Тесты для rendering.rfc_renderer.render_rfc — JSON → markdown."""

from __future__ import annotations

from code_loops.rendering.rfc_renderer import render_rfc


def _minimal_rfc() -> dict:
    return {
        "title": "Test RFC",
        "shapes_considered": {
            "axis1_options": [
                {"letter": "A", "description": "patch at symptom", "verdict": "rejected"},
                {"letter": "C", "description": "downstream filter", "verdict": "SELECTED"},
            ],
            "axis2_options": [
                {"letter": "F", "description": "predicate", "verdict": "SELECTED"},
            ],
            "chosen": "C × F",
            "rationale": "Minimal surface, structurally closes the bug class.",
        },
        "context": "Bug X happens in flow Y.",
        "proposed_approach": "Add filter at C.",
        "file_changes": [
            {
                "path": "app/foo.py",
                "modification": "Add filter step",
                "rationale": "closes bypass surface",
            }
        ],
        "new_files_proposed": [],
        "tests": [
            {"name": "test_filter_drops_stub", "description": "stub filtered", "kind": "unit"}
        ],
        "risks": [
            {
                "title": "Risk X",
                "description": "may break Y",
                "mitigation": "covered by test Z",
                "severity": "low",
            }
        ],
        "rollback": "single git revert",
    }


def test_render_includes_title():
    md = render_rfc(_minimal_rfc())
    assert "# RFC: Test RFC" in md


def test_render_includes_phase_1_marker():
    md = render_rfc(_minimal_rfc())
    assert "## Shapes considered" in md
    assert "## End of Phase 1 — proceed to Phase 2" in md


def test_render_axis_letters_listed():
    md = render_rfc(_minimal_rfc())
    assert "**A.**" in md
    assert "**C.**" in md
    assert "**F.**" in md


def test_render_chosen_shape():
    md = render_rfc(_minimal_rfc())
    assert "Cross-axis chosen shape: C × F" in md


def test_render_rationale():
    md = render_rfc(_minimal_rfc())
    assert "Minimal surface, structurally closes the bug class." in md


def test_render_context():
    md = render_rfc(_minimal_rfc())
    assert "## Context" in md
    assert "Bug X happens in flow Y." in md


def test_render_file_changes_paths():
    md = render_rfc(_minimal_rfc())
    assert "`app/foo.py`" in md
    assert "Add filter step" in md
    assert "closes bypass surface" in md


def test_render_new_files_marked():
    rfc = _minimal_rfc()
    rfc["new_files_proposed"] = [
        {"path": "app/new_module.py", "purpose": "new helper", "key_exports": "do_x"}
    ]
    md = render_rfc(rfc)
    assert "`app/new_module.py` **(new)**" in md
    assert "new helper" in md


def test_render_tests_with_kind():
    md = render_rfc(_minimal_rfc())
    assert "test_filter_drops_stub" in md
    assert "*(unit)*" in md


def test_render_risks_with_severity():
    md = render_rfc(_minimal_rfc())
    assert "**Risk X** [low]" in md
    assert "Mitigation: covered by test Z" in md


def test_render_skips_empty_sections():
    """Пустые optional секции не должны попасть в markdown."""
    rfc = _minimal_rfc()
    # eval_design / alternatives_considered / decision_log / open_questions
    # / revision_notes отсутствуют — не должны рендериться
    md = render_rfc(rfc)
    assert "## Eval design" not in md
    assert "## Alternatives considered" not in md
    assert "## Decision log" not in md
    assert "## Open questions" not in md
    assert "## Revision notes" not in md


def test_render_revision_notes_when_present():
    rfc = _minimal_rfc()
    rfc["revision_notes"] = "Round 1: addressed concern X."
    md = render_rfc(rfc)
    assert "## Revision notes" in md
    assert "Round 1: addressed concern X." in md


def test_render_postmortem_constraint():
    rfc = _minimal_rfc()
    rfc["shapes_considered"]["postmortem_constraint"] = "Layer A rejected per 9-lesson signal."
    md = render_rfc(rfc)
    assert "Postmortem-mode constraint" in md
    assert "Layer A rejected per 9-lesson signal." in md


def test_render_revision_constraint():
    rfc = _minimal_rfc()
    rfc["shapes_considered"]["revision_constraint"] = "Pass_2 uses Axis 1 = C, prior was B."
    md = render_rfc(rfc)
    assert "Revision-mode constraint" in md
    assert "Pass_2 uses Axis 1 = C" in md


def test_render_handles_minimal_inputs_gracefully():
    """Пустые / отсутствующие поля не должны падать."""
    rfc = {
        "title": "",
        "shapes_considered": {},
        "context": "",
        "proposed_approach": "",
        "file_changes": [],
        "new_files_proposed": [],
        "tests": [],
        "risks": [],
        "rollback": "",
    }
    md = render_rfc(rfc)
    assert "# RFC: untitled" in md
    assert "## Shapes considered" in md
