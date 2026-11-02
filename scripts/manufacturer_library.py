"""Persistent manufacturer, catalog and product inventory for the workbench."""

from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from openpyxl import load_workbook

# An extensible directory of cutting-tool manufacturers, not a claim that every
# manufacturer worldwide is known. Each receives a separate mapping profile.
DIRECTORY = [
    "Gühring",
    "SOWIN",
    "PROMATOOL",
    "Sandvik Coromant",
    "Seco",
    "Walter",
    "Kennametal",
    "WIDIA",
    "ISCAR",
    "TaeguTec",
    "Tungaloy",
    "Mitsubishi Materials",
    "Sumitomo Electric",
    "Kyocera",
    "OSG",
    "NACHI",
    "YAMAWA",
    "Union Tool",
    "YG-1",
    "Korloy",
    "ZCC-CT",
    "Hoffmann GARANT",
    "Dormer Pramet",
    "CERATIZIT",
    "KOMET",
    "TEAM CUTTING TOOLS",
    "MAPAL",
    "EMUGE-FRANKEN",
    "HORN",
    "HAIMER",
    "SCHUNK",
    "REGO-FIX",
    "BIG DAISHOWA",
    "NT TOOL",
    "LMT Tools",
    "FRAISA",
    "Mikron Tool",
    "DIXI",
    "WNT",
    "VARGUS",
    "Carmex",
    "Allied Machine",
    "Harvey Tool",
    "Helical Solutions",
    "AccuPro",
    "Ingersoll",
    "M.A. Ford",
    "SGS",
    "Alpen",
    "Maykestag",
    "Stock",
    "RUKO",
    "Dijet",
    "Ewe",
    "Boehlerit",
]


def slug(value):
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    result = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return result or "manufacturer-" + hashlib.sha256(value.encode()).hexdigest()[:10]


def key(value):
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()
    ).casefold()


