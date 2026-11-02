from __future__ import annotations

import sys
from pathlib import Path


MINERU_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MINERU_SOURCE))

import app as mineru_app  # noqa: E402


def identity(form_items: list[tuple[str, str]]) -> tuple[dict, str]:
    return mineru_app._config_from_form(form_items)


def test_equivalent_boolean_and_enum_spellings_share_identity() -> None:
    verbose = [
        ("backend", " VLM-TRANSFORMERS "),
        ("effort", " HIGH "),
        ("lang_list", " CH "),
        ("parse_method", " AUTO "),
        ("formula_enable", "True"),
        ("table_enable", "1"),
        ("image_analysis", " YES "),
        ("client_side_output_generation", "Off"),
    ]
    canonical = [
        ("backend", "vlm-engine"),
        ("effort", "high"),
        ("lang_list", "ch"),
        ("parse_method", "auto"),
        ("formula_enable", "true"),
        ("table_enable", "true"),
        ("image_analysis", "true"),
        ("client_side_output_generation", "false"),
    ]

    verbose_config, verbose_digest = identity(verbose)
    canonical_config, canonical_digest = identity(canonical)

    assert verbose_digest == canonical_digest
    assert verbose_config == canonical_config
    assert verbose_config["request"]["backend"] == "vlm-engine"
    assert verbose_config["request"]["formula_enable"] == "true"


def test_implicit_defaults_and_explicit_defaults_share_identity() -> None:
    implicit_config, implicit_digest = identity([])
    explicit_config, explicit_digest = identity(
        [
            ("backend", "PIPELINE"),
            ("effort", "medium"),
            ("lang_list", "ch"),
            ("parse_method", "auto"),
            ("formula_enable", "yes"),
            ("table_enable", "on"),
            ("image_analysis", "1"),
            ("client_side_output_generation", "0"),
        ]
    )

    assert implicit_digest == explicit_digest
    assert implicit_config == explicit_config


def test_exact_request_provenance_is_not_part_of_page_digest() -> None:
    original = [
        ("backend", " VLM-TRANSFORMERS "),
        ("table_enable", "True"),
        ("return_md", "YES"),
        ("force", "0"),
        ("return_md", "1"),
    ]
    equivalent = [
        ("backend", "vlm-engine"),
        ("table_enable", "1"),
        ("return_md", "true"),
    ]

    _, original_digest = identity(original)
    equivalent_config, equivalent_digest = identity(equivalent)

    assert original_digest == equivalent_digest
    assert "request_form" not in equivalent_config
    assert mineru_app._request_form_provenance(original) == [
        {"name": "backend", "value": " VLM-TRANSFORMERS "},
        {"name": "table_enable", "value": "True"},
        {"name": "return_md", "value": "YES"},
        {"name": "force", "value": "0"},
        {"name": "return_md", "value": "1"},
    ]


def test_distinct_parser_configurations_remain_distinct() -> None:
    _, baseline = identity([])
    distinct_forms = [
        [("backend", "vlm-engine")],
        [("parse_method", "ocr")],
        [("table_enable", "false")],
        [("formula_enable", "false")],
        [("image_analysis", "false")],
        [("server_url", "http://parser-a:30000")],
        [("server_url", "http://parser-b:30000")],
    ]
    distinct_digests = [identity(form)[1] for form in distinct_forms]

    assert baseline not in distinct_digests
    assert len(set(distinct_digests)) == len(distinct_digests)


def test_environment_boolean_and_enum_spelling_is_canonical(monkeypatch) -> None:
    monkeypatch.setattr(mineru_app, "TABLE_MERGE", " FALSE ")
    monkeypatch.setattr(mineru_app, "DEVICE_MODE", " CPU ")
    first_config, first_digest = identity([])
    monkeypatch.setattr(mineru_app, "TABLE_MERGE", "0")
    monkeypatch.setattr(mineru_app, "DEVICE_MODE", "cpu")
    second_config, second_digest = identity([])

    assert first_digest == second_digest
    assert first_config == second_config
    assert first_config["table_merge_enable"] == "false"
    assert first_config["device_mode"] == "cpu"


def test_upstream_fields_use_the_same_canonical_semantics() -> None:
    fields = mineru_app._upstream_fields(
        [
            ("backend", " VLM-TRANSFORMERS "),
            ("parse_method", " OCR "),
            ("table_enable", "1"),
            ("image_analysis", "OFF"),
            ("server_url", "http://parser:30000/path"),
        ]
    )

    assert ("backend", "vlm-engine") in fields
    assert ("parse_method", "ocr") in fields
    assert ("table_enable", "true") in fields
    assert ("image_analysis", "false") in fields
    assert ("server_url", "http://parser:30000/path") in fields
