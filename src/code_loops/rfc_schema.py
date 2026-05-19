"""JSON Schema для RFC + динамическое построение из evidence.

Phase 2 (compose) использует схему здесь для constrained decoding —
Anthropic CLI с --json-schema делает grammar-constrained sampling на
уровне токенов, модель физически не может эмиттить значения вне схемы.

Ключевая защита от галлюцинаций: file_changes[].path имеет enum из
evidence.verified_files (всё что Phase 1 реально нашла). Архитектор
физически не может сослаться на файл который не был верифицирован.

Новые файлы (которых ещё нет в кодовой базе) живут в отдельной секции
new_files_proposed без enum constraint — Phase 1 о них не знает, и
такие файлы по определению не могут быть верифицированы (их не
существует). Это honest архитектурное решение: enum закрывает фабрикацию
ссылок на existing code, не пытается закрыть проектирование нового кода.
"""

from __future__ import annotations

from typing import Any

# Базовая RFC схема. Большая часть полей — free-form prose strings
# (Context, Rationale, Decision log) потому что constrained decoding не
# имеет смысла для естественного языка. Constraint только там где
# фабрикация возможна и опасна — file paths.
_BASE_RFC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "shapes_considered",
        "context",
        "proposed_approach",
        "file_changes",
        "new_files_proposed",
        "tests",
        "risks",
        "rollback",
    ],
    "properties": {
        "title": {
            "type": "string",
            "description": "Короткий заголовок RFC (1 строка).",
        },
        "shapes_considered": {
            "type": "object",
            "description": (
                "Phase 1 из software-architect.md — обязательно перед "
                "RFC body. Enumerate shapes по двум осям, выбери одну, "
                "обоснуй."
            ),
            "required": ["axis1_options", "axis2_options", "chosen", "rationale"],
            "properties": {
                "axis1_options": {
                    "type": "array",
                    "description": "Опции Оси 1 (WHERE the fix lives).",
                    "items": {
                        "type": "object",
                        "required": ["letter", "description", "verdict"],
                        "properties": {
                            "letter": {
                                "type": "string",
                                "enum": ["A", "B", "C", "D", "E"],
                            },
                            "description": {"type": "string"},
                            "verdict": {
                                "type": "string",
                                "description": "1-line trade-off + selected/rejected + reason",
                            },
                        },
                    },
                },
                "axis2_options": {
                    "type": "array",
                    "description": "Опции Оси 2 (WHAT KIND of intervention).",
                    "items": {
                        "type": "object",
                        "required": ["letter", "description", "verdict"],
                        "properties": {
                            "letter": {
                                "type": "string",
                                "enum": ["F", "G", "H", "I"],
                            },
                            "description": {"type": "string"},
                            "verdict": {"type": "string"},
                        },
                    },
                },
                "chosen": {
                    "type": "string",
                    "description": "Выбранная комбинация, например 'C × F'.",
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "3-5 предложений: почему именно эта комбинация, "
                        "что жертвуется, что приобретается. Включить "
                        "explicit acknowledgement какой Axis-1 candidate "
                        "был отвергнут и почему."
                    ),
                },
                "postmortem_constraint": {
                    "type": "string",
                    "description": (
                        "Если задача из постмортема — обоснование почему "
                        "НЕ Layer A (symptom site). Пусто для feature mode."
                    ),
                },
                "revision_constraint": {
                    "type": "string",
                    "description": (
                        "В redesign-mode — обоснование почему выбранный "
                        "Axis-1 letter отличается от предыдущего pass."
                    ),
                },
            },
        },
        "context": {
            "type": "string",
            "description": "1-3 paragraphs про ситуацию и constraints.",
        },
        "proposed_approach": {
            "type": "string",
            "description": (
                "Algorithmic shape, why it's chosen. Свободный текст. "
                "Когда упоминаешь конкретные файлы/функции — они должны быть "
                "из evidence (file_changes секция enforce это структурно)."
            ),
        },
        "file_changes": {
            "type": "array",
            "description": (
                "Список изменений в существующих файлах. path обязан "
                "быть из evidence.verified_files (enum enforced)."
            ),
            "items": {
                "type": "object",
                "required": ["path", "modification", "rationale"],
                "properties": {
                    "path": {
                        "type": "string",
                        # enum заполняется dynamically в build_rfc_schema
                    },
                    "modification": {
                        "type": "string",
                        "description": "Что именно меняется (1-3 sentences).",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Почему это изменение нужно.",
                    },
                    "side_effects": {
                        "type": "string",
                        "description": (
                            "Если изменение вносит side effect (DB write, "
                            "outbox push, LLM call, file write, external API "
                            "call), описать. Пусто для pure refactor."
                        ),
                    },
                },
            },
        },
        "new_files_proposed": {
            "type": "array",
            "description": (
                "Новые файлы которые предлагается создать. Без enum "
                "constraint — таких файлов по определению нет в evidence."
            ),
            "items": {
                "type": "object",
                "required": ["path", "purpose"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Предлагаемый путь нового файла, относительно "
                            "base_repo. Snake_case, расширение .py для Python."
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "description": "Назначение файла (1-3 sentences).",
                    },
                    "key_exports": {
                        "type": "string",
                        "description": "Что публично экспортируется.",
                    },
                },
            },
        },
        "tests": {
            "type": "array",
            "description": "Тесты которые нужно добавить/изменить.",
            "items": {
                "type": "object",
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["unit", "integration", "e2e", "eval", "regression"],
                    },
                },
            },
        },
        "eval_design": {
            "type": "string",
            "description": (
                "Когда задача затрагивает AI/LLM surface — раздел про eval. Пусто иначе."
            ),
        },
        "alternatives_considered": {
            "type": "string",
            "description": "Альтернативы которые были рассмотрены и отвергнуты.",
        },
        "decision_log": {
            "type": "string",
            "description": "Hard trade-offs которые были сделаны и почему.",
        },
        "risks": {
            "type": "array",
            "description": "Риски и mitigation.",
            "items": {
                "type": "object",
                "required": ["title", "description"],
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "mitigation": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
            },
        },
        "open_questions": {
            "type": "string",
            "description": "Что осталось открытым / требует follow-up.",
        },
        "rollback": {
            "type": "string",
            "description": "Как откатить если ship'нули и пошло не так.",
        },
        "revision_notes": {
            "type": "string",
            "description": (
                "В revision mode — что изменилось vs предыдущий draft. Пусто в initial draft."
            ),
        },
    },
}