class Library:
    def __init__(self, root: Path, mappings: Path):
        self.root, self.mappings = root, mappings
        root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS manufacturers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, mapping_path TEXT);
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalogs (
                    id TEXT PRIMARY KEY, manufacturer_id TEXT NOT NULL REFERENCES manufacturers(id),
                    sha256 TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, pages INTEGER NOT NULL,
                    revision TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'registered', last_run TEXT,
                    UNIQUE(manufacturer_id, sha256));
                CREATE TABLE IF NOT EXISTS products (
                    catalog_id TEXT NOT NULL REFERENCES catalogs(id), version_id TEXT NOT NULL,
                    name_key TEXT NOT NULL, signature TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(catalog_id,version_id));
            """)
            for name in DIRECTORY:
                db.execute(
                    "INSERT OR IGNORE INTO manufacturers(id,name) VALUES (?,?)",
                    (slug(name), name),
                )
            for path in sorted(mappings.glob("*.yaml")):
                mapping = yaml.safe_load(path.read_text())
                name = mapping["manufacturer"]
                db.execute(
                    "INSERT OR IGNORE INTO manufacturers(id,name) VALUES (?,?)",
                    (slug(name), name),
                )
                db.execute(
                    "UPDATE manufacturers SET mapping_path=? WHERE name=? AND mapping_path IS NULL",
                    (str(path), name),
                )
            db.execute(
                "INSERT OR IGNORE INTO preferences VALUES ('target_manufacturer','guhring')"
            )

    def connect(self):
        db = sqlite3.connect(self.root / "library.sqlite", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def manufacturers(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute("""SELECT m.*, COUNT(c.id) catalog_count,
                SUM(CASE WHEN c.status='validated' THEN 1 ELSE 0 END) validated_count
                FROM manufacturers m LEFT JOIN catalogs c ON c.manufacturer_id=m.id
                GROUP BY m.id ORDER BY m.name COLLATE NOCASE""")
            ]

    def manufacturer(self, id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM manufacturers WHERE id=?", (id,)).fetchone()
            if row is None:
                raise ValueError("Choose a manufacturer from the library.")
            return dict(row)

    def add_manufacturer(self, name):
        name = re.sub(r"\s+", " ", name.strip())
        if not 1 <= len(name) <= 100:
            raise ValueError("Manufacturer name must contain 1–100 characters.")
        with self.connect() as db:
            for row in db.execute("SELECT * FROM manufacturers"):
                if key(row["name"]) == key(name):
                    return dict(row)
            id = slug(name)
            if db.execute("SELECT 1 FROM manufacturers WHERE id=?", (id,)).fetchone():
                id += "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
            db.execute("INSERT INTO manufacturers(id,name) VALUES (?,?)", (id, name))
        return self.manufacturer(id)

    def target(self, id=None):
        if id is not None:
            self.manufacturer(id)
            with self.connect() as db:
                db.execute(
                    "UPDATE preferences SET value=? WHERE key='target_manufacturer'",
                    (id,),
                )
        with self.connect() as db:
            return db.execute(
                "SELECT value FROM preferences WHERE key='target_manufacturer'"
            ).fetchone()[0]

    def mapping(self, id):
        manufacturer = self.manufacturer(id)
        path = (
            Path(manufacturer["mapping_path"]) if manufacturer["mapping_path"] else None
        )
        if path is not None and path.is_file():
            result = yaml.safe_load(path.read_text())
        else:
            result = {
                "mapping_schema_version": "1.0",
                "mapping_profile": id,
                "mapping_version": "0.1.0",
                "manufacturer": manufacturer["name"],
                "series": [],
                "families": [],
                "rules": [],
                "matcher_contract": {
                    "version": "1.0",
                    "status": "provisional",
                    "wide_fields": [],
                },
            }
        # Old profiles specify field codes, but do not establish what each
        # geometric label means in every series. Do not guess those meanings.
        result.setdefault("symbol_dictionary", [])
        return result

    def save_mapping(self, id, mapping):
        manufacturer = self.manufacturer(id)
        validate_mapping(mapping)
        if mapping["manufacturer"] != manufacturer["name"]:
            raise ValueError("A mapping belongs to exactly one manufacturer.")
        for other in self.manufacturers():
            if other["id"] != id and other["mapping_path"]:
                existing = yaml.safe_load(Path(other["mapping_path"]).read_text())
                if existing["mapping_profile"] == mapping["mapping_profile"]:
                    raise ValueError(
                        "Each manufacturer must have its own mapping profile identifier."
                    )
        mapping = json.loads(json.dumps(mapping))
        previous = self.mapping(id)
        if (
            mapping != previous
            and mapping["mapping_version"] == previous["mapping_version"]
        ):
            version = mapping["mapping_version"].split(".")
            if len(version) == 3 and all(part.isdigit() for part in version):
                mapping["mapping_version"] = ".".join(
                    version[:2] + [str(int(version[2]) + 1)]
                )
        path = self.root / "mappings" / f"{id}.yaml"
        path.parent.mkdir(exist_ok=True)
        text = yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False)
        if path.exists():
            archive = path.parent / "history"
            archive.mkdir(exist_ok=True)
            old = path.read_bytes()
            (archive / f"{id}-{hashlib.sha256(old).hexdigest()}.yaml").write_bytes(old)
        temp = path.with_suffix(".tmp")
        temp.write_text(text)
        temp.replace(path)
        with self.connect() as db:
            db.execute(
                "UPDATE manufacturers SET mapping_path=? WHERE id=?", (str(path), id)
            )
        return mapping

    def register(self, manufacturer_id, path, sha, pages, revision):
        self.manufacturer(manufacturer_id)
        id = manufacturer_id + "-" + sha[:16]
        with self.connect() as db:
            db.execute(
                """INSERT INTO catalogs(id,manufacturer_id,sha256,name,path,pages,revision)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(manufacturer_id,sha256) DO UPDATE SET path=excluded.path""",
                (id, manufacturer_id, sha, path.name, str(path), pages, revision),
            )
        return id

    def catalogs(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute("""SELECT c.*,m.name manufacturer,
                (SELECT COUNT(*) FROM products p WHERE p.catalog_id=c.id) product_count
                FROM catalogs c JOIN manufacturers m ON m.id=c.manufacturer_id ORDER BY c.name""")
            ]

    def import_manifest(self, manifest, manifest_path, run=None):
        """Import historical inventory without treating its old reviews as new validation."""
        manufacturer = self.add_manufacturer(manifest["manufacturer"])
        parsed = {
            p["source_sha256"]
            for p in manifest.get("page_states", [])
            if p["status"] in ("parsed", "empty")
        }
        with self.connect() as db:
            for document in manifest["documents"]:
                path = Path(document["file"])
                if not path.is_absolute():
                    path = manifest_path.parent / path
                if not path.is_file():
                    continue
                id = manufacturer["id"] + "-" + document["sha256"][:16]
                db.execute(
                    """INSERT OR IGNORE INTO catalogs(id,manufacturer_id,sha256,name,path,pages,revision,status,last_run)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        id,
                        manufacturer["id"],
                        document["sha256"],
                        path.name,
                        str(path),
                        document["pages"],
                        manifest["revision"],
                        "previously_parsed"
                        if document["sha256"] in parsed
                        else "registered",
                        run,
                    ),
                )

    def status(self, id, status, run):
        with self.connect() as db:
            db.execute(
                "UPDATE catalogs SET status=?,last_run=? WHERE id=?", (status, run, id)
            )

    def index_workbook(self, id, path):
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:

            def rows(sheet):
                iterator = workbook[sheet].iter_rows(values_only=True)
                headers = next(iterator)
                return [dict(zip(headers, r)) for r in iterator]

            products, attributes = rows("products"), rows("attributes")
        finally:
            workbook.close()
        by_product = {}
        for a in attributes:
            by_product.setdefault(a["product_version_id"], []).append(a)
        with self.connect() as db:
            db.execute("DELETE FROM products WHERE catalog_id=?", (id,))
            for p in products:
                attrs = by_product.get(p["product_version_id"], [])
                p["attributes"] = attrs
                sig = parameter_signature(p, attrs)
                name = key(p.get("order_number") or p.get("article_number"))
                db.execute(
                    "INSERT INTO products VALUES (?,?,?,?,?)",
                    (
                        id,
                        p["product_version_id"],
                        name,
                        sig,
                        json.dumps(p, ensure_ascii=False),
                    ),
                )

    def products(self, manufacturer_id):
        self.manufacturer(manufacturer_id)
        with self.connect() as db:
            return [
                {
                    **json.loads(r["data"]),
                    "catalog_id": r["catalog_id"],
                    "catalog_name": r["catalog_name"],
                }
                for r in db.execute(
                    "SELECT p.data,p.catalog_id,c.name AS catalog_name FROM products p JOIN catalogs c ON c.id=p.catalog_id WHERE c.manufacturer_id=? ORDER BY c.name,p.version_id",
                    (manufacturer_id,),
                )
            ]

    def duplicates(self, catalog_id=None):
        with self.connect() as db:
            records = [
                dict(r)
                for r in db.execute(
                    "SELECT p.*, c.manufacturer_id FROM products p JOIN catalogs c ON c.id=p.catalog_id"
                )
            ]
        signatures, names = {}, {}
        pairs = {}
        for record in records:
            candidates = []
            if record["signature"]:
                candidates += signatures.setdefault(record["signature"], [])
            if record["name_key"]:
                candidates += names.setdefault(record["name_key"], [])
            for old in candidates:
                if catalog_id and catalog_id not in (
                    record["catalog_id"],
                    old["catalog_id"],
                ):
                    continue
                pair = tuple(
                    sorted(
                        (
                            old["catalog_id"] + ":" + old["version_id"],
                            record["catalog_id"] + ":" + record["version_id"],
                        )
                    )
                )
                equal = bool(
                    record["signature"] and record["signature"] == old["signature"]
                )
                pairs[pair] = {
                    "kind": "same_parameters"
                    if equal
                    else "same_name_conflicting_or_incomplete_parameters",
                    "left": {
                        "catalog_id": old["catalog_id"],
                        "product": json.loads(old["data"]),
                    },
                    "right": {
                        "catalog_id": record["catalog_id"],
                        "product": json.loads(record["data"]),
                    },
                }
            if record["signature"]:
                signatures[record["signature"]].append(record)
            if record["name_key"]:
                names[record["name_key"]].append(record)
        return list(pairs.values())


