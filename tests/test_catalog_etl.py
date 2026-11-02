from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook


HOST_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = Path("/scripts") if Path("/scripts/catalog_etl.py").is_file() else HOST_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import catalog_etl as etl  # noqa: E402


def raw_record(value: str) -> etl.RawRecord:
    values = {
        "source_sha256": "a" * 64,
        "cache_key": "b" * 64,
        "page_idx": 0,
        "table_index": 0,
        "row_index": 1,
        "column_index": 1,
        "artifact_path": "/parsed/example.json",
        "raw_parameter_name": "Bestell-Nr.",
        "raw_value": value,
    }
    return etl.RawRecord("raw", 2, values, etl.stable_id("raw", values), "openai-row")


def rule(*, data_type: str = "decimal") -> etl.MappingRule:
    return etl.MappingRule(
        id="test-rule",
        families=frozenset({"family"}),
        series=frozenset(),
        raw_headers=("d1",),
        normalized_headers=frozenset({"d1"}),
        canonical_field="d1",
        data_type=data_type,
        source_unit="mm",
        canonical_unit="mm",
        transform="identity",
        criticality="hard",
        review_required=False,
        matcher_column="d1",
    )


class NormalizationTests(unittest.TestCase):
    def test_headers_and_identifiers_preserve_source_vocabulary(self) -> None:
        self.assertEqual(etl.normalize_header(" Ø-Bereich mm "), "ø-bereichmm")
        self.assertEqual(etl.identifier_kind("Art.-Nr."), "article_number")
        self.assertEqual(etl.identifier_kind("Bestell-Nr."), "order_number")
        self.assertIsNone(etl.identifier_kind("d1"))

    def test_decimal_comma_diameter_and_tolerances(self) -> None:
        self.assertEqual(etl.normalize_value("Ø 11,80", rule())["value_decimal"], "11.8")
        asymmetric = etl.normalize_value("11,8 +0,02/-0,01", rule())
        self.assertEqual(asymmetric["value_text"], "11,8 +0,02/-0,01")
        self.assertEqual(asymmetric["value_decimal"], "11.8")
        self.assertEqual(asymmetric["tolerance_lower"], "-0.01")
        self.assertEqual(asymmetric["tolerance_upper"], "0.02")
        symmetric = etl.normalize_value("8 ± 0,1", rule())
        self.assertEqual(symmetric["tolerance_lower"], "-0.1")
        self.assertEqual(symmetric["tolerance_upper"], "0.1")

    def test_range_and_invalid_numeric_are_explicit(self) -> None:
        parsed = etl.normalize_value("3,5–12,0", rule(data_type="range"))
        self.assertEqual(parsed["value_min"], "3.5")
        self.assertEqual(parsed["value_max"], "12")
        with self.assertRaises(ValueError):
            etl.normalize_value("12 approx", rule())


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        info = {
            number: etl.SeriesInfo(number, "family", None, "sku", None)
            for number in ("3893", "3894", "4605", "4665")
        }
        self.mapping = etl.MappingProfile(
            raw={"matcher_contract": {}},
            rules=[],
            series=info,
            required_fields={"family": frozenset()},
            wide_fields=[],
        )

    def test_concatenated_fanout_identifiers(self) -> None:
        tokens = etl.extract_order_tokens(raw_record("3893 6.0053894 6.006"), self.mapping)
        self.assertEqual([item.order_number for item in tokens], ["3893 6.005", "3894 6.006"])

    def test_unknown_accessory_reference_is_not_promoted_to_sku(self) -> None:
        self.assertEqual(etl.extract_order_tokens(raw_record("4364 5.020"), self.mapping), [])

    def test_text_identifier_keeps_leading_zeroes(self) -> None:
        self.mapping.series["4605"] = etl.SeriesInfo("4605", "family", None, "sku", None)
        tokens = etl.extract_order_tokens(raw_record("4605 00123.040"), self.mapping)
        self.assertEqual(tokens[0].order_number, "4605 00123.040")


class WorkbookTests(unittest.TestCase):
    def test_formula_like_text_is_written_as_text_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.xlsx"
            etl.write_validated_workbook(
                path,
                [("data", ["identifier", "raw"], [{"identifier": "00123.040", "raw": "=2+2"}])],
                {"test": "formula-safety"},
            )
            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                cells = next(workbook["data"].iter_rows(min_row=2))
                self.assertEqual(cells[0].value, "00123.040")
                self.assertEqual(cells[1].value, "=2+2")
                self.assertNotEqual(cells[1].data_type, "f")
            finally:
                workbook.close()
            self.assertEqual(etl.validate_workbook(path)["manifest"]["test"], "formula-safety")


class RealRawSmokeTests(unittest.TestCase):
    @staticmethod
    def _paths() -> dict[str, Path] | None:
        if Path("/parsed/master_raw.xlsx").is_file():
            return {
                "raw": Path("/parsed/master_raw.xlsx"),
                "manifest": Path("/catalogs/manifests/guhring-production.yaml"),
                "mapping": Path("/mappings/guhring.yaml"),
                "schemas": Path("/schemas"),
                "decisions": Path("/catalogs/review_decisions.jsonl"),
                "openai": Path("/parsed/openai"),
            }
        raw = HOST_ROOT / "parsed/master_raw.xlsx"
        if not raw.is_file():
            return None
        return {
            "raw": raw,
            "manifest": HOST_ROOT / "catalogs/manifests/guhring-production.yaml",
            "mapping": HOST_ROOT / "mappings/guhring.yaml",
            "schemas": HOST_ROOT / "schemas",
            "decisions": HOST_ROOT / "catalogs/review_decisions.jsonl",
            "openai": HOST_ROOT / "parsed/openai",
        }

    def test_real_raw_build_is_immutable_fail_closed_and_cached(self) -> None:
        paths = self._paths()
        if paths is None:
            self.skipTest("parsed/master_raw.xlsx is not available")
        before = hashlib.sha256(paths["raw"].read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            config = etl.Config(
                raw_path=paths["raw"],
                catalog_manifest_path=paths["manifest"],
                mapping_path=paths["mapping"],
                manifest_schema_path=paths["schemas"] / "catalog-manifest.schema.json",
                mapping_schema_path=paths["schemas"] / "field-mapping.schema.json",
                decision_schema_path=paths["schemas"] / "review-decision.schema.json",
                decisions_path=paths["decisions"],
                openai_root=paths["openai"],
                output_root=temporary / "outputs",
                publish_root=temporary / "outputs/latest",
                logs_root=temporary / "logs",
                use_openai=False,
                force=False,
                fail_on_quality_gate=False,
            )
            first = etl.run_etl(config)
            self.assertFalse(first["cache_hit"])
            self.assertEqual(first["results"]["unique_order_numbers"], 124)
            self.assertEqual(first["completeness"]["expected_total"], 125)
            self.assertEqual(first["results"]["matcher_published_rows"], 0)
            second = etl.run_etl(config)
            self.assertTrue(second["cache_hit"])
            validated = etl.validate_published_outputs(config)
            self.assertEqual(validated["status"], "valid")
            matcher = load_workbook(
                config.publish_root / "master_matcher.xlsx", read_only=True, data_only=False
            )
            try:
                self.assertEqual(sum(1 for _ in matcher["matcher"].iter_rows()), 1)
                self.assertGreater(sum(1 for _ in matcher["blocked_candidates"].iter_rows()), 1)
            finally:
                matcher.close()
        after = hashlib.sha256(paths["raw"].read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
