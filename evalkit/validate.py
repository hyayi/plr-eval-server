"""Dataset validator for the PLR prompt lab.

Checks a dataset directory against the contract documented in DATASET_SPEC.md.
Each check prints a human-readable PASS/WARN/FAIL line. Exits non-zero on any
error (FAIL). Warnings do not block exit 0 unless --strict is requested.

Usage (programmatic):
    from validate import validate_dataset
    ok = validate_dataset("/path/to/dataset")   # returns True iff no errors

Usage (CLI entry-point — wired via lab.py validate-dataset):
    python3 lab.py validate-dataset <path>
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plr_schema import (
    GENDER_ENUM,
    MILITARY_ENUM,
    VEHICLE_TYPE_ENUM,
)

# ---------------------------------------------------------------------------
# Allowed label vocabularies per attribute.
# These are the values that validate_dataset accepts without error.
# ---------------------------------------------------------------------------

_ATTR_VOCAB: dict[str, frozenset[str]] = {
    "gender": frozenset((*GENDER_ENUM, "unknown")),
    "vehicle_type": frozenset(VEHICLE_TYPE_ENUM),
    "military": frozenset(MILITARY_ENUM),
}

# Required fields in manifest.yaml
_MANIFEST_REQUIRED = ("attribute", "n", "created", "source_note")

# Required fields per labels.jsonl line
_LABELS_REQUIRED = ("obj_id", "label")


# ---------------------------------------------------------------------------
# Result accumulator
# ---------------------------------------------------------------------------


@dataclass
class _Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        print(f"  FAIL  {msg}")
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        print(f"  WARN  {msg}")
        self.warnings.append(msg)

    def ok(self, msg: str) -> None:
        print(f"  PASS  {msg}")

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_manifest(ds_path: Path, report: _Report) -> dict[str, Any]:
    """Check manifest.yaml: present, parses, has required fields.

    Returns parsed manifest dict (or {} on failure) so later checks can
    read the attribute field.
    """
    mpath = ds_path / "manifest.yaml"
    if not mpath.exists():
        report.error("manifest.yaml not found")
        return {}

    try:
        import yaml  # type: ignore[import]
        with open(mpath, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            report.error("manifest.yaml does not parse to a YAML mapping")
            return {}
    except Exception as exc:  # noqa: BLE001
        report.error(f"manifest.yaml parse error: {exc}")
        return {}

    missing = [k for k in _MANIFEST_REQUIRED if k not in data]
    # 다속성 manifest: 단일 `attribute:` 대신 `attributes:` 맵도 인정.
    if "attribute" in missing and isinstance(data.get("attributes"), dict) and data["attributes"]:
        missing.remove("attribute")
    if missing:
        report.error(f"manifest.yaml missing required fields: {missing}")
    else:
        attr_desc = (data.get("attribute")
                     or "+".join(str(k) for k in (data.get("attributes") or {})))
        report.ok(
            f"manifest.yaml valid (attribute={attr_desc!r}, n={data.get('n')})"
        )
    return data


def _check_labels(
    ds_path: Path,
    attribute: str | None,
    report: _Report,
    declared_labels: list | None = None,
    vocab_map: dict[str, frozenset[str] | None] | None = None,
) -> set[str]:
    """Check labels.jsonl: present, each line valid JSON with required fields,
    labels in allowed vocabulary.

    Returns the set of obj_ids found in labels.jsonl (empty set on absence/error).
    """
    lpath = ds_path / "labels.jsonl"
    if not lpath.exists():
        report.error("labels.jsonl not found")
        return set()

    # Label vocabulary: the dataset's own manifest declaration wins (generic
    # datasets bring their own label set); the built-in PLR presets remain as
    # the fallback for gender/vehicle_type/military. Unknown attribute with
    # no declaration -> no vocabulary check (warned at the manifest step).
    if declared_labels:
        vocab: frozenset[str] | None = frozenset(str(x) for x in declared_labels)
    else:
        vocab = _ATTR_VOCAB.get(attribute or "") if attribute else None

    # 다속성 행({"labels": {attr: label}})의 속성별 vocab: manifest
    # `attributes:` 맵의 labels 선언 → 프리셋 순. 선언에 없는 속성 키는
    # 오타로 간주해 error (eval 조인에서 조용히 빠지는 사고 방지).
    vocab_map = vocab_map or {}
    allowed_attrs = frozenset(vocab_map) if vocab_map else None
    obj_ids: set[str] = set()
    line_errors = 0

    def _check_one(lineno: int, oid: str, attr: str | None,
                   label: object, voc: frozenset[str] | None) -> None:
        nonlocal line_errors
        if voc is not None and label not in voc:
            report.error(
                f"labels.jsonl line {lineno}: obj_id={oid!r} "
                f"label={label!r} not in vocab for attribute={attr!r} "
                f"(allowed: {sorted(voc)})"
            )
            line_errors += 1

    with open(lpath, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                report.error(f"labels.jsonl line {lineno}: JSON parse error: {exc}")
                line_errors += 1
                continue

            ot = rec.get("object_type")
            if ot is not None and str(ot).strip().lower() not in ("person", "vehicle"):
                report.error(
                    f"labels.jsonl line {lineno}: object_type={ot!r} must be "
                    f"'person' or 'vehicle' (per-crop prompt hint for mixed datasets)"
                )
                line_errors += 1

            multi = rec.get("labels")
            if isinstance(multi, dict):
                if "obj_id" not in rec:
                    report.error(f"labels.jsonl line {lineno}: missing required fields ['obj_id']")
                    line_errors += 1
                    continue
                if not multi:
                    report.error(f"labels.jsonl line {lineno}: empty \"labels\" dict")
                    line_errors += 1
                    continue
                obj_ids.add(rec["obj_id"])
                for attr, label in multi.items():
                    if allowed_attrs is not None and attr not in allowed_attrs:
                        report.error(
                            f"labels.jsonl line {lineno}: obj_id={rec['obj_id']!r} "
                            f"attribute {attr!r} not declared in manifest "
                            f"`attributes:` (declared: {sorted(allowed_attrs)})"
                        )
                        line_errors += 1
                        continue
                    _check_one(lineno, rec["obj_id"], attr, label,
                               vocab_map.get(attr) or _ATTR_VOCAB.get(attr))
                continue

            missing = [k for k in _LABELS_REQUIRED if k not in rec]
            if missing:
                report.error(
                    f"labels.jsonl line {lineno}: missing required fields {missing}"
                )
                line_errors += 1
                continue

            obj_ids.add(rec["obj_id"])
            # Out-of-vocabulary label: error (malformed) rather than warn,
            # because it will silently break eval scoring.
            _check_one(lineno, rec["obj_id"], attribute, rec["label"], vocab)

    if line_errors == 0:
        report.ok(f"labels.jsonl: {len(obj_ids)} records, all valid")

    return obj_ids


def _check_crops(
    ds_path: Path,
    labeled_ids: set[str],
    report: _Report,
) -> set[str]:
    """Check crops/ directory: present, every labeled obj_id has a .jpg,
    report crops-without-labels (warn) and labels-without-crops (error).

    Returns the set of obj_ids found as crop files.
    """
    crops_dir = ds_path / "crops"
    if not crops_dir.exists():
        report.error("crops/ directory not found")
        return set()

    crop_ids: set[str] = {p.stem for p in crops_dir.glob("*.jpg")}

    labels_without_crops = labeled_ids - crop_ids
    crops_without_labels = crop_ids - labeled_ids

    if labels_without_crops:
        report.error(
            f"labels-without-crops ({len(labels_without_crops)} obj_ids): "
            f"{sorted(labels_without_crops)}"
        )
    if crops_without_labels:
        report.warn(
            f"crops-without-labels ({len(crops_without_labels)} obj_ids): "
            f"{sorted(crops_without_labels)} — no label assigned (not an error)"
        )

    if not labels_without_crops:
        report.ok(
            f"crops/: {len(crop_ids)} crop(s), all labeled obj_ids have a crop"
        )

    return crop_ids


def validate_dataset(path: str | Path, *, verbose: bool = True) -> bool:
    """Validate a dataset directory against the PLR dataset spec.

    Args:
        path:    Path to the dataset directory.
        verbose: If True (default), print PASS/WARN/FAIL lines to stdout.

    Returns:
        True if no errors were found (warnings allowed), False otherwise.
    """
    ds_path = Path(path)
    report = _Report()

    if verbose:
        print(f"Validating dataset: {ds_path}")

    # 1. manifest.yaml
    manifest = _check_manifest(ds_path, report)
    attribute: str | None = manifest.get("attribute") if manifest else None

    # 2. labels.jsonl
    declared = manifest.get("labels") if isinstance(manifest.get("labels"), (list, tuple)) else None
    if attribute and not declared and attribute not in _ATTR_VOCAB:
        report.warn(
            f"attribute {attribute!r} is not a built-in preset and manifest.yaml "
            "declares no `labels:` — label vocabulary cannot be checked "
            "(declare labels/pred_path for generic datasets, see docs/DATASET_SPEC.md)"
        )
    # 다속성 manifest(attributes: 맵) → 속성별 vocab 지도. 항목이 labels를
    # 선언하면 그것, 아니면 프리셋(_ATTR_VOCAB), 둘 다 없으면 검사 생략(None).
    vocab_map: dict[str, frozenset[str] | None] = {}
    attrs_map = manifest.get("attributes") if isinstance(manifest.get("attributes"), dict) else None
    if attrs_map:
        for attr, spec in attrs_map.items():
            spec = spec if isinstance(spec, dict) else {}
            lab = spec.get("labels")
            if isinstance(lab, (list, tuple)) and lab:
                vocab_map[str(attr)] = frozenset(str(x) for x in lab)
            else:
                vocab_map[str(attr)] = _ATTR_VOCAB.get(str(attr))
                if str(attr) not in _ATTR_VOCAB:
                    report.warn(
                        f"attributes.{attr}: no `labels:` and not a preset — "
                        f"label vocabulary cannot be checked"
                    )
    labeled_ids = _check_labels(ds_path, attribute, report,
                                declared_labels=declared, vocab_map=vocab_map)

    # 3. crops/
    crop_ids = _check_crops(ds_path, labeled_ids, report)

    all_dataset_ids = labeled_ids | crop_ids

    # Summary
    if verbose:
        print(
            f"\nSummary: {len(crop_ids)} crops, {len(labeled_ids)} labels, "
            f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
        )
        if report.has_errors:
            print("Result: FAIL (errors found — see above)")
        else:
            print("Result: PASS")

    return not report.has_errors
