#!/usr/bin/env python3
"""Build reviewed, canonical and matcher-ready catalog layers from MinerU RAW.

The pipeline is deliberately fail-closed.  It never changes ``master_raw.xlsx``,
never accepts an OpenAI proposal without an explicit review decision, and never
publishes a matcher row while catalog-wide or row-level blockers are open.
Parser attempts are selected by the exact ``cache_key`` pinned in the catalog
manifest; CPU/VLM outputs for the same page are therefore never mixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import export_raw


ETL_VERSION = "1.2.0"
CACHE_SCHEMA_VERSION = 1
WORKBOOK_SCHEMA_VERSION = "1.1"
EXCEL_MAX_TEXT = 32_767

RAW_REQUIRED_COLUMNS = set(export_raw.ALL_COLUMNS)
OPENAI_REVIEW_COLUMNS = {
    "openai_row_id",
    "openai_verdict",
    "openai_confidence",
    "openai_issues",
    "openai_processed_product_family",
    "openai_processed_article_number",
    "openai_processed_parameter_name",
    "openai_processed_value",
    "openai_evidence",
    "openai_model",
    "openai_response_id",
    "openai_cache_key",
    "openai_cache_hit",
    "openai_verification_basis",
}

PRODUCT_COLUMNS = [
    "product_key",
    "product_version_id",
    "catalog_id",
    "catalog_role",
    "catalog_revision",
    "manufacturer",
    "family",
    "subfamily",
    "series",
    "item_type",
    "article_number",
    "order_number",
    "size_code",
    "standard",
    "identity_status",
    "publication_status",
    "data_confidence",
    "primary_provenance_id",
    "logical_row_id",
]

ATTRIBUTE_COLUMNS = [
    "attribute_id",
    "product_version_id",
    "canonical_field",
    "raw_parameter_name",
    "value_type",
    "value_text",
    "value_decimal",
    "value_min",
    "value_max",
    "tolerance_lower",
    "tolerance_upper",
    "fit_class",
    "raw_unit",
    "canonical_unit",
    "mapping_profile",
    "mapping_version",
    "mapping_rule_id",
    "criticality",
    "confidence",
    "review_status",
    "review_decision_id",
    "decision_source",
    "decision_reason",
    "provenance_id",
]

RELATION_COLUMNS = [
    "relation_id",
    "source_product_version_id",
    "relation_type",
    "target_product_key",
    "target_product_version_id",
    "target_external_reference",
    "criticality",
    "confidence",
    "review_status",
    "review_decision_id",
    "provenance_id",
    "catalog_id",
    "catalog_revision",
    "direction",
]

PROVENANCE_COLUMNS = [
    "provenance_id",
    "evidence_index",
    "raw_element_id",
    "logical_row_id",
    "catalog_id",
    "document_id",
    "parent_document_id",
    "source_file",
    "source_sha256",
    "page",
    "page_idx",
    "source_page",
    "source_page_idx",
    "catalog_page_label",
    "table_index",
    "row_index",
    "column_index",
    "bbox",
    "bbox_scope",
    "bbox_units",
    "raw_header_path",
    "raw_parameter_name",
    "raw_value",
    "evidence",
    "evidence_type",
    "parser",
    "parser_version",
    "parser_backend",
    "cache_key",
    "artifact_path",
]

ISSUE_COLUMNS = [
    "issue_id",
    "catalog_id",
    "entity_type",
    "entity_id",
    "issue_code",
    "severity",
    "blocks_publication",
    "status",
    "message",
    "provenance_id",
    "detected_by",
    "detector_version",
    "created_at",
    "resolved_at",
    "resolution",
    "resolution_source",
    "resolution_actor",
]

REVIEWED_EXTRA_COLUMNS = [
    "raw_element_id",
    "openai_row_id",
    "catalog_id",
    "catalog_role",
    "catalog_revision",
    "manufacturer",
    "document_id",
    "source_page",
    "selected_artifact",
    "selection_reason",
    "logical_row_ids",
    "row_classification",
    "mapping_rule_id",
    "canonical_field",
    "validation_mode",
    "openai_verdict",
    "openai_confidence",
    "openai_issues",
    "openai_proposed_parameter_name",
    "openai_proposed_value",
    "openai_evidence",
    "review_status",
    "accepted_value",
    "decision_source",
    "decision_reason",
    "review_decision_id",
    "model_id",
    "prompt_version",
    "response_id",
    "reviewed_at",
]


class ETLError(RuntimeError):
    """Base error for deterministic catalog processing."""


class ConfigurationError(ETLError):
    """A local schema, manifest, mapping or input is invalid."""


class WorkbookValidationError(ETLError):
    """An XLSX workbook does not match its declared content."""


@dataclass(frozen=True)
class Config:
    raw_path: Path
    catalog_manifest_path: Path
    mapping_path: Path
    manifest_schema_path: Path
    mapping_schema_path: Path
    decision_schema_path: Path
    decisions_path: Path
    openai_root: Path
    output_root: Path
    publish_root: Path
    logs_root: Path
    use_openai: bool
    force: bool
    fail_on_quality_gate: bool
    use_page_completeness: bool = False
    reconstructions_path: Path | None = None
    allow_partial_inventory: bool = False


@dataclass(frozen=True)
class RawRecord:
    sheet_name: str
    workbook_row: int
    values: dict[str, Any]
    raw_element_id: str
    openai_row_id: str

    @property
    def source_sha256(self) -> str:
        return str(self.values.get("source_sha256") or "")

    @property
    def page_idx(self) -> int:
        value = self.values.get("page_idx")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return int(value or 0)

    @property
    def cache_key(self) -> str:
        return str(self.values.get("cache_key") or "")

    @property
    def table_index(self) -> int:
        return int(self.values.get("table_index") or 0)

    @property
    def row_index(self) -> int:
        return int(self.values.get("row_index") or 0)

    @property
    def column_index(self) -> int:
        return int(self.values.get("column_index") or 0)


@dataclass(frozen=True)
class DocumentInfo:
    document_id: str
    file: str
    sha256: str
    pages: int
    source_pages: tuple[int, ...]
    aliases: tuple[str, ...]
    source_url: str | None
    original_source_sha256: str | None

    def source_page(self, page_idx: int) -> int | None:
        if 0 <= page_idx < len(self.source_pages):
            return self.source_pages[page_idx]
        return None


@dataclass(frozen=True)
class SeriesInfo:
    article_number: str
    family: str
    subfamily: str | None
    item_type: str
    series_name: str | None


@dataclass(frozen=True)
class MappingRule:
    id: str
    families: frozenset[str]
    series: frozenset[str]
    raw_headers: tuple[str, ...]
    normalized_headers: frozenset[str]
    canonical_field: str
    data_type: str
    source_unit: str | None
    canonical_unit: str | None
    transform: str
    criticality: str
    review_required: bool
    matcher_column: str | None
    raw_header_paths: tuple[tuple[str, ...], ...] = ()
    minimum: str | None = None
    maximum: str | None = None
    allowed_values: tuple[str, ...] = ()
    allow_fit: bool = False


@dataclass
class MappingProfile:
    raw: dict[str, Any]
    rules: list[MappingRule]
    series: dict[str, SeriesInfo]
    required_fields: dict[str, frozenset[str]]
    wide_fields: list[str]

    def resolve(
        self,
        family: str | None,
        article_number: str | None,
        header: str | None,
        header_path: Sequence[str] | None = None,
    ) -> MappingRule | None:
        if not family or not header:
            return None
        key = normalize_header(header)
        candidates = [
            rule
            for rule in self.rules
            if family in rule.families
            and key in rule.normalized_headers
            and (not rule.series or article_number in rule.series)
            and (
                not rule.raw_header_paths
                or tuple(normalize_header(str(part)) for part in (header_path or []))
                in rule.raw_header_paths
            )
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda rule: (
                not bool(rule.series),
                not bool(rule.raw_header_paths),
                rule.id,
            )
        )
        winner = candidates[0]
        conflicting = {
            (
                item.canonical_field,
                item.data_type,
                item.source_unit,
                item.canonical_unit,
                item.transform,
                item.criticality,
                item.review_required,
                item.minimum,
                item.maximum,
                item.allowed_values,
                item.allow_fit,
                item.matcher_column,
            )
            for item in candidates
            if bool(item.series) == bool(winner.series)
            and bool(item.raw_header_paths) == bool(winner.raw_header_paths)
        }
        if len(conflicting) > 1:
            raise ConfigurationError(
                f"Conflicting mapping rules for family={family!r}, series={article_number!r}, header={header!r}."
            )
        return winner

    def header_known(self, header: str | None) -> bool:
        if not header:
            return False
        key = normalize_header(header)
        return any(key in rule.normalized_headers for rule in self.rules) or any(
            key in {normalize_header(value) for value in rule["raw_headers"]}
            for rule in self.raw.get("relation_rules", [])
        )


@dataclass
class TableModel:
    source_sha256: str
    page_idx: int
    cache_key: str
    table_index: int
    cells: dict[tuple[int, int], RawRecord]
    header_by_column: dict[int, str]
    header_row: int | None
    data_rows: list[int]
    parameter_columns: set[int]
    identifier_columns: set[int]
    continuation_of: str | None = None
    header_evidence: dict[int, RawRecord] = field(default_factory=dict)
    row_types: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderToken:
    article_number: str
    order_number: str
    raw_record: RawRecord


@dataclass
class AttributeCandidate:
    raw_record: RawRecord
    raw_header: str
    rule: MappingRule
    normalized: dict[str, Any]
    provenance_id: str
    issues: list[str] = field(default_factory=list)


@dataclass
class ProductCandidate:
    logical_row_id: str
    article_number: str | None
    order_number: str | None
    family: str | None
    subfamily: str | None
    item_type: str
    source_sha256: str
    page_idx: int
    table_index: int
    row_index: int
    identifier_record: RawRecord | None
    attributes: list[AttributeCandidate] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)
    product_key: str | None = None
    product_version_id: str | None = None
    publication_status: str = "draft"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S.%fZ")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *values: Any) -> str:
    return f"{prefix}_{sha256_json(list(values))[:32]}"


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        + b"\n",
    )


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
        destination.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Could not read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"YAML root must be an object: {path}")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"JSON root must be an object: {path}")
    return value


def validate_schema(instance: Any, schema_path: Path, label: str) -> None:
    schema = load_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(instance), key=lambda error: list(error.absolute_path)
    )
    if errors:
        rendered = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ConfigurationError(
            f"{label} does not match {schema_path}: " + "; ".join(rendered)
        )


def normalize_header(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = text.replace("∅", "ø").replace("Ø", "ø")
    text = re.sub(r"\s+", "", text)
    text = text.replace("–", "-").replace("—", "-")
    return text


def identifier_kind(header: str | None) -> str | None:
    key = normalize_header(header)
    compact = re.sub(r"[^a-zäöüß]", "", key)
    if "bestellnr" in compact or "bestellnummer" in compact or "ordernumber" in compact:
        return "order_number"
    if (
        "artikelnr" in compact
        or "artikelnummer" in compact
        or compact in {"artnr", "articlenumber", "articleno"}
    ):
        return "article_number"
    if key in {"артикул", "кодтовара", "sku"}:
        return "article_number"
    if key in {"заказнойномер", "номерзаказа", "кодзаказа"}:
        return "order_number"
    return None


def _load_documents(manifest: Mapping[str, Any]) -> dict[str, DocumentInfo]:
    documents: dict[str, DocumentInfo] = {}
    ids: set[str] = set()
    for item in manifest["documents"]:
        sha = str(item["sha256"])
        if sha in documents:
            raise ConfigurationError(f"Duplicate document SHA-256 in manifest: {sha}")
        document_id = str(item["document_id"])
        if document_id in ids:
            raise ConfigurationError(
                f"Duplicate document_id in manifest: {document_id}"
            )
        ids.add(document_id)
        source_pages = tuple(int(value) for value in item.get("source_pages") or [])
        if source_pages and len(source_pages) != int(item["pages"]):
            raise ConfigurationError(
                f"Document {document_id} has {len(source_pages)} source_pages for {item['pages']} pages."
            )
        documents[sha] = DocumentInfo(
            document_id=document_id,
            file=str(item["file"]),
            sha256=sha,
            pages=int(item["pages"]),
            source_pages=source_pages,
            aliases=tuple(str(value) for value in item.get("aliases") or []),
            source_url=item.get("source_url"),
            original_source_sha256=item.get("original_source_sha256"),
        )
    return documents


def load_manifest(
    config: Config,
) -> tuple[dict[str, Any], dict[str, DocumentInfo], dict[tuple[str, int], str]]:
    manifest = load_yaml(config.catalog_manifest_path)
    validate_schema(manifest, config.manifest_schema_path, "Catalog manifest")
    import catalog_registry

    try:
        catalog_registry.verify_inventory(manifest, config.catalog_manifest_path)
    except (ValueError, OSError) as exc:
        raise ConfigurationError(str(exc)) from exc
    documents = _load_documents(manifest)
    selections: dict[tuple[str, int], str] = {}
    for item in manifest.get("parse_selection") or []:
        key = (str(item["source_sha256"]), int(item["page_idx"]))
        if key in selections:
            raise ConfigurationError(f"Duplicate parse_selection for {key}.")
        if key[0] not in documents:
            raise ConfigurationError(
                f"parse_selection references an unregistered SHA: {key[0]}"
            )
        if key[1] >= documents[key[0]].pages:
            raise ConfigurationError(
                f"parse_selection page is outside document bounds: {key}"
            )
        selections[key] = str(item["cache_key"])
    expected_pages = {
        (document.sha256, page_idx)
        for document in documents.values()
        for page_idx in range(document.pages)
    }
    reviewed_empty = {
        (row["source_sha256"], row["page_idx"])
        for row in manifest.get("page_states", [])
        if row["status"] == "empty" and row.get("reviewer") and row.get("reason")
    }
    missing = expected_pages - set(selections) - reviewed_empty
    if missing and (
        not config.allow_partial_inventory or manifest["status"] == "published"
    ):
        raise ConfigurationError(
            "Manifest must pin one cache_key for every registered page; missing "
            + ", ".join(f"{sha[:12]}:{page}" for sha, page in sorted(missing))
        )
    return manifest, documents, selections


def load_mapping(config: Config, manifest: Mapping[str, Any]) -> MappingProfile:
    raw = load_yaml(config.mapping_path)
    validate_schema(raw, config.mapping_schema_path, "FIELD_MAPPING")
    if raw["mapping_profile"] != manifest["mapping_profile"]:
        raise ConfigurationError(
            "Manifest mapping_profile does not match FIELD_MAPPING."
        )
    if str(raw["manufacturer"]).casefold() != str(manifest["manufacturer"]).casefold():
        raise ConfigurationError("Manifest manufacturer does not match FIELD_MAPPING.")
    if raw["matcher_contract"]["version"] != manifest["matcher_contract"]["version"]:
        raise ConfigurationError(
            "Manifest and mapping matcher contract versions differ."
        )

    series: dict[str, SeriesInfo] = {}
    for item in raw["series"]:
        number = str(item["article_number"])
        if number in series:
            raise ConfigurationError(f"Duplicate series mapping: {number}")
        series[number] = SeriesInfo(
            article_number=number,
            family=str(item["family"]),
            subfamily=item.get("subfamily"),
            item_type=str(item["item_type"]),
            series_name=item.get("series_name"),
        )
    required_fields: dict[str, frozenset[str]] = {}
    for item in raw["families"]:
        name = str(item["name"])
        if name in required_fields:
            raise ConfigurationError(f"Duplicate family contract: {name}")
        required_fields[name] = frozenset(
            str(value) for value in item["required_fields"]
        )
    for info in series.values():
        if info.family not in required_fields:
            raise ConfigurationError(
                f"Series {info.article_number} references unknown family {info.family}."
            )

    rules: list[MappingRule] = []
    rule_ids: set[str] = set()
    for item in raw["rules"]:
        rule_id = str(item["id"])
        if rule_id in rule_ids:
            raise ConfigurationError(f"Duplicate mapping rule id: {rule_id}")
        rule_ids.add(rule_id)
        families = frozenset(str(value) for value in item["families"])
        unknown_families = families - set(required_fields)
        if unknown_families:
            raise ConfigurationError(
                f"Rule {rule_id} references unknown families: {sorted(unknown_families)}"
            )
        series_filter = frozenset(str(value) for value in item.get("series") or [])
        unknown_series = series_filter - set(series)
        if unknown_series:
            raise ConfigurationError(
                f"Rule {rule_id} references unknown series: {sorted(unknown_series)}"
            )
        headers = tuple(str(value) for value in item["raw_headers"])
        rules.append(
            MappingRule(
                id=rule_id,
                families=families,
                series=series_filter,
                raw_headers=headers,
                normalized_headers=frozenset(
                    normalize_header(value) for value in headers
                ),
                canonical_field=str(item["canonical_field"]),
                data_type=str(item["data_type"]),
                source_unit=item.get("source_unit"),
                canonical_unit=item.get("canonical_unit"),
                transform=str(item.get("transform") or "identity"),
                criticality=str(item["criticality"]),
                review_required=bool(item.get("review_required", False)),
                matcher_column=item.get("matcher_column"),
                raw_header_paths=tuple(
                    tuple(normalize_header(str(part)) for part in path)
                    for path in item.get("raw_header_paths", [])
                ),
                minimum=item.get("minimum"),
                maximum=item.get("maximum"),
                allowed_values=tuple(item.get("allowed_values", [])),
                allow_fit=bool(item.get("allow_fit", False)),
            )
        )

    profile = MappingProfile(
        raw=raw,
        rules=rules,
        series=series,
        required_fields=required_fields,
        wide_fields=[str(value) for value in raw["matcher_contract"]["wide_fields"]],
    )
    # Exercise every possible lookup once so equal-priority conflicts fail before processing.
    for article, info in series.items():
        for rule in rules:
            if info.family not in rule.families:
                continue
            for header in rule.raw_headers:
                for path in rule.raw_header_paths or ((),):
                    profile.resolve(info.family, article, header, path)
    for context in raw.get("context_rules", []):
        try:
            pattern = re.compile(context["pattern"])
        except re.error as exc:
            raise ConfigurationError(f"Invalid context pattern: {exc}") from exc
        if (
            "value" not in pattern.groupindex
            or not set(context["families"]).issubset(required_fields)
            or not set(context["series"]).issubset(series)
        ):
            raise ConfigurationError(
                "Context patterns need a named value group and approved families/series."
            )
    for rule in rules:
        if rule.allow_fit and rule.data_type != "decimal":
            raise ConfigurationError("allow_fit requires a decimal mapping rule.")
        for bound in (rule.minimum, rule.maximum):
            if bound is not None:
                try:
                    _parse_decimal(bound)
                except ValueError as exc:
                    raise ConfigurationError(
                        f"Invalid bound for {rule.id}: {exc}"
                    ) from exc
        if (
            rule.minimum is not None
            and rule.maximum is not None
            and Decimal(rule.minimum) > Decimal(rule.maximum)
        ):
            raise ConfigurationError(f"Minimum exceeds maximum for {rule.id}.")
    for identity in raw.get("identifiers", []):
        try:
            pattern = re.compile(identity["pattern"])
        except re.error as exc:
            raise ConfigurationError(f"Invalid identifier pattern: {exc}") from exc
        if "order" not in pattern.groupindex or (
            "series" not in pattern.groupindex and not identity.get("article_number")
        ):
            raise ConfigurationError(
                "Identifier patterns need named order and series groups (or fixed article_number)."
            )
    for relation in raw.get("relation_rules", []):
        if (
            not set(relation["families"]).issubset(required_fields)
            or relation.get("separator", ";") == ""
        ):
            raise ConfigurationError("Invalid relation families or separator.")
    for rule in rules:
        for family in rule.families:
            relation_rule(profile, family, rule.raw_headers[0])
    return profile


def load_raw_workbook(path: Path) -> tuple[list[str], list[RawRecord], dict[str, str]]:
    if not path.is_file():
        raise ConfigurationError(
            f"RAW workbook does not exist: {path}. Run `make export` first."
        )
    workbook = load_workbook(path, read_only=True, data_only=False)
    records: list[RawRecord] = []
    raw_headers: list[str] | None = None
    raw_hasher = hashlib.sha256()
    manifest_values: dict[str, str] = {}
    try:
        for worksheet in workbook.worksheets:
            if worksheet.title == "_manifest":
                rows = worksheet.iter_rows(values_only=True)
                try:
                    header = next(rows)
                except StopIteration as exc:
                    raise WorkbookValidationError("RAW _manifest is empty.") from exc
                if tuple(header) != ("key", "value"):
                    raise WorkbookValidationError("RAW _manifest header is invalid.")
                for key, value, *_ in rows:
                    if key is not None:
                        manifest_values[str(key)] = (
                            str(value) if value is not None else ""
                        )
                continue
            rows = worksheet.iter_rows()
            try:
                header_cells = next(rows)
            except StopIteration:
                continue
            headers = [str(cell.value or "") for cell in header_cells]
            if len(headers) != len(set(headers)) or any(not value for value in headers):
                raise WorkbookValidationError(
                    f"RAW sheet {worksheet.title!r} has invalid headers."
                )
            missing = RAW_REQUIRED_COLUMNS - set(headers)
            if missing:
                raise WorkbookValidationError(
                    f"RAW sheet misses columns: {sorted(missing)}"
                )
            if raw_headers is None:
                raw_headers = headers
            elif headers != raw_headers:
                raise WorkbookValidationError(
                    "All RAW sheets must use one column order."
                )
            for workbook_row, cells in enumerate(rows, start=2):
                if all(cell.value is None for cell in cells):
                    continue
                if any(cell.data_type == "f" for cell in cells):
                    raise WorkbookValidationError(
                        f"Formula found in RAW sheet {worksheet.title}, row {workbook_row}."
                    )
                values = {
                    header: json_value(cell.value)
                    for header, cell in zip(headers, cells, strict=False)
                }
                hash_values = dict(values)
                if hash_values.get("raw_value") is None:
                    hash_values["raw_value"] = ""
                raw_hasher.update(export_raw._canonical_record_bytes(hash_values))
                identity = {
                    "sheet": worksheet.title,
                    "workbook_row": workbook_row,
                    "values": values,
                }
                openai_row_id = sha256_json(identity)[:32]
                raw_element_id = stable_id(
                    "raw",
                    values.get("source_sha256"),
                    values.get("cache_key"),
                    values.get("page_idx"),
                    values.get("table_index"),
                    values.get("row_index"),
                    values.get("column_index"),
                    values.get("artifact_path"),
                    values.get("raw_parameter_name"),
                    values.get("raw_value"),
                )
                records.append(
                    RawRecord(
                        sheet_name=worksheet.title,
                        workbook_row=workbook_row,
                        values=values,
                        raw_element_id=raw_element_id,
                        openai_row_id=openai_row_id,
                    )
                )
    finally:
        workbook.close()
    if raw_headers is None:
        raise WorkbookValidationError("RAW workbook contains no records.")
    duplicated_raw_ids = [
        raw_id
        for raw_id, count in Counter(
            record.raw_element_id for record in records
        ).items()
        if count > 1
    ]
    if duplicated_raw_ids:
        raise WorkbookValidationError(
            f"RAW workbook contains {len(duplicated_raw_ids)} duplicate stable raw_element_id values."
        )
    duplicated_openai_ids = [
        row_id
        for row_id, count in Counter(record.openai_row_id for record in records).items()
        if count > 1
    ]
    if duplicated_openai_ids:
        raise WorkbookValidationError(
            f"RAW workbook contains {len(duplicated_openai_ids)} duplicate OpenAI row identities."
        )
    if not manifest_values:
        raise WorkbookValidationError("RAW workbook has no _manifest values.")
    expected_records = int(manifest_values.get("records", "-1"))
    if expected_records != len(records):
        raise WorkbookValidationError(
            f"RAW workbook contains {len(records)} records, manifest declares {expected_records}."
        )
    actual_record_hash = raw_hasher.hexdigest()
    if manifest_values.get("record_sha256") != actual_record_hash:
        raise WorkbookValidationError("RAW workbook record_sha256 is invalid.")
    return raw_headers, records, manifest_values


def load_openai_reviews(
    config: Config,
    raw_sha256: str,
    raw_records: Sequence[RawRecord],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not config.use_openai:
        return {}, {"status": "not_requested"}
    latest_path = config.openai_root / "latest.json"
    if not latest_path.is_file():
        raise ConfigurationError(
            "OpenAI review was requested, but parsed/openai/latest.json is absent. "
            "Run an explicit OpenAI target first."
        )
    latest = load_json(latest_path)
    run_id = str(latest.get("run_id") or "")
    if latest.get("status") != "success" or not run_id:
        raise ConfigurationError("OpenAI latest pointer is not a successful run.")
    run_manifest_path = config.openai_root / "runs" / run_id / "manifest.json"
    run_manifest = load_json(run_manifest_path)
    if run_manifest.get("status") != "success":
        raise ConfigurationError("OpenAI run manifest is not successful.")
    if run_manifest.get("input_sha256") != raw_sha256:
        raise ConfigurationError(
            "OpenAI review was created from a different master_raw.xlsx; refusing a stale join."
        )
    processed_path = config.openai_root / "runs" / run_id / "master_processed.xlsx"
    expected_sha = str(run_manifest.get("processed_xlsx_sha256") or "")
    if not processed_path.is_file() or sha256_file(processed_path) != expected_sha:
        raise ConfigurationError(
            "OpenAI processed workbook is missing or has a bad SHA-256."
        )

    raw_by_openai_id = {record.openai_row_id: record for record in raw_records}
    reviews: dict[str, dict[str, Any]] = {}
    workbook = load_workbook(processed_path, read_only=True, data_only=False)
    try:
        if workbook.sheetnames != ["processed"]:
            raise WorkbookValidationError(
                "OpenAI processed workbook must contain only 'processed'."
            )
        rows = workbook["processed"].iter_rows()
        try:
            header_cells = next(rows)
        except StopIteration as exc:
            raise WorkbookValidationError(
                "OpenAI processed workbook is empty."
            ) from exc
        headers = [str(cell.value or "") for cell in header_cells]
        if not OPENAI_REVIEW_COLUMNS.issubset(headers):
            raise WorkbookValidationError(
                "OpenAI processed workbook misses review columns."
            )
        raw_headers = [
            header for header in headers if header not in OPENAI_REVIEW_COLUMNS
        ]
        for cells in rows:
            if all(cell.value is None for cell in cells):
                continue
            if any(cell.data_type == "f" for cell in cells):
                raise WorkbookValidationError(
                    "Formula found in OpenAI processed workbook."
                )
            values = {
                header: json_value(cell.value)
                for header, cell in zip(headers, cells, strict=False)
            }
            row_id = str(values.get("openai_row_id") or "")
            source = raw_by_openai_id.get(row_id)
            if source is None:
                raise WorkbookValidationError(
                    f"OpenAI workbook contains unknown row_id {row_id!r}."
                )
            for header in raw_headers:
                if values.get(header) != source.values.get(header):
                    raise WorkbookValidationError(
                        f"OpenAI workbook changed RAW field {header!r} for {row_id}."
                    )
            if row_id in reviews:
                raise WorkbookValidationError(f"Duplicate OpenAI row_id {row_id}.")
            issues_raw = values.get("openai_issues")
            try:
                issues = json.loads(issues_raw) if isinstance(issues_raw, str) else []
            except json.JSONDecodeError as exc:
                raise WorkbookValidationError(
                    f"Invalid OpenAI issues JSON for {row_id}."
                ) from exc
            if not isinstance(issues, list):
                raise WorkbookValidationError(
                    f"OpenAI issues must be a list for {row_id}."
                )
            values["openai_issues"] = [str(value) for value in issues]
            values["prompt_version"] = run_manifest.get("prompt_version")
            values["reviewed_at"] = run_manifest.get("created_at")
            reviews[row_id] = values
    finally:
        workbook.close()
    return reviews, {
        "status": "loaded",
        "run_id": run_id,
        "reviewed_rows": len(reviews),
        "processed_xlsx_sha256": expected_sha,
        "response_models": run_manifest.get("response_models") or [],
        "prompt_version": run_manifest.get("prompt_version"),
    }


def load_review_decisions(
    config: Config,
    raw_records: Sequence[RawRecord],
) -> tuple[dict[str, dict[str, Any]], str]:
    if not config.decisions_path.exists():
        return {}, sha256_bytes(b"")
    schema = load_json(config.decision_schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    raw_ids = {record.raw_element_id for record in raw_records}
    decisions: dict[str, dict[str, Any]] = {}
    canonical_lines: list[bytes] = []
    try:
        lines = config.decisions_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"Could not read review decisions: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decision = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"Invalid review decision JSON on line {line_number}: {exc}"
            ) from exc
        errors = sorted(
            validator.iter_errors(decision), key=lambda error: list(error.absolute_path)
        )
        if errors:
            raise ConfigurationError(
                f"Review decision line {line_number} is invalid: {errors[0].message}"
            )
        if "raw_element_id" not in decision:
            canonical_lines.append(canonical_bytes(decision) + b"\n")
            continue
        raw_id = str(decision["raw_element_id"])
        if raw_id not in raw_ids:
            raise ConfigurationError(
                f"Review decision references unknown raw_element_id: {raw_id}"
            )
        if raw_id in decisions and datetime.fromisoformat(
            decision["decided_at"].replace("Z", "+00:00")
        ) <= datetime.fromisoformat(
            decisions[raw_id]["decided_at"].replace("Z", "+00:00")
        ):
            raise ConfigurationError(
                f"Review history must advance decided_at for {raw_id}."
            )
        normalized = {str(key): json_value(value) for key, value in decision.items()}
        decisions[raw_id] = normalized
        canonical_lines.append(canonical_bytes(normalized) + b"\n")
    return decisions, sha256_bytes(b"".join(canonical_lines))


class IssueRegistry:
    def __init__(self, catalog_id: str, created_at: str) -> None:
        self.catalog_id = catalog_id
        self.created_at = created_at
        self.rows: list[dict[str, Any]] = []
        self._by_identity: dict[str, str] = {}

    def add(
        self,
        *,
        entity_type: str,
        entity_id: str,
        code: str,
        severity: str,
        blocks_publication: bool,
        message: str,
        provenance_id: str | None = None,
    ) -> str:
        identity = sha256_json(
            [
                entity_type,
                entity_id,
                code,
                severity,
                blocks_publication,
                message,
                provenance_id,
            ]
        )
        existing = self._by_identity.get(identity)
        if existing:
            return existing
        issue_id = f"issue_{identity[:32]}"
        self._by_identity[identity] = issue_id
        self.rows.append(
            {
                "issue_id": issue_id,
                "catalog_id": self.catalog_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "issue_code": code,
                "severity": severity,
                "blocks_publication": blocks_publication,
                "status": "open",
                "message": message,
                "provenance_id": provenance_id,
                "detected_by": "catalog_etl",
                "detector_version": ETL_VERSION,
                "created_at": self.created_at,
                "resolved_at": None,
                "resolution": None,
                "resolution_source": None,
                "resolution_actor": None,
            }
        )
        return issue_id

    def is_blocking(self, issue_id: str) -> bool:
        return any(
            row["issue_id"] == issue_id and bool(row["blocks_publication"])
            for row in self.rows
        )


UNIT_VALUES = {"mm", "°", "grad", "inch", "in", ""}


def _row_values(
    cells: Mapping[tuple[int, int], RawRecord], row_index: int
) -> list[RawRecord]:
    return sorted(
        (record for (row, _), record in cells.items() if row == row_index),
        key=lambda record: record.column_index,
    )


def _is_unit_row(records: Sequence[RawRecord]) -> bool:
    values = [
        str(record.values.get("raw_value") or "").strip().casefold()
        for record in records
    ]
    nonempty = [value for value in values if value]
    return (
        bool(nonempty)
        and sum(value in UNIT_VALUES for value in nonempty) / len(nonempty) >= 0.6
    )


def _header_score(records: Sequence[RawRecord], mapping: MappingProfile) -> int:
    score = 0
    for record in records:
        value = str(record.values.get("raw_value") or "")
        if identifier_kind(value) or mapping.header_known(value):
            score += 1
    return score


def analyze_table(records: Sequence[RawRecord], mapping: MappingProfile) -> TableModel:
    if not records:
        raise ConfigurationError("Cannot analyze an empty table.")
    exemplar = records[0]
    cells: dict[tuple[int, int], RawRecord] = {}
    for record in sorted(
        records,
        key=lambda item: (item.row_index, item.column_index, item.raw_element_id),
    ):
        coordinate = (record.row_index, record.column_index)
        existing = cells.get(coordinate)
        if existing is not None and existing.values.get(
            "raw_value"
        ) != record.values.get("raw_value"):
            raise ConfigurationError(
                f"Selected artifact contains conflicting cells at table={record.table_index}, coordinate={coordinate}."
            )
        cells.setdefault(coordinate, record)

    headers_by_column: dict[int, Counter[str]] = defaultdict(Counter)
    for record in cells.values():
        parameter = record.values.get("raw_parameter_name")
        if parameter not in (None, ""):
            headers_by_column[record.column_index][str(parameter)] += 1
    header_by_column = {
        column: counts.most_common(1)[0][0]
        for column, counts in headers_by_column.items()
    }

    all_rows = sorted({row for row, _ in cells})
    header_row: int | None = None
    best_score = 0
    for row_index in all_rows:
        row = _row_values(cells, row_index)
        score = _header_score(row, mapping)
        if score > best_score:
            best_score = score
            header_row = row_index
    if best_score < 1:
        header_row = None
    elif header_row is not None:
        for record in _row_values(cells, header_row):
            value = record.values.get("raw_value")
            if value not in (None, "") and (
                identifier_kind(str(value)) or mapping.header_known(str(value))
            ):
                header_by_column[record.column_index] = str(value)

    parameter_columns = {
        column
        for column, header in header_by_column.items()
        if not identifier_kind(header) and mapping.header_known(header)
    }
    identifier_columns = {
        column for column, header in header_by_column.items() if identifier_kind(header)
    }

    data_rows: list[int] = []
    for row_index in all_rows:
        if row_index == header_row:
            continue
        row = _row_values(cells, row_index)
        if not any(record.values.get("raw_value") not in (None, "") for record in row):
            continue
        if _is_unit_row(row):
            continue
        score = _header_score(row, mapping)
        nonempty = sum(
            record.values.get("raw_value") not in (None, "") for record in row
        )
        if score >= 2 and score * 2 >= nonempty:
            continue
        data_rows.append(row_index)

    return TableModel(
        source_sha256=exemplar.source_sha256,
        page_idx=exemplar.page_idx,
        cache_key=exemplar.cache_key,
        table_index=exemplar.table_index,
        cells=cells,
        header_by_column=header_by_column,
        header_row=header_row,
        data_rows=data_rows,
        parameter_columns=parameter_columns,
        identifier_columns=identifier_columns,
    )


def select_raw_records(
    records: Sequence[RawRecord],
    documents: Mapping[str, DocumentInfo],
    selections: Mapping[tuple[str, int], str],
    reviewed_empty: set[tuple[str, int]] | frozenset[tuple[str, int]] = frozenset(),
) -> tuple[list[RawRecord], dict[str, bool], dict[str, str], dict[str, Any]]:
    available: dict[tuple[str, int], set[str]] = defaultdict(set)
    artifact_paths: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    for record in records:
        page = (record.source_sha256, record.page_idx)
        available[page].add(record.cache_key)
        artifact_paths[(record.source_sha256, record.page_idx, record.cache_key)].add(
            str(record.values.get("artifact_path") or "")
        )
    selected_ids: dict[str, bool] = {}
    selection_reason: dict[str, str] = {}
    selected: list[RawRecord] = []
    page_reports: list[dict[str, Any]] = []
    for page, cache_key in sorted(selections.items()):
        if page in reviewed_empty and cache_key not in available.get(page, set()):
            page_reports.append(
                {
                    "document_id": documents[page[0]].document_id,
                    "source_sha256": page[0],
                    "page_idx": page[1],
                    "cache_key": cache_key,
                    "status": "reviewed_empty",
                    "raw_records": 0,
                }
            )
            continue
        if cache_key not in available.get(page, set()):
            raise ConfigurationError(
                f"Pinned cache_key {cache_key} is absent for source={page[0]}, page_idx={page[1]}."
            )
        paths = {
            value for value in artifact_paths[(page[0], page[1], cache_key)] if value
        }
        if len(paths) != 1:
            raise ConfigurationError(
                f"Pinned extraction identity resolves to {len(paths)} artifact paths for {page}; refusing to mix them."
            )
        page_records = [
            record
            for record in records
            if (record.source_sha256, record.page_idx, record.cache_key)
            == (page[0], page[1], cache_key)
        ]
        selected.extend(page_records)
        for record in page_records:
            selected_ids[record.raw_element_id] = True
            selection_reason[record.raw_element_id] = "manifest_cache_key"
        document = documents[page[0]]
        page_reports.append(
            {
                "document_id": document.document_id,
                "source_sha256": page[0],
                "page_idx": page[1],
                "source_page": document.source_page(page[1]),
                "cache_key": cache_key,
                "artifact_path": next(iter(paths)),
                "records": len(page_records),
                "available_variants": sorted(available[page]),
            }
        )
    for record in records:
        selected_ids.setdefault(record.raw_element_id, False)
        selection_reason.setdefault(record.raw_element_id, "non_selected_parse_variant")
    unregistered = sorted(
        {
            (record.source_sha256, record.page_idx)
            for record in records
            if record.source_sha256 not in documents
        }
    )
    return (
        selected,
        selected_ids,
        selection_reason,
        {
            "selected_pages": page_reports,
            "selected_records": len(selected),
            "all_records": len(records),
            "unregistered_pages": unregistered,
        },
    )


def build_tables(
    records: Sequence[RawRecord], mapping: MappingProfile
) -> list[TableModel]:
    grouped: dict[tuple[str, int, str, int], list[RawRecord]] = defaultdict(list)
    for record in records:
        grouped[
            (
                record.source_sha256,
                record.page_idx,
                record.cache_key,
                record.table_index,
            )
        ].append(record)
    return [
        analyze_table(grouped[key], mapping)
        for key in sorted(
            grouped, key=lambda item: (item[0], item[1], item[2], item[3])
        )
    ]


def extract_order_tokens(
    raw_record: RawRecord,
    mapping: MappingProfile,
    header: str | None = None,
) -> list[OrderToken]:
    text = str(raw_record.values.get("raw_value") or "").strip()
    if not text:
        return []
    configured = mapping.raw.get("identifiers", [])
    if configured:
        found = {}
        for identity in configured:
            if identity.get("raw_headers") and normalize_header(
                header or raw_record.values.get("raw_parameter_name")
            ) not in {normalize_header(value) for value in identity["raw_headers"]}:
                continue
            match = re.fullmatch(identity["pattern"], text)
            if match:
                article = match.groupdict().get("series") or identity.get(
                    "article_number"
                )
                order = match.group("order")
                if article in mapping.series and order:
                    found[(article, order)] = OrderToken(article, order, raw_record)
        if len(found) > 1:
            raise ConfigurationError(f"Ambiguous identifier patterns for {text!r}.")
        return list(found.values())
    articles = sorted(mapping.series, key=lambda value: (-len(value), value))
    if articles:
        pattern = re.compile(
            r"(?=(" + "|".join(re.escape(value) for value in articles) + r")\s+)"
        )
        matches = list(pattern.finditer(text))
        tokens: list[OrderToken] = []
        for index, match in enumerate(matches):
            start = match.start(1)
            end = matches[index + 1].start(1) if index + 1 < len(matches) else len(text)
            segment = text[start:end].strip()
            parsed = re.fullmatch(r"(\d{1,8})\s+(\d+(?:\.\d+))", segment)
            if parsed is None or parsed.group(1) not in mapping.series:
                continue
            order_number = f"{parsed.group(1)} {parsed.group(2)}"
            tokens.append(OrderToken(parsed.group(1), order_number, raw_record))
        if tokens:
            return tokens
    generic = re.fullmatch(r"(\d{3,8})\s+(\d+(?:\.\d+))", text)
    if generic and generic.group(1) in mapping.series:
        return [
            OrderToken(
                generic.group(1), f"{generic.group(1)} {generic.group(2)}", raw_record
            )
        ]
    return []


def table_order_tokens_by_row(
    table: TableModel,
    mapping: MappingProfile,
) -> tuple[dict[int, list[OrderToken]], list[OrderToken]]:
    per_row: dict[int, list[OrderToken]] = defaultdict(list)
    flattened: list[OrderToken] = []
    for row_index in table.data_rows:
        for record in _row_values(table.cells, row_index):
            header = _record_header(table, record)
            if any(
                normalize_header(header)
                in {normalize_header(value) for value in rule["raw_headers"]}
                for rule in mapping.raw.get("relation_rules", [])
            ):
                continue
            tokens = extract_order_tokens(record, mapping, header)
            per_row[row_index].extend(tokens)
            flattened.extend(tokens)
    return dict(per_row), flattened


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"-0", "+0", ""}:
        return "0"
    return rendered


def _parse_decimal(value: str) -> str:
    cleaned = value.strip().replace("−", "-").replace(",", ".")
    cleaned = re.sub(r"^(?:mm\s*|[øØ∅]\s*)", "", cleaned, flags=re.IGNORECASE)
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        raise ValueError(f"not an exact decimal: {value!r}")
    try:
        return _decimal_text(Decimal(cleaned))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def _normalize_source_value(raw_value: Any, rule: MappingRule) -> dict[str, Any]:
    value_text = None if raw_value is None else str(raw_value)
    result = {
        "value_type": rule.data_type,
        "value_text": value_text,
        "value_decimal": None,
        "value_min": None,
        "value_max": None,
        "tolerance_lower": None,
        "tolerance_upper": None,
        "fit_class": None,
    }
    if value_text is None or not value_text.strip():
        return result
    text = value_text.strip()
    if rule.data_type in {"string", "identifier", "enum", "boolean"}:
        if rule.allowed_values and text not in rule.allowed_values:
            raise ValueError(f"not an allowed value: {text!r}")
        if rule.data_type == "boolean":
            values = {
                "true": "true",
                "false": "false",
                "yes": "true",
                "no": "false",
                "ja": "true",
                "nein": "false",
                "да": "true",
                "нет": "false",
            }
            if text.casefold() not in values:
                raise ValueError(f"not an explicit boolean: {text!r}")
            result["value_text"] = values[text.casefold()]
        return result
    if rule.data_type == "integer":
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError(f"not an exact integer: {text!r}")
        result["value_decimal"] = str(int(text))
        return result
    if rule.allow_fit:
        fit = re.fullmatch(
            r"[Ø∅]?\s*([+-]?\d+(?:[.,]\d+)?)\s*([A-Za-z]{1,2}[1-9][0-9]?)", text
        )
        if fit:
            result["value_decimal"] = _parse_decimal(fit.group(1))
            result["fit_class"] = fit.group(2)
            return result
    if rule.data_type == "range":
        matched = re.fullmatch(
            r"\s*([+-]?\d+(?:[.,]\d+)?)\s*[-–—]\s*([+-]?\d+(?:[.,]\d+)?)\s*",
            text,
        )
        if not matched:
            raise ValueError(f"not an exact range: {text!r}")
        result["value_min"] = _parse_decimal(matched.group(1))
        result["value_max"] = _parse_decimal(matched.group(2))
        return result

    asymmetric = re.fullmatch(
        r"\s*([+-]?\d+(?:[.,]\d+)?)\s*([+-]\s*\d+(?:[.,]\d+)?)\s*(?:/|\s)\s*([+-]\s*\d+(?:[.,]\d+)?)\s*",
        text,
    )
    symmetric = re.fullmatch(
        r"\s*([+-]?\d+(?:[.,]\d+)?)\s*[±]\s*(\d+(?:[.,]\d+)?)\s*",
        text,
    )
    if asymmetric:
        result["value_decimal"] = _parse_decimal(asymmetric.group(1))
        offsets = [
            Decimal(re.sub(r"\s", "", asymmetric.group(index)).replace(",", "."))
            for index in (2, 3)
        ]
        result["tolerance_upper"] = _decimal_text(max(offsets))
        result["tolerance_lower"] = _decimal_text(min(offsets))
        return result
    if symmetric:
        result["value_decimal"] = _parse_decimal(symmetric.group(1))
        tolerance = _parse_decimal(symmetric.group(2)).lstrip("+").lstrip("-")
        result["tolerance_upper"] = tolerance
        result["tolerance_lower"] = "-" + tolerance
        return result
    result["value_decimal"] = _parse_decimal(text)
    return result


def normalize_value(raw_value: Any, rule: MappingRule) -> dict[str, Any]:
    result = _normalize_source_value(raw_value, rule)
    numeric = (
        "value_decimal",
        "value_min",
        "value_max",
        "tolerance_lower",
        "tolerance_upper",
    )
    if rule.data_type not in {"decimal", "integer", "range"} or all(
        result[key] is None for key in numeric
    ):
        return result
    units = {
        "mm": ("length", Decimal("1")),
        "cm": ("length", Decimal("10")),
        "m": ("length", Decimal("1000")),
        "in": ("length", Decimal("25.4")),
        "inch": ("length", Decimal("25.4")),
        "°": ("angle", Decimal("1")),
        "deg": ("angle", Decimal("1")),
    }
    if rule.source_unit != rule.canonical_unit:
        source, target = units.get(rule.source_unit), units.get(rule.canonical_unit)
        if source is None or target is None or source[0] != target[0]:
            raise ValueError(
                f"Unsupported unit conversion: {rule.source_unit} -> {rule.canonical_unit}"
            )
        factor = source[1] / target[1]
        for key in numeric:
            if result[key] is not None:
                result[key] = _decimal_text(Decimal(result[key]) * factor)
    if result["value_min"] is not None and Decimal(result["value_min"]) > Decimal(
        result["value_max"]
    ):
        raise ValueError("Range minimum exceeds maximum.")
    for key in ("value_decimal", "value_min", "value_max"):
        if result[key] is None:
            continue
        value = Decimal(result[key])
        if (rule.minimum is not None and value < Decimal(rule.minimum)) or (
            rule.maximum is not None and value > Decimal(rule.maximum)
        ):
            raise ValueError("Value is outside the approved mapping bounds.")
    return result


def provenance_row(
    record: RawRecord,
    *,
    manifest: Mapping[str, Any],
    document: DocumentInfo,
    logical_row_id: str,
    evidence_index: int,
    evidence_type: str,
) -> dict[str, Any]:
    source_page = document.source_page(record.page_idx)
    provenance_id = stable_id(
        "prov", record.raw_element_id, logical_row_id, evidence_index, evidence_type
    )
    return {
        "provenance_id": provenance_id,
        "evidence_index": evidence_index,
        "raw_element_id": record.raw_element_id,
        "logical_row_id": logical_row_id,
        "catalog_id": manifest["catalog_id"],
        "document_id": document.document_id,
        "parent_document_id": (
            f"doc_sha256_{document.original_source_sha256}"
            if document.original_source_sha256
            else None
        ),
        "source_file": record.values.get("source_file"),
        "source_sha256": record.source_sha256,
        "page": record.values.get("page"),
        "page_idx": record.page_idx,
        "source_page": source_page,
        "source_page_idx": source_page - 1 if source_page is not None else None,
        "catalog_page_label": None,
        "table_index": record.values.get("source_table_index", record.table_index),
        "row_index": record.row_index,
        "column_index": record.column_index,
        "bbox": record.values.get("bbox"),
        "bbox_scope": record.values.get("bbox_scope"),
        "bbox_units": record.values.get("bbox_units"),
        "raw_header_path": record.values.get("header_path"),
        "raw_parameter_name": record.values.get("raw_parameter_name"),
        "raw_value": record.values.get("raw_value"),
        "evidence": record.values.get("evidence")
        or str(record.values.get("raw_value") or ""),
        "evidence_type": "reviewed_reconstruction"
        if record.values.get("reconstruction_id")
        else evidence_type,
        "parser": record.values.get("parser"),
        "parser_version": record.values.get("parser_version"),
        "parser_backend": record.values.get("parser_backend"),
        "cache_key": record.cache_key,
        "artifact_path": record.values.get("artifact_path"),
    }


def _table_is_parameter_table(table: TableModel) -> bool:
    return bool(table.parameter_columns)


def _compatible_token_assignment(
    rows: Sequence[int], tokens: Sequence[OrderToken]
) -> dict[int, list[OrderToken]] | None:
    if not rows or not tokens:
        return None
    if len(tokens) == len(rows):
        return {row: [token] for row, token in zip(rows, tokens, strict=True)}
    if len(tokens) % len(rows) == 0:
        fanout = len(tokens) // len(rows)
        if 1 < fanout <= 4:
            return {
                row: list(tokens[index * fanout : (index + 1) * fanout])
                for index, row in enumerate(rows)
            }
    return None


def assign_table_identifiers(
    table: TableModel,
    page_tables: Sequence[TableModel],
    mapping: MappingProfile,
    used_companions: set[int],
) -> tuple[dict[int, list[OrderToken]], int | None, str | None]:
    per_row, flattened = table_order_tokens_by_row(table, mapping)
    # Row-local evidence takes precedence over counts: a missing code beside a
    # fan-out row must never shift the following codes to different products.
    if flattened:
        return (
            {row: list(per_row.get(row, [])) for row in table.data_rows},
            table.table_index,
            "same_table_sparse",
        )

    candidates: list[tuple[int, TableModel, dict[int, list[OrderToken]]]] = []
    for companion in page_tables:
        if (
            companion.table_index == table.table_index
            or companion.table_index in used_companions
        ):
            continue
        if _table_is_parameter_table(companion):
            continue
        _, companion_tokens = table_order_tokens_by_row(companion, mapping)
        assignment = _compatible_token_assignment(table.data_rows, companion_tokens)
        if assignment is None:
            continue
        distance = abs(companion.table_index - table.table_index)
        candidates.append((distance, companion, assignment))
    if not candidates:
        return {row: [] for row in table.data_rows}, None, None
    candidates.sort(key=lambda item: (item[0], item[1].table_index))
    best_distance = candidates[0][0]
    equally_near = [item for item in candidates if item[0] == best_distance]
    if len(equally_near) != 1:
        return {row: [] for row in table.data_rows}, None, "ambiguous_companion_table"
    _, companion, assignment = candidates[0]
    used_companions.add(companion.table_index)
    return assignment, companion.table_index, "companion_table_ordinal"


def _record_header(table: TableModel, record: RawRecord) -> str | None:
    parameter = record.values.get("raw_parameter_name")
    if parameter not in (None, ""):
        return str(parameter)
    return table.header_by_column.get(record.column_index)


def _review_for_record(
    record: RawRecord,
    openai_reviews: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return openai_reviews.get(record.openai_row_id)


def _accepted_raw_value(
    record: RawRecord,
    decision: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
) -> tuple[Any, str, str, str | None]:
    raw_value = record.values.get("raw_value")
    if decision is None:
        return raw_value, "rule", "deterministic FIELD_MAPPING applied to RAW", None
    action = str(decision["action"])
    decision_id = stable_id("decision", decision)
    if action == "accept_raw":
        return raw_value, "reviewer", str(decision["reason"]), decision_id
    if action == "accept_proposal":
        if review is None:
            raise ConfigurationError(
                f"Decision {decision_id} accepts an OpenAI proposal, but no review exists."
            )
        proposal = review.get("openai_processed_value")
        if (
            proposal in (None, "")
            or not str(review.get("openai_evidence") or "").strip()
        ):
            raise ConfigurationError(
                f"Decision {decision_id} accepts an empty OpenAI proposal."
            )
        return proposal, "reviewer", str(decision["reason"]), decision_id
    if action in {"reject", "not_applicable"}:
        return None, "reviewer", str(decision["reason"]), decision_id
    raise ConfigurationError(f"Unsupported review action: {action}")


def raw_header_path(record: RawRecord) -> list[str]:
    value = (
        record.values.get("header_path") or record.values.get("raw_header_path") or []
    )
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    return [str(part) for part in value] if isinstance(value, list) else []


def table_id(table: TableModel) -> str:
    return stable_id("table", table.source_sha256, table.page_idx, table.table_index)


def relation_rule(
    mapping: MappingProfile, family: str | None, header: str | None
) -> Mapping[str, Any] | None:
    matches = [
        rule
        for rule in mapping.raw.get("relation_rules", [])
        if family in rule["families"]
        and normalize_header(header)
        in {normalize_header(value) for value in rule["raw_headers"]}
    ]
    if len(matches) > 1:
        raise ConfigurationError(f"Ambiguous relation mapping for {family}/{header}.")
    return matches[0] if matches else None


def classify_row(table: TableModel, row: int, mapping: MappingProfile) -> str:
    records = _row_values(table.cells, row)
    text = " ".join(str(item.values.get("raw_value") or "") for item in records).strip()
    if not text:
        return "continuation"
    if (
        row == table.header_row
        or _is_unit_row(records)
        or (
            _header_score(records, mapping) >= 2
            and _header_score(records, mapping) * 2 >= len(records)
        )
    ):
        return "table_header"
    tokens = [
        token
        for item in records
        for token in extract_order_tokens(item, mapping, _record_header(table, item))
    ]
    if tokens:
        types = {mapping.series[token.article_number].item_type for token in tokens}
        return (
            next(iter(types))
            if len(types) == 1
            and next(iter(types)) in {"sku", "size_variant", "kit", "component"}
            else "sku"
        )
    if text in mapping.series:
        return "series_header"
    if text in mapping.required_fields:
        return "family_header"
    if any(
        normalize_header(_record_header(table, item))
        in {normalize_header(header) for header in rule["raw_headers"]}
        for item in records
        for rule in mapping.raw.get("relation_rules", [])
    ):
        return "compatibility_relation"
    if re.match(r"(?i)^(?:note|hinweis|achtung|примечание|сноска|[*†‡])", text):
        return "note"
    if re.search(r"(?i)^(?:continued|fortsetzung|продолжение)", text):
        return "continuation"
    if re.search(r"(?:€|\$|₽|\bEUR\b|\bUSD\b|\bRUB\b)", text):
        return "price"
    return "unknown"


def link_document_tables(
    tables: Sequence[TableModel],
    mapping: MappingProfile,
    documents: Mapping[str, DocumentInfo],
) -> list[dict[str, Any]]:
    """Inherit context only with adjacency, equal columns and source evidence.

    The last table of one page can precede the first of the next. Repeated
    headers must agree; a headerless continuation also needs equal horizontal
    geometry and a common series, or an explicit continuation marker.
    """
    pages: dict[tuple[str, int], list[TableModel]] = defaultdict(list)
    for table in tables:
        pages[(table.source_sha256, table.page_idx)].append(table)
        if table.header_row is not None:
            table.header_evidence = {
                item.column_index: item
                for item in _row_values(table.cells, table.header_row)
            }
        else:
            for item in table.cells.values():
                if item.values.get("raw_parameter_name") and raw_header_path(item):
                    table.header_evidence.setdefault(item.column_index, item)
    links = []
    for key in sorted(pages):
        current = min(pages[key], key=lambda item: item.table_index)
        previous_page = pages.get((key[0], key[1] - 1), [])
        if not previous_page:
            continue
        doc = documents[key[0]]
        if (
            doc.source_pages
            and doc.source_page(key[1]) != (doc.source_page(key[1] - 1) or 0) + 1
        ):
            continue
        previous = max(previous_page, key=lambda item: item.table_index)
        columns = {col for _, col in current.cells}
        if (
            columns != {col for _, col in previous.cells}
            or not previous.parameter_columns
        ):
            continue
        headers = {
            col: normalize_header(value)
            for col, value in previous.header_by_column.items()
        }
        current_headers = {
            col: normalize_header(value)
            for col, value in current.header_by_column.items()
        }
        if current_headers and current_headers != headers:
            continue

        def series_in(table: TableModel) -> set[str]:
            return {
                token.article_number
                for item in table.cells.values()
                for token in extract_order_tokens(
                    item,
                    mapping,
                    table.header_by_column.get(item.column_index)
                    or previous.header_by_column.get(item.column_index),
                )
            }

        same_series = bool(series_in(current) & series_in(previous))
        marked = any(
            re.search(
                r"(?i)continued|fortsetzung|продолжение",
                str(item.values.get("raw_value") or ""),
            )
            for item in current.cells.values()
        )

        def geometry(table: TableModel) -> tuple[float, float] | None:
            item = next(iter(table.cells.values()))
            bbox = item.values.get("bbox")
            try:
                bbox = json.loads(bbox) if isinstance(bbox, str) else bbox
                if len(bbox) == 4:
                    return float(bbox[0]), float(bbox[2])
            except (TypeError, ValueError):
                return None
            return None

        left, right = geometry(previous), geometry(current)
        same_geometry = (
            left is not None
            and right is not None
            and all(abs(a - b) <= 1 for a, b in zip(left, right))
        )
        repeated = bool(current_headers) and current_headers == headers
        if not (
            (repeated and same_series)
            or (not current_headers and same_geometry and (same_series or marked))
        ):
            continue
        current.continuation_of = table_id(previous)
        if not current_headers:
            current.header_by_column = dict(previous.header_by_column)
            current.parameter_columns = set(previous.parameter_columns)
            current.identifier_columns = set(previous.identifier_columns)
            current.header_evidence = dict(previous.header_evidence)
        links.append(
            {
                "table_id": table_id(current),
                "continues_table_id": table_id(previous),
                "source_sha256": key[0],
                "page_idx": key[1],
                "table_index": current.table_index,
                "basis": "repeated_headers_and_series"
                if repeated
                else "geometry_and_context",
                "header_raw_element_ids": [
                    item.raw_element_id for item in current.header_evidence.values()
                ],
            }
        )
    for table in tables:
        table.row_types = {
            row: classify_row(table, row, mapping)
            for row in sorted({r for r, _ in table.cells})
        }
        table.data_rows = [
            row
            for row in table.data_rows
            if table.row_types[row]
            not in {
                "family_header",
                "series_header",
                "table_header",
                "note",
                "price",
                "continuation",
            }
        ]
    return links


def reconstruct_relations(
    products: Sequence[ProductCandidate],
    tables: Sequence[TableModel],
    mapping: MappingProfile,
    manifest: Mapping[str, Any],
    documents: Mapping[str, DocumentInfo],
    decisions: Mapping[str, Mapping[str, Any]],
    issues: IssueRegistry,
    provenance: list[dict[str, Any]],
    usage: dict[str, list[str]],
) -> list[dict[str, Any]]:
    by_order: dict[str, list[ProductCandidate]] = defaultdict(list)
    for product in products:
        if product.order_number:
            by_order[product.order_number].append(product)
    by_table = {
        (table.source_sha256, table.page_idx, table.table_index): table
        for table in tables
    }
    relations = []
    for product in products:
        table = by_table[(product.source_sha256, product.page_idx, product.table_index)]
        for record in _row_values(table.cells, product.row_index):
            rule = relation_rule(mapping, product.family, _record_header(table, record))
            if rule is None or record.values.get("raw_value") in (None, ""):
                continue
            decision = decisions.get(record.raw_element_id)
            if decision and decision["action"] in {"reject", "not_applicable"}:
                continue
            targets = str(record.values["raw_value"]).split(rule.get("separator", ";"))
            for reference in targets:
                reference = reference.strip()
                targets_found = by_order.get(reference, [])
                evidence = provenance_row(
                    record,
                    manifest=manifest,
                    document=documents[record.source_sha256],
                    logical_row_id=product.logical_row_id,
                    evidence_index=len(provenance),
                    evidence_type="relation",
                )
                provenance.append(evidence)
                usage[record.raw_element_id].append(product.logical_row_id)
                accepted = not rule.get("review_required", True) or bool(
                    decision and decision["action"] == "accept_raw"
                )
                target = targets_found[0] if len(targets_found) == 1 else None
                if target is None or not accepted:
                    product.issue_ids.append(
                        issues.add(
                            entity_type="logical_row",
                            entity_id=product.logical_row_id,
                            code="relation_target_unresolved"
                            if target is None
                            else "relation_requires_review",
                            severity="critical",
                            blocks_publication=True,
                            message=f"{rule['relation_type']} target {reference!r} requires review.",
                            provenance_id=evidence["provenance_id"],
                        )
                    )
                relations.append(
                    {
                        "relation_id": stable_id(
                            "relation",
                            product.product_version_id,
                            rule["relation_type"],
                            reference,
                            record.raw_element_id,
                        ),
                        "source_product_version_id": product.product_version_id,
                        "relation_type": rule["relation_type"],
                        "target_product_key": target.product_key if target else None,
                        "target_product_version_id": target.product_version_id
                        if target
                        else None,
                        "target_external_reference": None if target else reference,
                        "criticality": "hard",
                        "confidence": "rule_based",
                        "review_status": "accepted"
                        if accepted and target
                        else "requires_review",
                        "review_decision_id": stable_id("decision", decision)
                        if decision
                        else None,
                        "provenance_id": evidence["provenance_id"],
                        "catalog_id": manifest["catalog_id"],
                        "catalog_revision": manifest["revision"],
                        "direction": "source_to_target",
                    }
                )
    return relations


def validate_relation_publication(
    products: Sequence[ProductCandidate],
    relations: Sequence[Mapping[str, Any]],
    issues: IssueRegistry,
) -> None:
    by_version = {item.product_version_id: item for item in products}
    while True:
        changed = False
        for relation in relations:
            source = by_version.get(relation["source_product_version_id"])
            target = by_version.get(relation["target_product_version_id"])
            if (
                source
                and source.publication_status == "published"
                and (target is None or target.publication_status != "published")
            ):
                source.issue_ids.append(
                    issues.add(
                        entity_type="logical_row",
                        entity_id=source.logical_row_id,
                        code="relation_target_blocked",
                        severity="critical",
                        blocks_publication=True,
                        message="Required relation points to a product withheld from publication.",
                    )
                )
                source.publication_status = "requires_review"
                changed = True
        if not changed:
            break


def collect_context_records(
    records: Sequence[RawRecord], raw_root: Path
) -> list[RawRecord]:
    """Keep non-table MinerU evidence alongside RAW without rewriting the workbook."""
    artifacts: dict[str, RawRecord] = {}
    for record in records:
        artifacts.setdefault(str(record.values.get("artifact_path") or ""), record)
    context = []
    for supplied, exemplar in sorted(artifacts.items()):
        path = Path(supplied)
        if not path.is_file() and path.is_absolute() and path.is_relative_to("/parsed"):
            path = raw_root / path.relative_to("/parsed")
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        manifest_path, artifact_manifest = export_raw._nearest_manifest(path, raw_root)
        metadata = export_raw._metadata_from_manifest(
            artifact_manifest, manifest_path, path.stem
        )
        observed = {
            block.get("source_page_idx", block.get("page_idx"))
            for block in payload
            if isinstance(block, Mapping)
        }
        notes = []
        for block in payload:
            if not isinstance(block, Mapping):
                continue
            for field_name in ("table_footnote", "table_caption"):
                fragments = block.get(field_name, [])
                if isinstance(fragments, str):
                    fragments = [fragments]
                if isinstance(fragments, list):
                    notes.extend(
                        {**block, "type": "note", "text": text}
                        for text in fragments
                        if isinstance(text, str) and text.strip()
                    )
        payload = [*payload, *notes]
        for index, block in enumerate(payload):
            if not isinstance(block, Mapping) or block.get("type") == "table":
                continue
            page = block.get("source_page_idx", block.get("page_idx"))
            if metadata.page_idx is not None and len(observed) <= 1:
                page = metadata.page_idx
            if not isinstance(page, int):
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            values = {
                **exemplar.values,
                "page_idx": page,
                "page": page + 1,
                "table_index": -1,
                "row_index": index,
                "column_index": 0,
                "raw_parameter_name": None,
                "header_path": None,
                "raw_value": text,
                "bbox": block.get("bbox"),
                "bbox_scope": "block",
            }
            raw_id = stable_id(
                "raw",
                exemplar.source_sha256,
                exemplar.cache_key,
                page,
                -1,
                index,
                0,
                supplied,
                None,
                text,
            )
            context.append(
                RawRecord(
                    "context",
                    index,
                    values,
                    raw_id,
                    stable_id("context_review", raw_id),
                )
            )
    selected_pages = {(record.source_sha256, record.page_idx) for record in records}
    return [
        record
        for record in context
        if (record.source_sha256, record.page_idx) in selected_pages
    ]


def context_attributes(
    candidate: ProductCandidate,
    records: Sequence[RawRecord],
    mapping: MappingProfile,
    manifest: Mapping[str, Any],
    document: DocumentInfo,
    decisions: Mapping[str, Mapping[str, Any]],
    issues: IssueRegistry,
    provenance: dict[str, dict[str, Any]],
    usage: dict[str, list[str]],
    updates: dict[str, dict[str, Any]],
) -> None:
    for config in mapping.raw.get("context_rules", []):
        if (
            candidate.family not in config["families"]
            or candidate.article_number not in config["series"]
        ):
            continue
        for record in records:
            if (record.source_sha256, record.page_idx) != (
                candidate.source_sha256,
                candidate.page_idx,
            ):
                continue
            match = re.fullmatch(
                config["pattern"], str(record.values.get("raw_value") or "")
            )
            if not match:
                continue
            decision = decisions.get(record.raw_element_id)
            if decision and decision["action"] in {"reject", "not_applicable"}:
                continue
            rule = MappingRule(
                id=config["id"],
                families=frozenset(config["families"]),
                series=frozenset(config["series"]),
                raw_headers=(),
                normalized_headers=frozenset(),
                canonical_field=config["canonical_field"],
                data_type=config["data_type"],
                source_unit=config.get("source_unit"),
                canonical_unit=config.get("canonical_unit"),
                transform=config.get("transform", "identity"),
                criticality=config["criticality"],
                review_required=config.get("review_required", True),
                matcher_column=config.get("matcher_column"),
                minimum=config.get("minimum"),
                maximum=config.get("maximum"),
                allowed_values=tuple(config.get("allowed_values", [])),
                allow_fit=bool(config.get("allow_fit", False)),
            )
            evidence = provenance_row(
                record,
                manifest=manifest,
                document=document,
                logical_row_id=candidate.logical_row_id,
                evidence_index=len(candidate.attributes) + 1,
                evidence_type="page_context",
            )
            provenance[evidence["provenance_id"]] = evidence
            usage[record.raw_element_id].append(candidate.logical_row_id)
            try:
                normalized = normalize_value(match.group("value"), rule)
            except ValueError as exc:
                candidate.issue_ids.append(
                    issues.add(
                        entity_type="logical_row",
                        entity_id=candidate.logical_row_id,
                        code="context_value_invalid",
                        severity="critical",
                        blocks_publication=True,
                        message=str(exc),
                        provenance_id=evidence["provenance_id"],
                    )
                )
                continue
            candidate.attributes.append(
                AttributeCandidate(
                    record,
                    config["pattern"],
                    rule,
                    normalized,
                    evidence["provenance_id"],
                )
            )
            updates[record.raw_element_id] = {
                "mapping_rule_id": rule.id,
                "canonical_field": rule.canonical_field,
                "accepted_value": match.group("value"),
                "decision_source": "reviewer" if decision else "rule",
                "decision_reason": decision["reason"]
                if decision
                else "Explicit page-context mapping with series filter.",
                "review_decision_id": stable_id("decision", decision)
                if decision
                else None,
                "review_status": "accepted" if decision else "accepted_by_rule",
            }
            if rule.review_required and not decision:
                candidate.issue_ids.append(
                    issues.add(
                        entity_type="logical_row",
                        entity_id=candidate.logical_row_id,
                        code="context_requires_review",
                        severity="critical",
                        blocks_publication=True,
                        message=f"Context rule {rule.id} needs a reviewer decision.",
                        provenance_id=evidence["provenance_id"],
                    )
                )


def reconstruct_catalog(
    *,
    manifest: Mapping[str, Any],
    documents: Mapping[str, DocumentInfo],
    mapping: MappingProfile,
    tables: Sequence[TableModel],
    openai_reviews: Mapping[str, Mapping[str, Any]],
    decisions: Mapping[str, Mapping[str, Any]],
    issues: IssueRegistry,
    selected_records: Sequence[RawRecord],
    context_records: Sequence[RawRecord] = (),
) -> tuple[
    list[ProductCandidate],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[str]],
]:
    catalog_id = str(manifest["catalog_id"])
    manufacturer = str(manifest["manufacturer"])
    global_blockers: list[str] = []

    if manifest["catalog_role"] == "UNASSIGNED":
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="catalog_role_unassigned",
                severity="critical",
                blocks_publication=True,
                message="Catalog role SOURCE/TARGET is not approved.",
            )
        )
    if manifest["status"] != "published":
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="catalog_manifest_not_published",
                severity="critical",
                blocks_publication=True,
                message=f"Catalog manifest status is {manifest['status']!r}, not 'published'.",
            )
        )
    if manifest["revision"] == "unknown":
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="catalog_revision_unknown",
                severity="critical",
                blocks_publication=True,
                message="Catalog revision must be confirmed before publication.",
            )
        )
    if (
        manifest["matcher_contract"]["status"] != "approved"
        or mapping.raw["matcher_contract"]["status"] != "approved"
    ):
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="matcher_contract_unapproved",
                severity="critical",
                blocks_publication=True,
                message="The .xlam matcher contract is provisional.",
            )
        )
    for blocker in manifest.get("publication_blockers") or []:
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="manifest_publication_blocker",
                severity="critical",
                blocks_publication=True,
                message=str(blocker),
            )
        )
    if manifest["review_policy"] == "required_for_publish" and not openai_reviews:
        global_blockers.append(
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="row_validation_not_run",
                severity="critical",
                blocks_publication=True,
                message="OpenAI row validation is required for publication but no approved run was supplied.",
            )
        )
    selected_by_raw_id = {record.raw_element_id: record for record in selected_records}
    usage: dict[str, list[str]] = defaultdict(list)
    review_updates: dict[str, dict[str, Any]] = {}
    provenance_by_id: dict[str, dict[str, Any]] = {}
    products: list[ProductCandidate] = []

    tables_by_page: dict[tuple[str, int, str], list[TableModel]] = defaultdict(list)
    for table in tables:
        tables_by_page[(table.source_sha256, table.page_idx, table.cache_key)].append(
            table
        )

    for page_key in sorted(tables_by_page):
        page_tables = sorted(
            tables_by_page[page_key], key=lambda item: item.table_index
        )
        used_companions: set[int] = set()
        for table in page_tables:
            if not _table_is_parameter_table(table):
                continue
            identifier_assignment, identifier_table, assignment_basis = (
                assign_table_identifiers(table, page_tables, mapping, used_companions)
            )
            document = documents[table.source_sha256]
            if assignment_basis == "ambiguous_companion_table":
                issues.add(
                    entity_type="table",
                    entity_id=f"{table.source_sha256}:{table.page_idx}:{table.table_index}",
                    code="ambiguous_identifier_table",
                    severity="critical",
                    blocks_publication=True,
                    message="More than one companion identifier table matches this parameter table.",
                )
            for row_index in table.data_rows:
                tokens = identifier_assignment.get(row_index, [])
                reviewed_tokens = []
                for token in tokens:
                    decision = decisions.get(token.raw_record.raw_element_id)
                    if decision:
                        value, _, _, _ = _accepted_raw_value(
                            token.raw_record,
                            decision,
                            openai_reviews.get(token.raw_record.openai_row_id),
                        )
                        if value is None:
                            continue
                        if decision["action"] == "accept_proposal":
                            from dataclasses import replace

                            proxy = replace(
                                token.raw_record,
                                values={**token.raw_record.values, "raw_value": value},
                            )
                            reviewed_tokens.extend(
                                OrderToken(
                                    item.article_number,
                                    item.order_number,
                                    token.raw_record,
                                )
                                for item in extract_order_tokens(
                                    proxy,
                                    mapping,
                                    _record_header(table, token.raw_record),
                                )
                            )
                            continue
                    reviewed_tokens.append(token)
                tokens = reviewed_tokens
                if not tokens:
                    tokens = [
                        OrderToken("", "", _row_values(table.cells, row_index)[0])
                    ]
                for token_index, token in enumerate(tokens):
                    article_number = token.article_number or None
                    order_number = token.order_number or None
                    series = mapping.series.get(article_number or "")
                    logical_row_id = stable_id(
                        "row",
                        catalog_id,
                        table.source_sha256,
                        table.page_idx,
                        table.cache_key,
                        table.table_index,
                        row_index,
                        token_index,
                        order_number,
                    )
                    candidate = ProductCandidate(
                        logical_row_id=logical_row_id,
                        article_number=article_number,
                        order_number=order_number,
                        family=series.family if series else None,
                        subfamily=series.subfamily if series else None,
                        item_type=series.item_type if series else "unknown",
                        source_sha256=table.source_sha256,
                        page_idx=table.page_idx,
                        table_index=table.table_index,
                        row_index=row_index,
                        identifier_record=token.raw_record if order_number else None,
                    )
                    if not order_number:
                        candidate.issue_ids.append(
                            issues.add(
                                entity_type="logical_row",
                                entity_id=logical_row_id,
                                code="identity_provisional",
                                severity="critical",
                                blocks_publication=True,
                                message="No unambiguous Bestell-Nr. could be assigned to this technical row.",
                            )
                        )
                    if series is None:
                        candidate.issue_ids.append(
                            issues.add(
                                entity_type="logical_row",
                                entity_id=logical_row_id,
                                code="family_unknown",
                                severity="critical",
                                blocks_publication=True,
                                message=f"No approved series/family mapping exists for article {article_number!r}.",
                            )
                        )

                    if candidate.identifier_record is not None:
                        if (
                            assignment_basis == "companion_table_ordinal"
                            and decisions.get(
                                candidate.identifier_record.raw_element_id, {}
                            ).get("action")
                            != "accept_raw"
                        ):
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="identifier_assignment_requires_review",
                                    severity="critical",
                                    blocks_publication=True,
                                    message="Identifier assignment across separate tables needs a reviewer decision.",
                                )
                            )
                        id_provenance = provenance_row(
                            candidate.identifier_record,
                            manifest=manifest,
                            document=document,
                            logical_row_id=logical_row_id,
                            evidence_index=0,
                            evidence_type="order_number",
                        )
                        provenance_by_id[id_provenance["provenance_id"]] = id_provenance
                        usage[candidate.identifier_record.raw_element_id].append(
                            logical_row_id
                        )
                        review = _review_for_record(
                            candidate.identifier_record, openai_reviews
                        )
                        if (
                            review
                            and review.get("openai_verdict")
                            in {"suspicious", "incorrect", "missing_context"}
                            and not (
                                decisions.get(
                                    candidate.identifier_record.raw_element_id, {}
                                ).get("action")
                                in {"accept_raw", "accept_proposal"}
                            )
                        ):
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="openai_identifier_warning",
                                    severity="critical",
                                    blocks_publication=True,
                                    message=(
                                        f"OpenAI verdict for identifier is {review.get('openai_verdict')}: "
                                        + "; ".join(review.get("openai_issues") or [])
                                    ),
                                    provenance_id=id_provenance["provenance_id"],
                                )
                            )

                    row_records = _row_values(table.cells, row_index)
                    for record in row_records:
                        if record.raw_element_id not in selected_by_raw_id:
                            continue
                        value = record.values.get("raw_value")
                        if (
                            value in (None, "")
                            and record.raw_element_id not in decisions
                        ):
                            continue
                        if extract_order_tokens(record, mapping):
                            continue
                        raw_header = _record_header(table, record)
                        if identifier_kind(raw_header):
                            continue
                        if (
                            table.continuation_of
                            and record.column_index in table.header_evidence
                        ):
                            header_record = table.header_evidence[record.column_index]
                            header_provenance = provenance_row(
                                header_record,
                                manifest=manifest,
                                document=document,
                                logical_row_id=logical_row_id,
                                evidence_index=len(candidate.attributes) + 1,
                                evidence_type="inherited_header",
                            )
                            provenance_by_id[header_provenance["provenance_id"]] = (
                                header_provenance
                            )
                            usage[header_record.raw_element_id].append(logical_row_id)
                        if (
                            relation_rule(mapping, candidate.family, raw_header)
                            is not None
                        ):
                            continue
                        inherited = table.header_evidence.get(record.column_index)
                        rule = mapping.resolve(
                            candidate.family,
                            candidate.article_number,
                            raw_header,
                            raw_header_path(record)
                            or (raw_header_path(inherited) if inherited else []),
                        )
                        if rule is None:
                            provenance = provenance_row(
                                record,
                                manifest=manifest,
                                document=document,
                                logical_row_id=logical_row_id,
                                evidence_index=len(candidate.attributes) + 1,
                                evidence_type="unmapped_raw_cell",
                            )
                            provenance_by_id[provenance["provenance_id"]] = provenance
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="mapping_missing"
                                    if raw_header
                                    else "header_missing",
                                    severity="critical",
                                    blocks_publication=True,
                                    message=(
                                        f"No contextual FIELD_MAPPING for raw header {raw_header!r}."
                                        if raw_header
                                        else f"No reliable header for column {record.column_index}."
                                    ),
                                    provenance_id=provenance["provenance_id"],
                                )
                            )
                            usage[record.raw_element_id].append(logical_row_id)
                            continue
                        review = _review_for_record(record, openai_reviews)
                        decision = decisions.get(record.raw_element_id)
                        (
                            accepted_value,
                            decision_source,
                            decision_reason,
                            decision_id,
                        ) = _accepted_raw_value(record, decision, review)
                        if accepted_value is None:
                            review_updates[record.raw_element_id] = {
                                "mapping_rule_id": rule.id,
                                "canonical_field": rule.canonical_field,
                                "accepted_value": None,
                                "decision_source": decision_source,
                                "decision_reason": decision_reason,
                                "review_decision_id": decision_id,
                                "review_status": decision["action"]
                                if decision
                                else "requires_review",
                            }
                            if decision and decision["action"] == "reject":
                                candidate.issue_ids.append(
                                    issues.add(
                                        entity_type="logical_row",
                                        entity_id=logical_row_id,
                                        code="attribute_rejected",
                                        severity="critical"
                                        if rule.criticality == "hard"
                                        else "warning",
                                        blocks_publication=rule.criticality == "hard",
                                        message=f"Reviewer rejected field {rule.canonical_field}.",
                                    )
                                )
                            continue
                        try:
                            normalized = normalize_value(accepted_value, rule)
                        except ValueError as exc:
                            provenance = provenance_row(
                                record,
                                manifest=manifest,
                                document=document,
                                logical_row_id=logical_row_id,
                                evidence_index=len(candidate.attributes) + 1,
                                evidence_type="attribute",
                            )
                            provenance_by_id[provenance["provenance_id"]] = provenance
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="value_type_error",
                                    severity="critical"
                                    if rule.criticality == "hard"
                                    else "warning",
                                    blocks_publication=rule.criticality == "hard",
                                    message=f"{rule.canonical_field}: {exc}",
                                    provenance_id=provenance["provenance_id"],
                                )
                            )
                            usage[record.raw_element_id].append(logical_row_id)
                            continue
                        provenance = provenance_row(
                            record,
                            manifest=manifest,
                            document=document,
                            logical_row_id=logical_row_id,
                            evidence_index=len(candidate.attributes) + 1,
                            evidence_type="attribute",
                        )
                        if (
                            decision_source == "reviewer"
                            and decision
                            and decision["action"] == "accept_proposal"
                        ):
                            provenance["evidence"] = review.get("openai_evidence")
                            provenance["evidence_type"] = "reviewed_attribute"
                        provenance_by_id[provenance["provenance_id"]] = provenance
                        attribute = AttributeCandidate(
                            raw_record=record,
                            raw_header=str(raw_header),
                            rule=rule,
                            normalized=normalized,
                            provenance_id=provenance["provenance_id"],
                        )
                        candidate.attributes.append(attribute)
                        usage[record.raw_element_id].append(logical_row_id)
                        review_status = "accepted_by_rule"
                        if decision is not None:
                            review_status = (
                                "accepted"
                                if decision["action"].startswith("accept")
                                else str(decision["action"])
                            )
                        elif (
                            review is not None and review.get("openai_verdict") == "ok"
                        ):
                            review_status = "validated_raw"
                        review_updates[record.raw_element_id] = {
                            "mapping_rule_id": rule.id,
                            "canonical_field": rule.canonical_field,
                            "accepted_value": accepted_value,
                            "decision_source": decision_source,
                            "decision_reason": decision_reason,
                            "review_decision_id": decision_id,
                            "review_status": review_status,
                        }
                        if rule.review_required and decision is None:
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="mapping_requires_review",
                                    severity="critical",
                                    blocks_publication=True,
                                    message=f"Mapping rule {rule.id} requires an explicit reviewer decision.",
                                    provenance_id=provenance["provenance_id"],
                                )
                            )
                        if (
                            review is not None
                            and review.get("openai_verdict")
                            in {"suspicious", "incorrect", "missing_context"}
                            and not (
                                decision
                                and decision["action"]
                                in {"accept_raw", "accept_proposal"}
                            )
                        ):
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="openai_attribute_warning",
                                    severity="critical"
                                    if rule.criticality == "hard"
                                    else "warning",
                                    blocks_publication=rule.criticality == "hard",
                                    message=(
                                        f"OpenAI verdict for {rule.canonical_field} is {review.get('openai_verdict')}: "
                                        + "; ".join(review.get("openai_issues") or [])
                                    ),
                                    provenance_id=provenance["provenance_id"],
                                )
                            )

                    context_attributes(
                        candidate,
                        context_records,
                        mapping,
                        manifest,
                        document,
                        decisions,
                        issues,
                        provenance_by_id,
                        usage,
                        review_updates,
                    )
                    canonical_fields = [
                        attribute.rule.canonical_field
                        for attribute in candidate.attributes
                    ]
                    for field_name, count in Counter(canonical_fields).items():
                        if count > 1:
                            values = {
                                str(attribute.normalized.get("value_text"))
                                for attribute in candidate.attributes
                                if attribute.rule.canonical_field == field_name
                            }
                            if len(values) > 1:
                                candidate.issue_ids.append(
                                    issues.add(
                                        entity_type="logical_row",
                                        entity_id=logical_row_id,
                                        code="conflicting_attribute",
                                        severity="critical",
                                        blocks_publication=True,
                                        message=f"Multiple conflicting values for {field_name}: {sorted(values)}",
                                    )
                                )
                    if candidate.family:
                        confirmed_na = {
                            value["canonical_field"]
                            for record_id, value in review_updates.items()
                            if value.get("review_status") == "not_applicable"
                            and record_id
                            in {record.raw_element_id for record in row_records}
                        }
                        missing_required = (
                            mapping.required_fields[candidate.family]
                            - set(canonical_fields)
                            - confirmed_na
                        )
                        for field_name in sorted(missing_required):
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="critical_field_missing",
                                    severity="critical",
                                    blocks_publication=True,
                                    message=f"Required field {field_name!r} is missing for family {candidate.family!r}.",
                                )
                            )
                    if manifest["review_policy"] == "required_for_publish":
                        used_records = [
                            attribute.raw_record for attribute in candidate.attributes
                        ]
                        if candidate.identifier_record is not None:
                            used_records.append(candidate.identifier_record)
                        if any(
                            record.openai_row_id not in openai_reviews
                            for record in used_records
                        ):
                            candidate.issue_ids.append(
                                issues.add(
                                    entity_type="logical_row",
                                    entity_id=logical_row_id,
                                    code="row_validation_missing",
                                    severity="critical",
                                    blocks_publication=True,
                                    message="Not every published field has a matching OpenAI row-validation result.",
                                )
                            )

                    if candidate.order_number and candidate.family:
                        candidate.product_key = stable_id(
                            "product",
                            manufacturer.casefold(),
                            candidate.family,
                            candidate.order_number,
                        )
                        candidate.product_version_id = stable_id(
                            "product_version",
                            candidate.product_key,
                            catalog_id,
                            manifest["revision"],
                        )
                    else:
                        candidate.product_key = stable_id(
                            "provisional_product", logical_row_id
                        )
                        candidate.product_version_id = stable_id(
                            "provisional_version", candidate.product_key, catalog_id
                        )
                    has_local_blocker = any(
                        issues.is_blocking(issue_id) for issue_id in candidate.issue_ids
                    )
                    candidate.publication_status = (
                        "published"
                        if not global_blockers and not has_local_blocker
                        else "requires_review"
                    )
                    products.append(candidate)

    # Duplicate identities are visible and withheld rather than silently merged.
    by_identity: dict[tuple[str, str], list[ProductCandidate]] = defaultdict(list)
    for candidate in products:
        if candidate.order_number and candidate.family:
            by_identity[(candidate.family, candidate.order_number)].append(candidate)
    for identity, candidates in by_identity.items():
        if len(candidates) <= 1:
            continue
        for candidate in candidates:
            candidate.issue_ids.append(
                issues.add(
                    entity_type="product",
                    entity_id=candidate.product_key or candidate.logical_row_id,
                    code="duplicate_identity",
                    severity="critical",
                    blocks_publication=True,
                    message=f"Duplicate product identity {identity[0]} / {identity[1]} occurs {len(candidates)} times.",
                )
            )
            candidate.publication_status = "requires_review"

    return products, list(provenance_by_id.values()), issues.rows, review_updates, usage


LOGICAL_ROW_COLUMNS = [
    "logical_row_id",
    "product_key",
    "product_version_id",
    "source_sha256",
    "page_idx",
    "source_page",
    "table_index",
    "row_index",
    "article_number",
    "order_number",
    "family",
    "item_type",
    "attribute_count",
    "publication_status",
    "blocking_issue_codes",
]

STRUCTURED_BASE_COLUMNS = [
    "product_key",
    "product_version_id",
    "catalog_id",
    "catalog_role",
    "catalog_revision",
    "manufacturer",
    "family",
    "subfamily",
    "series",
    "item_type",
    "article_number",
    "order_number",
    "publication_status",
    "logical_row_id",
    "source_file",
    "source_sha256",
    "source_page",
    "table_index",
    "row_index",
    "blocking_issue_codes",
]

BLOCKED_MATCHER_EXTRA_COLUMNS = ["blocked_reason"]

OUTPUT_FILENAMES = (
    "master_reviewed.xlsx",
    "master_canonical.xlsx",
    "master_structured.xlsx",
    "master_matcher.xlsx",
)


def _candidate_issue_rows(
    candidate: ProductCandidate,
    issue_rows: Sequence[Mapping[str, Any]],
    *,
    include_catalog: bool = True,
) -> list[Mapping[str, Any]]:
    ids = set(candidate.issue_ids)
    entity_ids = {
        candidate.logical_row_id,
        candidate.product_key,
        candidate.product_version_id,
    }
    return [
        row
        for row in issue_rows
        if row.get("issue_id") in ids
        or row.get("entity_id") in entity_ids
        or (include_catalog and row.get("entity_type") == "catalog")
    ]


def _blocking_issue_codes(
    candidate: ProductCandidate,
    issue_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    return sorted(
        {
            str(row["issue_code"])
            for row in _candidate_issue_rows(candidate, issue_rows)
            if bool(row.get("blocks_publication")) and row.get("status") == "open"
        }
    )


def apply_golden_expectations(
    *,
    manifest: Mapping[str, Any],
    products: Sequence[ProductCandidate],
    issues: IssueRegistry,
) -> dict[str, Any]:
    """Run the manifest-pinned page completeness gate.

    Expectations are deliberately metadata, not inferred values.  The gate only
    counts unambiguous order numbers reconstructed from the selected parser
    attempts and reports differences; it never synthesizes a missing product.
    """

    catalog_id = str(manifest["catalog_id"])
    expectation = manifest.get("golden_expectations")
    if not isinstance(expectation, Mapping):
        issues.add(
            entity_type="catalog",
            entity_id=catalog_id,
            code="completeness_not_checked",
            severity="critical",
            blocks_publication=True,
            message="No golden_expectations contract is registered for this catalog import.",
        )
        return {
            "status": "not_configured",
            "pages": [],
            "expected_total": None,
            "detected_total": 0,
        }

    page_items = expectation.get("pages") or []
    source_page_lookup: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for document in manifest.get("documents") or []:
        for document_page_idx, source_page in enumerate(
            document.get("source_pages") or []
        ):
            source_page_lookup[int(source_page)].append(
                (str(document["sha256"]), document_page_idx)
            )
    by_page: dict[tuple[str, int], set[str]] = defaultdict(set)
    for candidate in products:
        if candidate.order_number:
            by_page[(candidate.source_sha256, candidate.page_idx)].add(
                candidate.order_number
            )

    page_reports: list[dict[str, Any]] = []
    all_passed = True
    checked_pages: set[tuple[str, int]] = set()
    for item in page_items:
        if item.get("source_sha256") is not None:
            sha = str(item["source_sha256"])
            page_idx = int(item["page_idx"])
        else:
            matches = source_page_lookup.get(int(item["source_page"]), [])
            if len(matches) != 1:
                raise ConfigurationError(
                    f"golden_expectations source_page={item['source_page']} resolves to {len(matches)} registered pages."
                )
            sha, page_idx = matches[0]
        if (
            sha not in {doc["sha256"] for doc in manifest["documents"]}
            or not any(
                doc["sha256"] == sha and 0 <= page_idx < doc["pages"]
                for doc in manifest["documents"]
            )
            or (sha, page_idx) in checked_pages
        ):
            raise ConfigurationError(
                "Golden page is unregistered, out of bounds, or duplicated."
            )
        checked_pages.add((sha, page_idx))
        expected = int(item.get("expected_skus", item.get("expected_products", 0)))
        detected_values = sorted(by_page.get((sha, page_idx), set()))
        detected = len(detected_values)
        missing_known = [
            str(value)
            for value in item.get("known_missing")
            or item.get("known_missing_order_numbers")
            or []
            if str(value) not in detected_values
        ]
        passed = detected == expected and not missing_known
        if not passed:
            all_passed = False
            issues.add(
                entity_type="catalog",
                entity_id=catalog_id,
                code="page_coverage_incomplete",
                severity="critical",
                blocks_publication=True,
                message=(
                    f"Selected page {sha[:12]}:{page_idx} yielded {detected}/{expected} "
                    f"unique Bestell-Nr.; registered missing={missing_known or 'unknown'}."
                ),
            )
        page_reports.append(
            {
                "source_sha256": sha,
                "page_idx": page_idx,
                "source_page": item.get("source_page"),
                "expected_skus": expected,
                "detected_skus": detected,
                "known_missing": missing_known,
                "passed": passed,
            }
        )

    expected_pages = {
        (doc["sha256"], page)
        for doc in manifest["documents"]
        for page in range(doc["pages"])
    }
    if checked_pages != expected_pages:
        all_passed = False
        issues.add(
            entity_type="catalog",
            entity_id=catalog_id,
            code="golden_page_coverage_incomplete",
            severity="critical",
            blocks_publication=True,
            message="Golden counts do not cover every registered page.",
        )
    expected_total = int(
        expectation.get(
            "expected_total_skus",
            expectation.get(
                "total_expected",
                expectation.get(
                    "total_products", sum(row["expected_skus"] for row in page_reports)
                ),
            ),
        )
    )
    detected_total = len(
        {candidate.order_number for candidate in products if candidate.order_number}
    )
    if detected_total != expected_total:
        all_passed = False
        issues.add(
            entity_type="catalog",
            entity_id=catalog_id,
            code="golden_set_not_passed",
            severity="critical",
            blocks_publication=True,
            message=f"Golden set yielded {detected_total}/{expected_total} unique Bestell-Nr.",
        )
    return {
        "status": "passed" if all_passed else "failed",
        "pages": page_reports,
        "expected_total": expected_total,
        "detected_total": detected_total,
    }


def finalize_publication_statuses(
    products: Sequence[ProductCandidate],
    issue_rows: Sequence[Mapping[str, Any]],
) -> None:
    for candidate in products:
        candidate.publication_status = (
            "requires_review"
            if _blocking_issue_codes(candidate, issue_rows)
            else "published"
        )


def _first_attribute_value(candidate: ProductCandidate, canonical_field: str) -> Any:
    values = [
        attribute.normalized.get("value_text")
        for attribute in candidate.attributes
        if attribute.rule.canonical_field == canonical_field
        and attribute.normalized.get("value_text") not in (None, "")
    ]
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def _wide_value(candidate: ProductCandidate, canonical_field: str) -> Any:
    attributes = [
        attribute
        for attribute in candidate.attributes
        if attribute.rule.canonical_field == canonical_field
    ]
    values = list(
        dict.fromkeys(
            attribute.normalized.get("value_text")
            for attribute in attributes
            if attribute.normalized.get("value_text") not in (None, "")
        )
    )
    return values[0] if len(values) == 1 else None


def materialize_catalog_rows(
    *,
    manifest: Mapping[str, Any],
    mapping: MappingProfile,
    documents: Mapping[str, DocumentInfo],
    products: Sequence[ProductCandidate],
    provenance_rows: Sequence[Mapping[str, Any]],
    issue_rows: Sequence[Mapping[str, Any]],
    review_updates: Mapping[str, Mapping[str, Any]] | None = None,
    openai_reviews: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    provenance_by_logical: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    review_updates = review_updates or {}
    openai_reviews = openai_reviews or {}
    for row in provenance_rows:
        provenance_by_logical[str(row["logical_row_id"])].append(row)

    product_rows: list[dict[str, Any]] = []
    attribute_rows: list[dict[str, Any]] = []
    logical_rows: list[dict[str, Any]] = []
    structured_rows: list[dict[str, Any]] = []
    for candidate in sorted(
        products,
        key=lambda item: (
            item.source_sha256,
            item.page_idx,
            item.table_index,
            item.row_index,
            item.order_number or "",
            item.logical_row_id,
        ),
    ):
        logical_provenance = sorted(
            provenance_by_logical.get(candidate.logical_row_id, []),
            key=lambda row: (
                row.get("evidence_index") or 0,
                str(row.get("provenance_id") or ""),
            ),
        )
        primary = next(
            (
                row
                for row in logical_provenance
                if row.get("evidence_type") == "order_number"
            ),
            logical_provenance[0] if logical_provenance else None,
        )
        series_info = mapping.series.get(candidate.article_number or "")
        product_rows.append(
            {
                "product_key": candidate.product_key,
                "product_version_id": candidate.product_version_id,
                "catalog_id": manifest["catalog_id"],
                "catalog_role": manifest["catalog_role"],
                "catalog_revision": manifest["revision"],
                "manufacturer": manifest["manufacturer"],
                "family": candidate.family,
                "subfamily": candidate.subfamily,
                "series": (series_info.series_name if series_info else None)
                or candidate.article_number,
                "item_type": candidate.item_type,
                "article_number": candidate.article_number,
                "order_number": candidate.order_number,
                "size_code": _first_attribute_value(candidate, "size"),
                "standard": _first_attribute_value(candidate, "standard"),
                "identity_status": (
                    "identified"
                    if candidate.order_number and candidate.family
                    else "provisional"
                ),
                "publication_status": candidate.publication_status,
                "data_confidence": (
                    "rule_based"
                    if candidate.publication_status == "published"
                    else "requires_review"
                ),
                "primary_provenance_id": primary.get("provenance_id")
                if primary
                else None,
                "logical_row_id": candidate.logical_row_id,
            }
        )
        for attribute in sorted(
            candidate.attributes,
            key=lambda item: (
                item.rule.canonical_field,
                item.raw_record.column_index,
                item.raw_record.raw_element_id,
            ),
        ):
            attribute_id = stable_id(
                "attribute",
                candidate.product_version_id,
                attribute.rule.canonical_field,
                attribute.raw_record.raw_element_id,
            )
            normalized = attribute.normalized
            update = review_updates.get(attribute.raw_record.raw_element_id) or {}
            review = openai_reviews.get(attribute.raw_record.openai_row_id) or {}
            attribute_rows.append(
                {
                    "attribute_id": attribute_id,
                    "product_version_id": candidate.product_version_id,
                    "canonical_field": attribute.rule.canonical_field,
                    "raw_parameter_name": attribute.raw_header,
                    "value_type": normalized.get("value_type"),
                    "value_text": normalized.get("value_text"),
                    "value_decimal": normalized.get("value_decimal"),
                    "value_min": normalized.get("value_min"),
                    "value_max": normalized.get("value_max"),
                    "tolerance_lower": normalized.get("tolerance_lower"),
                    "tolerance_upper": normalized.get("tolerance_upper"),
                    "fit_class": normalized.get("fit_class"),
                    "raw_unit": attribute.rule.source_unit,
                    "canonical_unit": attribute.rule.canonical_unit,
                    "mapping_profile": mapping.raw["mapping_profile"],
                    "mapping_version": mapping.raw["mapping_version"],
                    "mapping_rule_id": attribute.rule.id,
                    "criticality": attribute.rule.criticality,
                    "confidence": review.get("openai_confidence")
                    if review
                    else "rule_based",
                    "review_status": update.get("review_status")
                    or (
                        "requires_review"
                        if attribute.rule.review_required
                        else "accepted_by_rule"
                    ),
                    "review_decision_id": update.get("review_decision_id"),
                    "decision_source": update.get("decision_source") or "FIELD_MAPPING",
                    "decision_reason": update.get("decision_reason")
                    or "Contextual deterministic mapping; raw evidence preserved.",
                    "provenance_id": attribute.provenance_id,
                }
            )
        source_page = documents[candidate.source_sha256].source_page(candidate.page_idx)
        blocker_codes = _blocking_issue_codes(candidate, issue_rows)
        logical_rows.append(
            {
                "logical_row_id": candidate.logical_row_id,
                "product_key": candidate.product_key,
                "product_version_id": candidate.product_version_id,
                "source_sha256": candidate.source_sha256,
                "page_idx": candidate.page_idx,
                "source_page": source_page,
                "table_index": candidate.table_index,
                "row_index": candidate.row_index,
                "article_number": candidate.article_number,
                "order_number": candidate.order_number,
                "family": candidate.family,
                "item_type": candidate.item_type,
                "attribute_count": len(candidate.attributes),
                "publication_status": candidate.publication_status,
                "blocking_issue_codes": json.dumps(blocker_codes, ensure_ascii=False),
            }
        )
        structured = {
            "product_key": candidate.product_key,
            "product_version_id": candidate.product_version_id,
            "catalog_id": manifest["catalog_id"],
            "catalog_role": manifest["catalog_role"],
            "catalog_revision": manifest["revision"],
            "manufacturer": manifest["manufacturer"],
            "family": candidate.family,
            "subfamily": candidate.subfamily,
            "series": candidate.article_number,
            "item_type": candidate.item_type,
            "article_number": candidate.article_number,
            "order_number": candidate.order_number,
            "publication_status": candidate.publication_status,
            "logical_row_id": candidate.logical_row_id,
            "source_file": primary.get("source_file") if primary else None,
            "source_sha256": candidate.source_sha256,
            "source_page": source_page,
            "table_index": candidate.table_index,
            "row_index": candidate.row_index,
            "blocking_issue_codes": json.dumps(blocker_codes, ensure_ascii=False),
        }
        for field_name in mapping.wide_fields:
            structured[field_name] = _wide_value(candidate, field_name)
        structured_rows.append(structured)
    return product_rows, attribute_rows, logical_rows, structured_rows


def build_reviewed_rows(
    *,
    raw_headers: Sequence[str],
    raw_records: Sequence[RawRecord],
    manifest: Mapping[str, Any],
    documents: Mapping[str, DocumentInfo],
    selected_flags: Mapping[str, bool],
    selection_reasons: Mapping[str, str],
    usage: Mapping[str, Sequence[str]],
    review_updates: Mapping[str, Mapping[str, Any]],
    openai_reviews: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    columns = list(raw_headers) + REVIEWED_EXTRA_COLUMNS
    rows: list[dict[str, Any]] = []
    for record in raw_records:
        selected = bool(selected_flags.get(record.raw_element_id))
        used_by = sorted(set(usage.get(record.raw_element_id, [])))
        update = review_updates.get(record.raw_element_id) or {}
        review = openai_reviews.get(record.openai_row_id) or {}
        document = documents.get(record.source_sha256)
        raw_parameter = str(record.values.get("raw_parameter_name") or "")
        raw_value = str(record.values.get("raw_value") or "")
        is_identifier = bool(
            identifier_kind(raw_parameter)
            or re.search(r"\d{3,8}\s+\d+\.\d+", raw_value)
        )
        if not selected:
            classification = "non_selected_parse_variant"
            review_status = "not_selected"
        elif used_by and update:
            classification = "mapped_attribute"
            review_status = str(update.get("review_status") or "accepted_by_rule")
        elif used_by and is_identifier:
            classification = "identifier_evidence"
            review_status = "accepted_by_rule"
        elif used_by:
            classification = "unmapped_evidence"
            review_status = "requires_review"
        else:
            classification = "selected_unassigned_cell"
            review_status = "not_applicable"
        if review and not update:
            review_status = "openai_proposal_only"
        row = {header: record.values.get(header) for header in raw_headers}
        row.update(
            {
                "raw_element_id": record.raw_element_id,
                "openai_row_id": record.openai_row_id,
                "catalog_id": manifest["catalog_id"],
                "catalog_role": manifest["catalog_role"],
                "catalog_revision": manifest["revision"],
                "manufacturer": manifest["manufacturer"],
                "document_id": document.document_id if document else None,
                "source_page": document.source_page(record.page_idx)
                if document
                else None,
                "selected_artifact": selected,
                "selection_reason": selection_reasons.get(record.raw_element_id),
                "logical_row_ids": json.dumps(used_by, ensure_ascii=False),
                "row_classification": classification,
                "mapping_rule_id": update.get("mapping_rule_id"),
                "canonical_field": update.get("canonical_field"),
                "validation_mode": "openai_suggestion"
                if review
                else "deterministic_only",
                "openai_verdict": review.get("openai_verdict"),
                "openai_confidence": review.get("openai_confidence"),
                "openai_issues": json.dumps(
                    review.get("openai_issues") or [], ensure_ascii=False
                ),
                "openai_proposed_parameter_name": review.get(
                    "openai_processed_parameter_name"
                ),
                "openai_proposed_value": review.get("openai_processed_value"),
                "openai_evidence": review.get("openai_evidence"),
                "review_status": review_status,
                "accepted_value": update.get("accepted_value"),
                "decision_source": update.get("decision_source"),
                "decision_reason": update.get("decision_reason"),
                "review_decision_id": update.get("review_decision_id"),
                "model_id": review.get("openai_model"),
                "prompt_version": review.get("prompt_version"),
                "response_id": review.get("openai_response_id"),
                "reviewed_at": review.get("reviewed_at"),
            }
        )
        rows.append(row)
    return columns, rows


def build_acceptance_summary(
    *,
    selected_records: Sequence[RawRecord],
    product_rows: Sequence[Mapping[str, Any]],
    attribute_rows: Sequence[Mapping[str, Any]],
    structured_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    raw_texts = [
        str(value)
        for record in selected_records
        for value in (
            record.values.get("raw_parameter_name"),
            record.values.get("raw_value"),
            record.values.get("header_path"),
        )
        if value not in (None, "")
    ]
    canonical_fields = {str(row.get("canonical_field")) for row in attribute_rows}
    parameter_checks = {
        name: name in canonical_fields for name in ("d1", "d2", "d3", "l1", "l2", "l3")
    }
    order_numbers = [
        str(row["order_number"]) for row in product_rows if row.get("order_number")
    ]
    return {
        "canonical_parameter_fields": parameter_checks,
        "all_required_parameter_names_observed": all(parameter_checks.values()),
        "diameter_symbol_cells": sum(
            any(symbol in text for symbol in ("Ø", "∅")) for text in raw_texts
        ),
        "decimal_comma_cells": sum(
            bool(re.search(r"\d,\d", text)) for text in raw_texts
        ),
        "tolerance_cells": sum(
            bool(
                re.search(
                    r"(?:±|\+\s*\d|/\s*-|\b(?:h|e|f|g|js)\d+\b)", text, re.IGNORECASE
                )
            )
            for text in raw_texts
        ),
        "merged_span_cells": sum(
            int(record.values.get("rowspan") or 1) > 1
            or int(record.values.get("colspan") or 1) > 1
            for record in selected_records
        ),
        "bbox_cells": sum(
            record.values.get("bbox") not in (None, "", "null")
            for record in selected_records
        ),
        "bbox_scopes": dict(
            sorted(
                Counter(
                    str(record.values.get("bbox_scope") or "unknown")
                    for record in selected_records
                ).items()
            )
        ),
        "source_pages": sorted(
            {
                int(row["source_page"])
                for row in structured_rows
                if row.get("source_page") not in (None, "")
            }
        ),
        "identified_order_numbers": len(order_numbers),
        "unique_order_numbers": len(set(order_numbers)),
        "duplicate_order_numbers": len(order_numbers) - len(set(order_numbers)),
        "known_series": sorted(
            {
                str(row["article_number"])
                for row in product_rows
                if row.get("article_number")
            }
        ),
    }


def _excel_cell(worksheet: Any, value: Any) -> WriteOnlyCell:
    if value is None:
        return WriteOnlyCell(worksheet, value=None)
    if isinstance(value, bool):
        return WriteOnlyCell(worksheet, value=value)
    if isinstance(value, int):
        return WriteOnlyCell(worksheet, value=value)
    if isinstance(value, float):
        return WriteOnlyCell(worksheet, value=value)
    text = str(value)
    if len(text) > EXCEL_MAX_TEXT:
        raise WorkbookValidationError(
            f"Cell text has {len(text)} characters; refusing a lossy XLSX truncation."
        )
    if ILLEGAL_CHARACTERS_RE.search(text):
        raise WorkbookValidationError(
            "Cell text contains an XML-illegal control character."
        )
    cell = WriteOnlyCell(worksheet, value=text)
    cell.data_type = "s"
    return cell


def _normalized_row(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    return {column: json_value(row.get(column)) for column in columns}


def _sheet_digest(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_bytes(_normalized_row(row, columns)) + b"\n")
    return digest.hexdigest()


def write_validated_workbook(
    path: Path,
    sheets: Sequence[tuple[str, Sequence[str], Sequence[Mapping[str, Any]]]],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_rows = {name: len(rows) for name, _, rows in sheets}
    sheet_columns = {name: list(columns) for name, columns, _ in sheets}
    sheet_hashes = {
        name: _sheet_digest(rows, columns) for name, columns, rows in sheets
    }
    content_sha256 = sha256_json(
        [
            {
                "sheet": name,
                "columns": list(columns),
                "rows": sheet_rows[name],
                "sha256": sheet_hashes[name],
            }
            for name, columns, _ in sheets
        ]
    )
    manifest_values = {
        "workbook_schema_version": WORKBOOK_SCHEMA_VERSION,
        **{str(key): json_value(value) for key, value in metadata.items()},
        "sheet_rows": json.dumps(sheet_rows, ensure_ascii=False, sort_keys=True),
        "sheet_columns": json.dumps(sheet_columns, ensure_ascii=False, sort_keys=True),
        "sheet_order": json.dumps([name for name, _, _ in sheets], ensure_ascii=False),
        "sheet_sha256": json.dumps(sheet_hashes, ensure_ascii=False, sort_keys=True),
        "content_sha256": content_sha256,
    }

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".xlsx", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook = Workbook(write_only=True, iso_dates=True)
        fixed_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
        workbook.properties.creator = "guhring-parser catalog_etl"
        workbook.properties.created = fixed_time
        workbook.properties.modified = fixed_time
        for sheet_name, columns, rows in sheets:
            worksheet = workbook.create_sheet(sheet_name)
            worksheet.append([_excel_cell(worksheet, column) for column in columns])
            for row in rows:
                worksheet.append(
                    [_excel_cell(worksheet, row.get(column)) for column in columns]
                )
        manifest_sheet = workbook.create_sheet("_manifest")
        manifest_sheet.append(
            [_excel_cell(manifest_sheet, "key"), _excel_cell(manifest_sheet, "value")]
        )
        for key in sorted(manifest_values):
            value = manifest_values[key]
            if not isinstance(value, str):
                value = json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            manifest_sheet.append(
                [_excel_cell(manifest_sheet, key), _excel_cell(manifest_sheet, value)]
            )
        workbook.save(temporary)
        validate_workbook(temporary)
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": path.name,
        "sha256": sha256_file(path),
        "content_sha256": content_sha256,
        "sheet_rows": sheet_rows,
        "sheet_sha256": sheet_hashes,
    }


def validate_workbook(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkbookValidationError(f"Workbook is missing: {path}")
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if "_manifest" not in workbook.sheetnames:
            raise WorkbookValidationError(f"Workbook has no _manifest: {path}")
        manifest_rows = workbook["_manifest"].iter_rows(values_only=True)
        try:
            header = next(manifest_rows)
        except StopIteration as exc:
            raise WorkbookValidationError(f"Empty _manifest in {path}") from exc
        if tuple(header) != ("key", "value"):
            raise WorkbookValidationError(f"Invalid _manifest header in {path}")
        manifest = {
            str(row[0]): str(row[1]) if row[1] is not None else ""
            for row in manifest_rows
            if row and row[0] is not None
        }
        try:
            declared_rows = json.loads(manifest["sheet_rows"])
            declared_columns = json.loads(manifest["sheet_columns"])
            declared_hashes = json.loads(manifest["sheet_sha256"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise WorkbookValidationError(
                f"Malformed _manifest in {path}: {exc}"
            ) from exc
        try:
            declared_order = json.loads(manifest["sheet_order"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise WorkbookValidationError(
                f"Malformed sheet_order in {path}: {exc}"
            ) from exc
        if not isinstance(declared_order, list) or set(declared_order) != set(
            declared_columns
        ):
            raise WorkbookValidationError(f"Invalid sheet_order in {path}")
        expected_names = declared_order + ["_manifest"]
        if workbook.sheetnames != expected_names:
            raise WorkbookValidationError(
                f"Workbook sheet order mismatch in {path}: {workbook.sheetnames} != {expected_names}"
            )
        actual_summaries: list[dict[str, Any]] = []
        for sheet_name in declared_order:
            worksheet = workbook[sheet_name]
            row_iter = worksheet.iter_rows()
            try:
                header_cells = next(row_iter)
            except StopIteration as exc:
                raise WorkbookValidationError(
                    f"Empty sheet {sheet_name} in {path}"
                ) from exc
            headers = [str(cell.value or "") for cell in header_cells]
            if headers != declared_columns[sheet_name]:
                raise WorkbookValidationError(
                    f"Column contract mismatch for {sheet_name} in {path}"
                )
            digest = hashlib.sha256()
            count = 0
            for cells in row_iter:
                if any(cell.data_type == "f" for cell in cells):
                    raise WorkbookValidationError(
                        f"Formula found in {sheet_name} in {path}"
                    )
                values = {
                    column: json_value(cells[index].value)
                    if index < len(cells)
                    else None
                    for index, column in enumerate(headers)
                }
                digest.update(canonical_bytes(values) + b"\n")
                count += 1
            if count != int(declared_rows[sheet_name]):
                raise WorkbookValidationError(
                    f"Row count mismatch for {sheet_name} in {path}"
                )
            actual_hash = digest.hexdigest()
            if actual_hash != declared_hashes[sheet_name]:
                raise WorkbookValidationError(
                    f"Row hash mismatch for {sheet_name} in {path}"
                )
            actual_summaries.append(
                {
                    "sheet": sheet_name,
                    "columns": headers,
                    "rows": count,
                    "sha256": actual_hash,
                }
            )
        actual_content_hash = sha256_json(actual_summaries)
        if actual_content_hash != manifest.get("content_sha256"):
            raise WorkbookValidationError(f"Content hash mismatch in {path}")
        return {"path": str(path), "manifest": manifest, "sha256": sha256_file(path)}
    finally:
        workbook.close()


def build_cache_identity(
    *,
    config: Config,
    raw_sha256: str,
    selected_records: Sequence[RawRecord],
    selections: Mapping[tuple[str, int], str],
    decisions_sha256: str,
    openai_meta: Mapping[str, Any],
    context_records: Sequence[RawRecord] = (),
) -> dict[str, Any]:
    selected_rows = sorted(
        (
            {
                "raw_element_id": record.raw_element_id,
                "values": record.values,
            }
            for record in selected_records
        ),
        key=lambda row: str(row["raw_element_id"]),
    )
    parser_configs = sorted(
        {
            (
                record.source_sha256,
                record.page_idx,
                record.cache_key,
                str(record.values.get("parser_version") or ""),
                str(record.values.get("parser_backend") or ""),
            )
            for record in selected_records
        }
    )
    schema_assets = {
        path.name: sha256_file(path)
        for path in sorted(config.manifest_schema_path.parent.glob("*.json"))
        if path.is_file()
    }
    identity = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "stage": "catalog-etl",
        "etl_version": ETL_VERSION,
        "raw_file_sha256": raw_sha256,
        "selected_raw_rows_sha256": sha256_json(selected_rows),
        "selected_raw_rows": len(selected_rows),
        "context_records_sha256": sha256_json(
            [record.values for record in context_records]
        ),
        "context_artifacts_sha256": {
            str(path): sha256_file(path)
            for record in selected_records
            if (path := (Path(str(record.values.get("artifact_path") or "")))).is_file()
        },
        "selected_pages": [
            {
                "source_sha256": source_sha,
                "page_idx": page_idx,
                "cache_key": cache_key,
            }
            for (source_sha, page_idx), cache_key in sorted(selections.items())
        ],
        "parser_configs": [
            {
                "source_sha256": value[0],
                "page_idx": value[1],
                "cache_key": value[2],
                "parser_version": value[3],
                "parser_backend": value[4],
            }
            for value in parser_configs
        ],
        "accepted_review_decisions_sha256": decisions_sha256,
        "reconstructions_sha256": sha256_file(config.reconstructions_path)
        if config.reconstructions_path
        else None,
        "openai_processed_xlsx_sha256": openai_meta.get("processed_xlsx_sha256"),
        "page_completeness_latest_sha256": sha256_file(
            config.openai_root / "completeness" / "latest.json"
        )
        if (config.use_openai or config.use_page_completeness)
        and (config.openai_root / "completeness" / "latest.json").is_file()
        else None,
        "catalog_manifest_sha256": sha256_file(config.catalog_manifest_path),
        "field_mapping_sha256": sha256_file(config.mapping_path),
        "schema_assets_sha256": schema_assets,
        "code_sha256": {
            "catalog_etl.py": sha256_file(Path(__file__)),
            "export_raw.py": sha256_file(Path(export_raw.__file__)),
            "catalog_registry.py": sha256_file(
                Path(__file__).with_name("catalog_registry.py")
            ),
            "page_completeness.py": sha256_file(
                Path(__file__).with_name("page_completeness.py")
            ),
            "reconstruction.py": sha256_file(
                Path(__file__).with_name("reconstruction.py")
            ),
        },
        "deterministic_config": {
            "use_openai": config.use_openai,
            "allow_partial_inventory": config.allow_partial_inventory,
            "workbook_schema_version": WORKBOOK_SCHEMA_VERSION,
        },
    }
    return identity


def _validate_cached_attempt(
    attempt_root: Path,
    expected_outputs: Mapping[str, Mapping[str, Any]],
) -> None:
    for filename in OUTPUT_FILENAMES:
        expected = expected_outputs.get(filename)
        if not isinstance(expected, Mapping):
            raise WorkbookValidationError(
                f"Cached run has no declaration for {filename}."
            )
        path = attempt_root / filename
        if sha256_file(path) != expected.get("sha256"):
            raise WorkbookValidationError(f"Cached file SHA-256 mismatch: {path}")
        validated = validate_workbook(path)
        if validated["manifest"].get("content_sha256") != expected.get(
            "content_sha256"
        ):
            raise WorkbookValidationError(
                f"Cached workbook content hash mismatch: {path}"
            )


def _publish_attempt(
    *,
    config: Config,
    attempt_root: Path,
    run_manifest: Mapping[str, Any],
    cache_hit: bool,
) -> None:
    config.publish_root.mkdir(parents=True, exist_ok=True)
    for filename in OUTPUT_FILENAMES:
        atomic_copy(attempt_root / filename, config.publish_root / filename)
    pointer = {
        "schema_version": 1,
        "status": "success",
        "cache_hit": cache_hit,
        "cache_key": run_manifest["cache_key"],
        "run_id": run_manifest["run_id"],
        "attempt_path": str(attempt_root),
        "attempt_relpath": str(attempt_root.relative_to(config.output_root)),
        "outputs": run_manifest["outputs"],
        "published_at": utc_now().isoformat(),
    }
    atomic_write_json(config.publish_root / "run.json", pointer)
    atomic_write_json(config.output_root / "latest.json", pointer)


def _write_run_log(config: Config, report: Mapping[str, Any]) -> Path:
    config.logs_root.mkdir(parents=True, exist_ok=True)
    run_id = str(report.get("run_id") or timestamp())
    log_path = config.logs_root / f"catalog_etl_{run_id}.json"
    atomic_write_json(log_path, report)
    atomic_write_json(config.logs_root / "catalog_etl_latest.json", report)
    return log_path


def try_cache_hit(
    *,
    config: Config,
    cache_key: str,
    started: float,
) -> dict[str, Any] | None:
    success_path = (
        config.output_root
        / "cache"
        / f"v{CACHE_SCHEMA_VERSION}"
        / cache_key
        / "success.json"
    )
    if config.force or not success_path.is_file():
        return None
    try:
        success = load_json(success_path)
    except (OSError, ETLError, ValueError, TypeError):
        return None
    if success.get("status") != "success" or success.get("cache_key") != cache_key:
        return None
    attempt_relpath = success.get("attempt_relpath")
    attempt_root = (
        config.output_root / str(attempt_relpath)
        if attempt_relpath
        else Path(str(success.get("attempt_path") or ""))
    )
    if not attempt_root.resolve().is_relative_to(
        success_path.parent.resolve() / "attempts"
    ):
        return None
    try:
        _validate_cached_attempt(attempt_root, success.get("outputs") or {})
    except (OSError, ETLError, KeyError, TypeError):
        return None
    _publish_attempt(
        config=config,
        attempt_root=attempt_root,
        run_manifest=success,
        cache_hit=True,
    )
    report = {
        **success,
        "cache_hit": True,
        "processing_time_seconds": round(time.monotonic() - started, 6),
    }
    if config.fail_on_quality_gate and report.get("results", {}).get("blocking_issues"):
        report["quality_gate_failed"] = True
    report["log_path"] = str(_write_run_log(config, report))
    return report


def _output_specs(
    *,
    raw_headers: Sequence[str],
    reviewed_rows: Sequence[Mapping[str, Any]],
    logical_rows: Sequence[Mapping[str, Any]],
    product_rows: Sequence[Mapping[str, Any]],
    attribute_rows: Sequence[Mapping[str, Any]],
    provenance_rows: Sequence[Mapping[str, Any]],
    issue_rows: Sequence[Mapping[str, Any]],
    structured_rows: Sequence[Mapping[str, Any]],
    wide_columns: Sequence[str],
    relation_rows: Sequence[Mapping[str, Any]] = (),
    row_classifications: Sequence[Mapping[str, Any]] = (),
    table_links: Sequence[Mapping[str, Any]] = (),
    proposed_missing: Sequence[Mapping[str, Any]] = (),
    decisions_history: Sequence[Mapping[str, Any]] = (),
    context_rows: Sequence[Mapping[str, Any]] = (),
    reconstruction_history: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[tuple[str, Sequence[str], Sequence[Mapping[str, Any]]]]]:
    published_versions = {
        str(row["product_version_id"])
        for row in product_rows
        if row.get("publication_status") == "published"
    }
    published_logical = {
        str(row["logical_row_id"])
        for row in product_rows
        if row.get("publication_status") == "published"
    }
    matcher_rows = [
        row for row in structured_rows if row.get("publication_status") == "published"
    ]
    blocked_rows = [
        {**row, "blocked_reason": row.get("blocking_issue_codes")}
        for row in structured_rows
        if row.get("publication_status") != "published"
    ]
    return {
        "master_reviewed.xlsx": [
            ("reviewed", raw_headers, reviewed_rows),
            (
                "reconstructions",
                [
                    "reconstruction_id",
                    "source_sha256",
                    "page_idx",
                    "cache_key",
                    "table_index",
                    "replace_rows",
                    "rows",
                    "status",
                    "reviewer",
                    "reason",
                    "evidence",
                    "decided_at",
                    "proposal_ids",
                ],
                reconstruction_history,
            ),
            ("logical_rows", LOGICAL_ROW_COLUMNS, logical_rows),
            (
                "row_classifications",
                [
                    "logical_row_id",
                    "table_id",
                    "page_idx",
                    "row_index",
                    "row_type",
                    "raw_element_ids",
                ],
                row_classifications,
            ),
            (
                "table_links",
                [
                    "table_id",
                    "continues_table_id",
                    "source_sha256",
                    "page_idx",
                    "table_index",
                    "basis",
                    "header_raw_element_ids",
                ],
                table_links,
            ),
            (
                "proposed_missing",
                [
                    "proposal_id",
                    "source_sha256",
                    "page_idx",
                    "element_type",
                    "proposed_value",
                    "bbox",
                    "evidence",
                    "confidence",
                    "review_status",
                ],
                proposed_missing,
            ),
            (
                "context_evidence",
                list(export_raw.ALL_COLUMNS)
                + [
                    "raw_element_id",
                    "mapping_rule_id",
                    "canonical_field",
                    "accepted_value",
                    "decision_source",
                    "decision_reason",
                    "review_status",
                ],
                context_rows,
            ),
            (
                "decisions",
                [
                    "review_decision_id",
                    "raw_element_id",
                    "proposal_id",
                    "source_sha256",
                    "page_idx",
                    "action",
                    "reviewer",
                    "reason",
                    "decided_at",
                ],
                decisions_history,
            ),
            ("issues", ISSUE_COLUMNS, issue_rows),
        ],
        "master_canonical.xlsx": [
            ("products", PRODUCT_COLUMNS, product_rows),
            ("attributes", ATTRIBUTE_COLUMNS, attribute_rows),
            ("relations", RELATION_COLUMNS, relation_rows),
            ("provenance", PROVENANCE_COLUMNS, provenance_rows),
            ("issues", ISSUE_COLUMNS, issue_rows),
            ("products_wide", wide_columns, structured_rows),
        ],
        "master_structured.xlsx": [
            ("structured", wide_columns, structured_rows),
            ("issues", ISSUE_COLUMNS, issue_rows),
        ],
        "master_matcher.xlsx": [
            ("matcher", wide_columns, matcher_rows),
            (
                "blocked_candidates",
                list(wide_columns) + BLOCKED_MATCHER_EXTRA_COLUMNS,
                blocked_rows,
            ),
            (
                "attributes",
                ATTRIBUTE_COLUMNS,
                [
                    row
                    for row in attribute_rows
                    if str(row.get("product_version_id")) in published_versions
                ],
            ),
            (
                "relations",
                RELATION_COLUMNS,
                [
                    row
                    for row in relation_rows
                    if row.get("source_product_version_id") in published_versions
                    and row.get("target_product_version_id") in published_versions
                    and row.get("review_status") == "accepted"
                ],
            ),
            (
                "provenance",
                PROVENANCE_COLUMNS,
                [
                    row
                    for row in provenance_rows
                    if str(row.get("logical_row_id")) in published_logical
                ],
            ),
            ("issues", ISSUE_COLUMNS, issue_rows),
        ],
    }


def load_page_completeness(
    config: Config,
    manifest: Mapping[str, Any],
    selections: Mapping[tuple[str, int], str],
    raw_sha: str,
    issues: IssueRegistry,
    reconstructed_proposals: set[tuple[str, str, int, str]] | frozenset = frozenset(),
    effective_records: Sequence[RawRecord] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = config.openai_root / "completeness"
    if (
        not (config.use_openai or config.use_page_completeness)
        or not (root / "latest.json").is_file()
    ):
        if reconstructed_proposals:
            raise ConfigurationError(
                "Reconstructed proposal references require a verified completeness report."
            )
        if manifest.get("require_page_completeness", False):
            issues.add(
                entity_type="catalog",
                entity_id=manifest["catalog_id"],
                code="page_completeness_not_run",
                severity="critical",
                blocks_publication=True,
                message="Visual page completeness review is required.",
            )
        return [], {"status": "not_loaded"}
    pointer = load_json(root / "latest.json")
    run_id = str(pointer.get("run_id") or "")
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ConfigurationError("Invalid completeness run_id.")
    path = root / "runs" / run_id / "result.json"
    if (
        pointer.get("status") != "success"
        or not path.is_file()
        or sha256_file(path) != pointer.get("result_sha256")
    ):
        raise ConfigurationError("Completeness result is missing or corrupt.")
    report = load_json(path)
    if report.get("input_sha256") != raw_sha or report.get(
        "selection_manifest_sha256"
    ) != sha256_file(config.catalog_manifest_path):
        raise ConfigurationError(
            "Completeness review is stale for the RAW or catalog manifest."
        )
    expected = {
        (doc["sha256"], page)
        for doc in manifest["documents"]
        for page in range(doc["pages"])
    }
    found = set()
    from page_completeness import validate_result
    from openai_process import ResponseValidationError

    decisions = {}
    if config.decisions_path.is_file():
        for line in config.decisions_path.read_text().splitlines():
            if not line.strip():
                continue
            decision = json.loads(line)
            validate_schema(decision, config.decision_schema_path, "Review decision")
            if "raw_element_id" in decision:
                continue
            key = decision.get("proposal_id") or (
                decision["source_sha256"],
                decision["page_idx"],
            )
            if key in decisions and datetime.fromisoformat(
                decision["decided_at"].replace("Z", "+00:00")
            ) <= datetime.fromisoformat(
                decisions[key]["decided_at"].replace("Z", "+00:00")
            ):
                raise ConfigurationError(
                    "Page/proposal review history must advance decided_at."
                )
            decisions[key] = decision
    proposals = [dict(row) for row in report.get("proposals", [])]
    by_proposal = {row["proposal_id"]: row for row in proposals}
    if len(by_proposal) != len(proposals):
        raise ConfigurationError("Duplicate missing-element proposal IDs.")
    raw_records = (
        load_raw_workbook(config.raw_path)[1]
        if effective_records is None
        else effective_records
    )
    mapping = load_mapping(config, manifest)
    for page in report.get("pages", []):
        key = (page["source_sha256"], page["page_idx"])
        if (
            key in found
            or key not in expected
            or page.get("parser_cache_key") != selections.get(key)
        ):
            raise ConfigurationError(
                "Completeness review has duplicate, unregistered or unpinned pages."
            )
        found.add(key)
        try:
            validate_result(page["result"])
        except ResponseValidationError as exc:
            raise ConfigurationError(str(exc)) from exc
        open_proposals = False
        for element in page["result"]["missing_elements"]:
            proposal_id = (
                "missing_" + sha256_json([page["review_cache_key"], element])[:32]
            )
            proposal = by_proposal.get(proposal_id)
            if (
                proposal is None
                or (proposal["source_sha256"], proposal["page_idx"]) != key
                or any(proposal.get(field) != value for field, value in element.items())
            ):
                raise ConfigurationError(
                    "Missing proposals differ from their validated visual evidence."
                )
            decision = decisions.get(proposal_id)
            reconstructed = (
                proposal_id,
                key[0],
                key[1],
                selections.get(key),
            ) in reconstructed_proposals
            if reconstructed and (
                not decision or decision["action"] != "accept_proposal"
            ):
                raise ConfigurationError(
                    "A reconstructed missing element requires an explicit accept_proposal decision."
                )
            if decision:
                proposal.update(
                    review_status="rejected"
                    if decision["action"] == "reject"
                    else "accepted"
                    if reconstructed
                    else "accepted_pending_reconstruction",
                    decision_source="reviewer",
                    decision_reason=decision["reason"],
                    reviewer=decision["reviewer"],
                    reviewed_at=decision["decided_at"],
                )
            open_proposals |= proposal.get("review_status") not in {
                "rejected",
                "accepted",
            }
        page_records = [
            record
            for record in raw_records
            if (record.source_sha256, record.page_idx) == key
            and record.cache_key == selections.get(key)
        ]
        counts_disagree = False
        for table in page["result"]["tables"]:
            if table["table_index"] is None:
                counts_disagree |= bool(table["visual_product_rows"])
                continue
            cells = [
                record
                for record in page_records
                if record.values.get("source_table_index", record.table_index)
                == table["table_index"]
            ]
            columns = {record.column_index for record in cells}
            if table["visual_product_rows"] is not None:
                product_rows = sum(
                    sum(
                        classify_row(analyzed, row, mapping)
                        in {"sku", "size_variant", "kit", "component"}
                        for row in analyzed.data_rows
                    )
                    for analyzed in build_tables(cells, mapping)
                )
                if table["visual_product_rows"] != product_rows:
                    counts_disagree = True
            if table["visual_columns"] is not None and table["visual_columns"] != len(
                columns
            ):
                counts_disagree = True
        page_decision = decisions.get(key)
        approved = bool(page_decision and page_decision["action"] == "approve_page")
        if (
            open_proposals
            or bool(page_decision and page_decision["action"] == "reject_page")
            or (page["result"]["page_quality"] != "pass" or counts_disagree)
            and not approved
        ):
            issues.add(
                entity_type="catalog",
                entity_id=manifest["catalog_id"],
                code="page_completeness_requires_review",
                severity="critical",
                blocks_publication=True,
                message=f"Visual completeness page {key}: {page['result']['summary']}",
            )
    if set(decisions) - set(by_proposal) - expected:
        raise ConfigurationError(
            "Review decision references an unknown visual page or proposal."
        )
    if {proof[0] for proof in reconstructed_proposals} - set(by_proposal):
        raise ConfigurationError(
            "Reconstruction references an unknown missing-element proposal."
        )
    if found != expected:
        issues.add(
            entity_type="catalog",
            entity_id=manifest["catalog_id"],
            code="page_completeness_coverage_incomplete",
            severity="critical",
            blocks_publication=True,
            message="Visual review does not cover all registered pages.",
        )
    return proposals, {
        "status": "loaded",
        "pages": len(found),
        "proposals": len(proposals),
    }


def run_etl(config: Config) -> dict[str, Any]:
    started = time.monotonic()
    raw_before = sha256_file(config.raw_path)
    manifest, documents, selections = load_manifest(config)
    mapping = load_mapping(config, manifest)
    raw_headers, raw_records, raw_manifest = load_raw_workbook(config.raw_path)
    raw_after_load = sha256_file(config.raw_path)
    if raw_before != raw_after_load:
        raise WorkbookValidationError(
            "master_raw.xlsx changed while it was being read."
        )

    selected_records, selected_flags, selection_reasons, selection_report = (
        select_raw_records(
            raw_records,
            documents,
            selections,
            {
                (row["source_sha256"], row["page_idx"])
                for row in manifest.get("page_states", [])
                if row["status"] == "empty"
            },
        )
    )
    openai_reviews, openai_meta = load_openai_reviews(config, raw_before, raw_records)
    context_records = collect_context_records(selected_records, config.raw_path.parent)
    decisions, decisions_sha256 = load_review_decisions(
        config, [*raw_records, *context_records]
    )
    from reconstruction import apply_reconstructions

    (
        selected_records,
        reconstruction_history,
        reconstruction_decisions,
        superseded_rows,
        reconstructed_proposals,
    ) = apply_reconstructions(
        config.reconstructions_path,
        config.manifest_schema_path.with_name("reconstruction.schema.json"),
        selected_records,
        manifest,
        documents,
        selections,
        mapping,
    )
    for record in raw_records:
        if record.raw_element_id in superseded_rows:
            selected_flags[record.raw_element_id] = False
            selection_reasons[record.raw_element_id] = (
                "superseded_by_reviewed_reconstruction:"
                + superseded_rows[record.raw_element_id]
            )
    reconstructed_records = [
        record for record in selected_records if record.values.get("reconstruction_id")
    ]
    for record in reconstructed_records:
        selected_flags[record.raw_element_id] = True
        selection_reasons[record.raw_element_id] = "reviewed_reconstruction"
    decisions.update(reconstruction_decisions)
    issue_registry = IssueRegistry(str(manifest["catalog_id"]), utc_now().isoformat())
    for entry in {
        row["reconstruction_id"]: row for row in reconstruction_history
    }.values():
        if entry["status"] == "draft":
            issue_registry.add(
                entity_type="catalog",
                entity_id=manifest["catalog_id"],
                code="reconstruction_requires_review",
                severity="critical",
                blocks_publication=True,
                message=f"Reconstruction {entry['reconstruction_id']} requires an explicit review decision.",
            )
    if int(raw_manifest.get("scan_errors") or 0):
        issue_registry.add(
            entity_type="catalog",
            entity_id=manifest["catalog_id"],
            code="raw_extraction_errors",
            severity="critical",
            blocks_publication=True,
            message="RAW export reports unprocessed or damaged parser artifacts; inspect the extraction log.",
        )
    for state in manifest.get("page_states", []):
        if state["status"] not in {"parsed", "empty"}:
            issue_registry.add(
                entity_type="catalog",
                entity_id=manifest["catalog_id"],
                code="page_processing_incomplete",
                severity="critical",
                blocks_publication=True,
                message=f"Page {state['source_sha256']}:{state['page_idx']} is {state['status']}.",
            )
    reviewed_empty = {
        (row["source_sha256"], row["page_idx"])
        for row in manifest.get("page_states", [])
        if row["status"] == "empty" and row.get("reviewer") and row.get("reason")
    }
    missing_pages = (
        {
            (document.sha256, page)
            for document in documents.values()
            for page in range(document.pages)
        }
        - set(selections)
        - reviewed_empty
    )
    if missing_pages:
        issue_registry.add(
            entity_type="catalog",
            entity_id=manifest["catalog_id"],
            code="catalog_inventory_incomplete",
            severity="critical",
            blocks_publication=True,
            message=f"{len(missing_pages)} registered pages lack a selected extraction or an audited empty-page decision. Partial workbooks are for review only.",
        )
    proposed_missing, completeness_meta = load_page_completeness(
        config,
        manifest,
        selections,
        raw_before,
        issue_registry,
        reconstructed_proposals,
        selected_records,
    )
    cache_identity = build_cache_identity(
        config=config,
        raw_sha256=raw_before,
        selected_records=selected_records,
        selections=selections,
        decisions_sha256=decisions_sha256,
        openai_meta=openai_meta,
        context_records=context_records,
    )
    cache_key = sha256_bytes(
        b"catalog-etl-cache-v1\0" + canonical_bytes(cache_identity)
    )
    cached = try_cache_hit(config=config, cache_key=cache_key, started=started)
    if cached is not None:
        return cached

    run_id = f"{timestamp()}-{uuid.uuid4().hex[:12]}"
    created_at = utc_now().isoformat()
    cache_root = config.output_root / "cache" / f"v{CACHE_SCHEMA_VERSION}" / cache_key
    attempt_root = cache_root / "attempts" / run_id
    attempt_root.mkdir(parents=True, exist_ok=False)
    try:
        tables = build_tables(selected_records, mapping)
        table_links = link_document_tables(tables, mapping, documents)
        products, provenance_rows, _, review_updates, usage = reconstruct_catalog(
            manifest=manifest,
            documents=documents,
            mapping=mapping,
            tables=tables,
            openai_reviews=openai_reviews,
            decisions=decisions,
            issues=issue_registry,
            selected_records=selected_records,
            context_records=[*context_records, *selected_records],
        )
        for record in context_records:
            if (
                re.match(
                    r"(?i)^(note|hinweis|achtung|примечание|сноска|[*†‡])",
                    str(record.values["raw_value"]),
                )
                and not usage.get(record.raw_element_id)
                and decisions.get(record.raw_element_id, {}).get("action")
                not in {"reject", "not_applicable"}
            ):
                issue_registry.add(
                    entity_type="catalog",
                    entity_id=manifest["catalog_id"],
                    code="unresolved_note_context",
                    severity="critical",
                    blocks_publication=True,
                    message=f"Unlinked technical note {record.raw_element_id} needs scoped context mapping.",
                )
        relation_rows = reconstruct_relations(
            products,
            tables,
            mapping,
            manifest,
            documents,
            decisions,
            issue_registry,
            provenance_rows,
            usage,
        )
        for table in tables:
            for row_index, row_type in table.row_types.items():
                row_records = _row_values(table.cells, row_index)
                if row_type == "unknown" and not any(
                    usage.get(record.raw_element_id) for record in row_records
                ):
                    issue_registry.add(
                        entity_type="catalog",
                        entity_id=manifest["catalog_id"],
                        code="unprocessed_row",
                        severity="critical",
                        blocks_publication=True,
                        message=f"Unclassified row {table_id(table)}:{row_index} is retained for review.",
                    )
                if (
                    row_type == "note"
                    and not any(
                        usage.get(record.raw_element_id) for record in row_records
                    )
                    and not all(
                        decisions.get(record.raw_element_id, {}).get("action")
                        in {"reject", "not_applicable"}
                        for record in row_records
                        if record.values.get("raw_value") not in (None, "")
                    )
                ):
                    issue_registry.add(
                        entity_type="catalog",
                        entity_id=manifest["catalog_id"],
                        code="unresolved_note_context",
                        severity="critical",
                        blocks_publication=True,
                        message=f"Unlinked table note {table_id(table)}:{row_index}.",
                    )
        completeness = apply_golden_expectations(
            manifest=manifest,
            products=products,
            issues=issue_registry,
        )
        finalize_publication_statuses(products, issue_registry.rows)
        validate_relation_publication(products, relation_rows, issue_registry)
        product_rows, attribute_rows, logical_rows, structured_rows = (
            materialize_catalog_rows(
                manifest=manifest,
                mapping=mapping,
                documents=documents,
                products=products,
                provenance_rows=provenance_rows,
                issue_rows=issue_registry.rows,
                review_updates=review_updates,
                openai_reviews=openai_reviews,
            )
        )
        reviewed_columns, reviewed_rows = build_reviewed_rows(
            raw_headers=raw_headers,
            raw_records=[*raw_records, *reconstructed_records],
            manifest=manifest,
            documents=documents,
            selected_flags=selected_flags,
            selection_reasons=selection_reasons,
            usage=usage,
            review_updates=review_updates,
            openai_reviews=openai_reviews,
        )
        classifications = {
            record.raw_element_id: table.row_types[record.row_index]
            for table in tables
            for record in table.cells.values()
        }
        for row in reviewed_rows:
            if row["selected_artifact"]:
                row["row_classification"] = classifications.get(
                    row["raw_element_id"], "unknown"
                )
        wide_columns = STRUCTURED_BASE_COLUMNS + mapping.wide_fields
        specs = _output_specs(
            raw_headers=reviewed_columns,
            reviewed_rows=reviewed_rows,
            logical_rows=logical_rows,
            product_rows=product_rows,
            attribute_rows=attribute_rows,
            provenance_rows=provenance_rows,
            issue_rows=issue_registry.rows,
            structured_rows=structured_rows,
            wide_columns=wide_columns,
            context_rows=[
                {
                    **record.values,
                    "raw_element_id": record.raw_element_id,
                    **review_updates.get(record.raw_element_id, {}),
                }
                for record in context_records
            ],
            reconstruction_history=reconstruction_history,
            relation_rows=relation_rows,
            table_links=table_links,
            proposed_missing=proposed_missing,
            decisions_history=(
                [
                    {
                        "review_decision_id": stable_id("decision", json.loads(line)),
                        **json.loads(line),
                    }
                    for line in config.decisions_path.read_text().splitlines()
                    if line.strip()
                ]
                if config.decisions_path.is_file()
                else []
            )
            + [
                {"review_decision_id": stable_id("decision", decision), **decision}
                for decision in reconstruction_decisions.values()
            ],
            row_classifications=[
                {
                    "logical_row_id": stable_id("source_row", table_id(table), row),
                    "table_id": table_id(table),
                    "page_idx": table.page_idx,
                    "row_index": row,
                    "row_type": kind,
                    "raw_element_ids": [
                        item.raw_element_id for item in _row_values(table.cells, row)
                    ],
                }
                for table in tables
                for row, kind in table.row_types.items()
            ],
        )
        publication_counts = dict(
            Counter(row["publication_status"] for row in product_rows)
        )
        acceptance = build_acceptance_summary(
            selected_records=selected_records,
            product_rows=product_rows,
            attribute_rows=attribute_rows,
            structured_rows=structured_rows,
        )
        common_metadata = {
            "etl_version": ETL_VERSION,
            "run_id": run_id,
            "cache_key": cache_key,
            "created_at": created_at,
            "catalog_id": manifest["catalog_id"],
            "catalog_role": manifest["catalog_role"],
            "catalog_revision": manifest["revision"],
            "manifest_sha256": cache_identity["catalog_manifest_sha256"],
            "mapping_profile": mapping.raw["mapping_profile"],
            "mapping_version": mapping.raw["mapping_version"],
            "mapping_sha256": cache_identity["field_mapping_sha256"],
            "raw_file_sha256": raw_before,
            "raw_record_sha256": raw_manifest.get("record_sha256"),
            "selected_raw_rows_sha256": cache_identity["selected_raw_rows_sha256"],
            "review_decisions_sha256": decisions_sha256,
            "openai_status": openai_meta.get("status"),
            "openai_processed_xlsx_sha256": openai_meta.get("processed_xlsx_sha256"),
            "matcher_contract_status": mapping.raw["matcher_contract"]["status"],
            "publication_policy": "published-only; fail-closed",
            "prompt_version": openai_meta.get("prompt_version"),
            "completeness_status": completeness["status"],
            "schema_assets_sha256": cache_identity["schema_assets_sha256"],
            "code_sha256": cache_identity["code_sha256"],
            "parser_configs": cache_identity["parser_configs"],
        }
        output_manifests: dict[str, dict[str, Any]] = {}
        for filename in OUTPUT_FILENAMES:
            output_manifests[filename] = write_validated_workbook(
                attempt_root / filename,
                specs[filename],
                {
                    **common_metadata,
                    "workbook_role": filename.removeprefix("master_").removesuffix(
                        ".xlsx"
                    ),
                },
            )

        raw_after = sha256_file(config.raw_path)
        if raw_after != raw_before:
            raise WorkbookValidationError(
                "master_raw.xlsx was modified during ETL; run rejected."
            )
        report = {
            "schema_version": 1,
            "status": "success",
            "run_id": run_id,
            "cache_key": cache_key,
            "cache_hit": False,
            "attempt_path": str(attempt_root),
            "attempt_relpath": str(attempt_root.relative_to(config.output_root)),
            "created_at": created_at,
            "input": {
                "raw_path": str(config.raw_path),
                "raw_sha256_before": raw_before,
                "raw_sha256_after": raw_after,
                "raw_records": len(raw_records),
                "selected_records": len(selected_records),
                "selection": selection_report,
            },
            "configuration": cache_identity,
            "openai": openai_meta,
            "manual_decisions": len(decisions),
            "page_completeness": completeness_meta,
            "results": {
                "tables": len(tables),
                "table_links": len(table_links),
                "relations": len(relation_rows),
                "logical_rows": len(logical_rows),
                "identified_products": sum(
                    bool(row.get("order_number")) for row in product_rows
                ),
                "unique_order_numbers": len(
                    {
                        row["order_number"]
                        for row in product_rows
                        if row.get("order_number")
                    }
                ),
                "attributes": len(attribute_rows),
                "provenance": len(provenance_rows),
                "issues": len(issue_registry.rows),
                "blocking_issues": sum(
                    bool(row.get("blocks_publication")) and row.get("status") == "open"
                    for row in issue_registry.rows
                ),
                "publication_statuses": publication_counts,
                "matcher_published_rows": publication_counts.get("published", 0),
                "reconstructed_cells": len(reconstructed_records),
                "superseded_raw_cells": len(superseded_rows),
            },
            "completeness": completeness,
            "quality_metrics": {
                "registered_documents": len(documents),
                "registered_pages": sum(doc.pages for doc in documents.values()),
                "selected_pages": len(selections),
                "reviewed_empty_pages": sum(
                    state["status"] == "empty"
                    for state in manifest.get("page_states", [])
                ),
                "provisional_products": sum(
                    row["identity_status"] == "provisional" for row in product_rows
                ),
                "duplicate_identity_issues": sum(
                    row["issue_code"] == "duplicate_identity"
                    for row in issue_registry.rows
                ),
                "attributes_without_provenance": sum(
                    not row["provenance_id"] for row in attribute_rows
                ),
                "sku_recall": completeness["detected_total"]
                / completeness["expected_total"]
                if completeness.get("expected_total")
                else None,
            },
            "acceptance": acceptance,
            "outputs": output_manifests,
            "processing_time_seconds": round(time.monotonic() - started, 6),
        }
        atomic_write_json(attempt_root / "manifest.json", report)
        success_pointer = dict(report)
        atomic_write_json(cache_root / "success.json", success_pointer)
        _publish_attempt(
            config=config,
            attempt_root=attempt_root,
            run_manifest=success_pointer,
            cache_hit=False,
        )
        report["log_path"] = str(_write_run_log(config, report))
        if config.fail_on_quality_gate and report["results"]["blocking_issues"]:
            report["quality_gate_failed"] = True
        return report
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "run_id": run_id,
            "cache_key": cache_key,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "processing_time_seconds": round(time.monotonic() - started, 6),
        }
        atomic_write_json(attempt_root / "failure.json", failure)
        _write_run_log(config, failure)
        raise


def validate_published_outputs(config: Config) -> dict[str, Any]:
    latest_path = config.output_root / "latest.json"
    latest = load_json(latest_path)
    if latest.get("status") != "success":
        raise WorkbookValidationError("ETL latest pointer is not successful.")
    results: dict[str, Any] = {}
    for filename in OUTPUT_FILENAMES:
        expected = (latest.get("outputs") or {}).get(filename)
        if not isinstance(expected, Mapping):
            raise WorkbookValidationError(f"Latest pointer has no {filename} metadata.")
        path = config.publish_root / filename
        if sha256_file(path) != expected.get("sha256"):
            raise WorkbookValidationError(f"Published SHA-256 mismatch: {path}")
        validated = validate_workbook(path)
        if validated["manifest"].get("content_sha256") != expected.get(
            "content_sha256"
        ):
            raise WorkbookValidationError(f"Published content hash mismatch: {path}")
        results[filename] = validated
    return {
        "status": "valid",
        "cache_key": latest.get("cache_key"),
        "run_id": latest.get("run_id"),
        "outputs": results,
    }


def _default_path(environment_name: str, value: str) -> Path:
    return Path(os.getenv(environment_name, value))


def parse_arguments(argv: Sequence[str] | None = None) -> tuple[Config, bool]:
    parser = argparse.ArgumentParser(
        description="Build fail-closed reviewed/canonical/structured catalog workbooks from MinerU RAW."
    )
    parser.add_argument(
        "--raw",
        type=Path,
        default=_default_path("ETL_RAW_PATH", "/inputs/master_raw.xlsx"),
    )
    parser.add_argument(
        "--catalog-manifest",
        type=Path,
        default=_default_path(
            "ETL_CATALOG_MANIFEST", "/catalogs/manifests/guhring-production.yaml"
        ),
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=_default_path("ETL_MAPPING", "/config/mappings/guhring.yaml"),
    )
    parser.add_argument(
        "--manifest-schema",
        type=Path,
        default=_default_path(
            "ETL_MANIFEST_SCHEMA", "/config/schemas/catalog-manifest.schema.json"
        ),
    )
    parser.add_argument(
        "--mapping-schema",
        type=Path,
        default=_default_path(
            "ETL_MAPPING_SCHEMA", "/config/schemas/field-mapping.schema.json"
        ),
    )
    parser.add_argument(
        "--decision-schema",
        type=Path,
        default=_default_path(
            "ETL_DECISION_SCHEMA", "/config/schemas/review-decision.schema.json"
        ),
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=_default_path("ETL_DECISIONS", "/catalogs/review_decisions.jsonl"),
    )
    parser.add_argument(
        "--openai-root",
        type=Path,
        default=_default_path("ETL_OPENAI_ROOT", "/inputs/openai"),
    )
    parser.add_argument(
        "--reconstructions",
        type=Path,
        help="Reviewed JSONL row additions/splits; immutable MinerU RAW remains unchanged.",
    )
    parser.add_argument(
        "--output-root", type=Path, default=_default_path("ETL_OUTPUT_ROOT", "/outputs")
    )
    parser.add_argument(
        "--publish-root",
        type=Path,
        default=_default_path("ETL_PUBLISH_ROOT", "/outputs/latest"),
    )
    parser.add_argument(
        "--logs-root", type=Path, default=_default_path("ETL_LOGS_ROOT", "/etl-logs")
    )
    parser.add_argument(
        "--use-openai",
        action="store_true",
        help="Read a verified existing OpenAI run; never calls the API.",
    )
    parser.add_argument(
        "--use-page-completeness",
        action="store_true",
        help="Load the separately cached visual page review.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new immutable attempt instead of reusing a valid cache hit.",
    )
    parser.add_argument("--fail-on-quality-gate", action="store_true")
    parser.add_argument(
        "--allow-partial-inventory",
        action="store_true",
        help="Generate review workbooks for an incomplete unpublished catalog; publication stays blocked.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the currently published ETL workbooks and hashes.",
    )
    arguments = parser.parse_args(argv)
    return Config(
        raw_path=arguments.raw,
        catalog_manifest_path=arguments.catalog_manifest,
        mapping_path=arguments.mapping,
        manifest_schema_path=arguments.manifest_schema,
        mapping_schema_path=arguments.mapping_schema,
        decision_schema_path=arguments.decision_schema,
        decisions_path=arguments.decisions,
        openai_root=arguments.openai_root,
        output_root=arguments.output_root,
        publish_root=arguments.publish_root,
        logs_root=arguments.logs_root,
        use_openai=arguments.use_openai,
        force=arguments.force,
        fail_on_quality_gate=arguments.fail_on_quality_gate,
        use_page_completeness=arguments.use_page_completeness,
        reconstructions_path=arguments.reconstructions,
        allow_partial_inventory=arguments.allow_partial_inventory,
    ), bool(arguments.validate_only)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config, validate_only = parse_arguments(argv)
        result = (
            validate_published_outputs(config) if validate_only else run_etl(config)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("quality_gate_failed"):
            return 3
        return 0
    except (ETLError, OSError, ValueError) as exc:
        print(f"catalog-etl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