def parameter_signature(product, attributes):
    """Compare complete typed values, units, fit and tolerances; never names alone."""
    identity_fields = {
        "manufacturer",
        "article_number",
        "order_number",
        "product_name",
        "name",
        "series",
        "catalog_id",
        "catalog_revision",
    }
    attributes = [
        a
        for a in attributes
        if a.get("canonical_field") not in identity_fields
        and a.get("value_type") != "identifier"
    ]
    if not attributes or product.get("identity_status") not in (
        "identified",
        "resolved",
    ):
        return None
    values = []
    for a in attributes:
        value = a.get("value_decimal") or a.get("value_text") or a.get("value_min")
        if value is None or a.get("review_status") in ("rejected", "unresolved"):
            return None

        def normalized(v):
            if v is None:
                return ""
            try:
                return format(Decimal(str(v)).normalize(), "f")
            except InvalidOperation:
                return key(v)

        values.append(
            (
                a["canonical_field"],
                a.get("canonical_unit") or "",
                *[
                    normalized(a.get(f))
                    for f in (
                        "value_text",
                        "value_decimal",
                        "value_min",
                        "value_max",
                        "tolerance_lower",
                        "tolerance_upper",
                        "fit_class",
                    )
                ],
            )
        )
    payload = [
        product.get("family"),
        product.get("subfamily"),
        product.get("item_type"),
        sorted(values),
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def validate_mapping(mapping):
    series = {s["article_number"] for s in mapping["series"]}
    families = {f["name"] for f in mapping["families"]}
    for rule in mapping["rules"]:
        if set(rule.get("series", [])) - series:
            raise ValueError(
                "Mapping rule references unknown article-number series codes: "
                + rule["id"]
            )
        if set(rule["families"]) - families:
            raise ValueError(
                "Mapping rule references an unknown tool family: " + rule["id"]
            )