def build_rfc_schema(evidence: dict | None) -> dict[str, Any]:
    """Построить RFC schema с динамическим enum из evidence.verified_files.

    Когда evidence is None (тесты, legacy callers, evidence stage не
    запускался) — возвращает схему без enum constraint. file_path
    остаётся свободной строкой; constrained decoding защищает только
    shape, не paths. Backward compatible.

    Когда evidence is dict — file_changes[].path получает enum constraint
    из verified_files. Архитектор физически не может вписать path не
    из evidence.
    """
    import copy

    schema = copy.deepcopy(_BASE_RFC_SCHEMA)

    if evidence is None:
        return schema

    verified_files = evidence.get("verified_files", [])
    if not isinstance(verified_files, list) or not verified_files:
        # Evidence есть, но verified_files пуст — не накладываем enum
        # (иначе ничего не сможет быть указано в file_changes). Это
        # сигнал что Phase 1 ничего не нашла; Phase 2 будет работать
        # через new_files_proposed.
        return schema

    # Применяем enum к file_changes[].path
    schema["properties"]["file_changes"]["items"]["properties"]["path"]["enum"] = verified_files
    return schema


def schema_size_estimate(schema: dict) -> int:
    """Примерная оценка размера скомпилированной grammar (для лимитов).

    Anthropic docs предупреждают: enum со многими элементами и сложные
    anyOf/oneOf могут упереться в 'Schema is too complex for compilation'
    400 error. Эта функция даёт быструю оценку чтобы caller мог решить
    нужен ли fallback (e.g. на post-hoc validation вместо enum).
    """
    import json

    return len(json.dumps(schema))
