"""Workflow lead schemas and the row validator (pure: no database)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalise_header, normalise_phone, parse_date, parse_decimal, parse_enum


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]
    _lookup: dict[str, str] = field(init=False, repr=False)

    def __post_init__(self):
        self._lookup = {}
        for f in self.fields:
            for label in (f.name, *f.aliases):
                self._lookup.setdefault(normalise_header(label), f.name)

    @property
    def reference_field(self) -> str:
        return next(f.name for f in self.fields if f.reference)

    @property
    def phone_field(self) -> str:
        """The phone field that identifies a lead (the first required phone field)."""
        return next(f.name for f in self.fields if f.type == "phone" and f.required)

    def map_headers(self, headers: list[str]) -> dict[int, str]:
        """Column index -> field name. Unknown columns are ignored; if two columns map to the
        same field, the left-most one wins."""
        mapping: dict[int, str] = {}
        taken: set[str] = set()
        for i, h in enumerate(headers):
            name = self._lookup.get(normalise_header(h or ""))
            if name and name not in taken:
                mapping[i] = name
                taken.add(name)
        return mapping

    def apply_defaults(self, values: dict) -> dict:
        out = dict(values)
        for f in self.fields:
            if out.get(f.name) is None and f.default is not None:
                out[f.name] = f.default
        return out


@dataclass
class RowResult:
    values: dict[str, str | None]  # normalised; None = missing or invalid optional
    reason: str | None = None     # set => rejected
    warnings: int = 0


def parse_value(f: Field, raw: str) -> str | None:
    """Normalise one non-empty raw value. Returns None if the value is invalid."""
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalise_phone(raw)
    if f.type == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if f.type == "decimal":
        d = parse_decimal(raw)
        return str(d) if d is not None and d >= 0 else None
    if f.type == "enum":
        return parse_enum(raw, list(f.values))
    raise ValueError(f"unknown field type {f.type!r}")


def validate_row(schema: WorkflowSchema, raw: dict[str, str]) -> RowResult:
    """Walk fields in schema order; the first failure of a required field rejects the row."""
    values: dict[str, str | None] = {}
    reason = None
    warnings = 0
    for f in schema.fields:
        text = (raw.get(f.name) or "").strip()
        if not text:
            values[f.name] = None
            if f.required and reason is None:
                reason = f"missing_required:{f.name}"
            continue
        parsed = parse_value(f, text)
        values[f.name] = parsed
        if parsed is None:
            if f.required:
                if reason is None:
                    reason = "invalid_phone" if f.type == "phone" else f"invalid:{f.name}"
            else:
                warnings += 1
    return RowResult(values, reason, 0 if reason else warnings)


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    schemas = {}
    for p in sorted(schema_dir.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        fields = [Field(name=f["name"], type=f["type"], required=f.get("required", False),
                        pii=f.get("pii", False), reference=f.get("reference", False),
                        aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                        default=f.get("default")) for f in doc["fields"]]
        schemas[doc["workflow_id"]] = WorkflowSchema(doc["workflow_id"], fields)
    return schemas


def load_dispositions(path: Path) -> list[tuple[str, str, str, int, int]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [(r["workflow_id"].strip(), r["code"].strip(), r["label"].strip(),
                 int(r["is_terminal"].strip().lower() == "true"),
                 int(r["is_success"].strip().lower() == "true"))
                for r in csv.DictReader(fh)]
