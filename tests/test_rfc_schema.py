"""Тесты для rfc_schema.build_rfc_schema — динамический enum file_path."""

from __future__ import annotations

from code_loops.rfc_schema import build_rfc_schema


def test_no_evidence_returns_schema_without_enum():
    schema = build_rfc_schema(None)
    file_path_props = schema["properties"]["file_changes"]["items"]["properties"]["path"]
    assert "enum" not in file_path_props, "no evidence — no enum constraint"
    assert file_path_props["type"] == "string"


def test_evidence_with_files_adds_enum():
    evidence = {
        "verified_files": [
            "app/core/validator/spelling_check.py",
            "app/domain/person.py",
        ],
        "verified_symbols": [],
    }
    schema = build_rfc_schema(evidence)
    enum = schema["properties"]["file_changes"]["items"]["properties"]["path"]["enum"]
    assert enum == [
        "app/core/validator/spelling_check.py",
        "app/domain/person.py",
    ]


def test_evidence_with_empty_files_no_enum():
    """Пустой verified_files → не накладываем enum (иначе ничего не сможет
    быть указано в file_changes)."""
    evidence = {"verified_files": [], "verified_symbols": []}
    schema = build_rfc_schema(evidence)
    file_path_props = schema["properties"]["file_changes"]["items"]["properties"]["path"]
    assert "enum" not in file_path_props


def test_evidence_with_non_list_verified_files_no_enum():
    """Защита от malformed evidence — если verified_files не список,
    игнорируем (не падаем, не накладываем enum)."""
    evidence = {"verified_files": "not a list", "verified_symbols": []}
    schema = build_rfc_schema(evidence)
    file_path_props = schema["properties"]["file_changes"]["items"]["properties"]["path"]
    assert "enum" not in file_path_props


def test_new_files_proposed_never_has_enum():
    """new_files_proposed по дизайну без enum (новые файлы не могут быть
    в evidence — их ещё не существует)."""
    evidence = {"verified_files": ["a.py", "b.py"], "verified_symbols": []}
    schema = build_rfc_schema(evidence)
    new_files_props = schema["properties"]["new_files_proposed"]["items"]["properties"]["path"]
    assert "enum" not in new_files_props


def test_schema_required_fields():
    """Required поля схемы соответствуют RFC structure (shapes, context,
    proposed_approach, file_changes, new_files_proposed, tests, risks, rollback)."""
    schema = build_rfc_schema(None)
    required = set(schema["required"])
    assert {
        "title",
        "shapes_considered",
        "context",
        "proposed_approach",
        "file_changes",
        "new_files_proposed",
        "tests",
        "risks",
        "rollback",
    } <= required


def test_shapes_considered_axis1_letters_enum():
    """Axis 1 letter enum — A/B/C/D/E (как в software-architect.md Phase 1)."""
    schema = build_rfc_schema(None)
    axis1_letter_enum = schema["properties"]["shapes_considered"]["properties"]["axis1_options"][
        "items"
    ]["properties"]["letter"]["enum"]
    assert set(axis1_letter_enum) == {"A", "B", "C", "D", "E"}


def test_shapes_considered_axis2_letters_enum():
    """Axis 2 letter enum — F/G/H/I."""
    schema = build_rfc_schema(None)
    axis2_letter_enum = schema["properties"]["shapes_considered"]["properties"]["axis2_options"][
        "items"
    ]["properties"]["letter"]["enum"]
    assert set(axis2_letter_enum) == {"F", "G", "H", "I"}


def test_build_schema_does_not_mutate_input_evidence():
    """build_rfc_schema не должна мутировать переданный evidence dict."""
    evidence = {"verified_files": ["a.py", "b.py"], "verified_symbols": []}
    evidence_snapshot = dict(evidence)
    build_rfc_schema(evidence)
    assert evidence == evidence_snapshot


def test_build_schema_returns_independent_objects():
    """Два вызова с разными evidence не должны делить enum список."""
    schema_a = build_rfc_schema({"verified_files": ["a.py"]})
    schema_b = build_rfc_schema({"verified_files": ["b.py"]})
    enum_a = schema_a["properties"]["file_changes"]["items"]["properties"]["path"]["enum"]
    enum_b = schema_b["properties"]["file_changes"]["items"]["properties"]["path"]["enum"]
    assert enum_a == ["a.py"]
    assert enum_b == ["b.py"]
