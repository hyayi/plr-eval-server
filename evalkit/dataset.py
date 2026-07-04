"""Dataset — 선택 가능한 골든셋 디렉터리 래퍼 + 속성 스펙 리졸버.

구조는 고정(crops/+labels.jsonl+manifest.yaml), 라벨 집합은 작성자의 것:
manifest가 labels/pred_path/margin_path/bias_pair/object_type_hint를
선언하면 커스텀 속성이 그대로 동작하고, gender/vehicle_type/military는
내장 프리셋(PRESET_SPECS — 선언 불필요한 참고 예시)이다.

(원문) a selectable golden-set directory for the PLR lab.

A dataset directory is a self-describing bundle of the files the eval cycle
already uses, so the existing ``eval/golden/<attribute>/`` layout is itself a
valid dataset. That keeps every current command and test working unchanged
while letting callers point ``--dataset`` at an arbitrary path.

Layout (all optional except crops/ + labels for a real run)::

    <dataset>/
        crops/<obj_id>.jpg      # object crops (gitignored in the repo layout)
        labels.jsonl            # {"obj_id": ..., "label": ...}  human ground truth
        predictions.jsonl       # {"obj_id": ..., "pred": ...}   model output
        attributes.jsonl        # {"obj_id": ..., "plr_json": {...}}
        manifest.yaml           # {attribute, n, created, source_note}

``Dataset(path)`` only wraps paths; it does not read the jsonl bodies (the
runners already do that). Accessors return ``pathlib.Path`` objects so callers
can pass ``str(ds.crops_dir)`` etc. to the existing runners.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Dataset:
    """A selectable golden-set directory.

    Args:
      path: dataset directory. The existing ``eval/golden/<attribute>/`` dirs
            are valid datasets.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    # -- path accessors --------------------------------------------------

    @property
    def crops_dir(self) -> Path:
        return self.path / "crops"

    @property
    def labels_path(self) -> Path:
        return self.path / "labels.jsonl"

    @property
    def predictions_path(self) -> Path:
        return self.path / "predictions.jsonl"

    @property
    def attributes_path(self) -> Path:
        return self.path / "attributes.jsonl"

    @property
    def queries_path(self) -> Path:
        return self.path / "queries.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.yaml"

    # -- content accessors -----------------------------------------------

    @property
    def manifest(self) -> dict[str, Any]:
        """Parsed manifest.yaml, or {} if absent.

        Fields (all optional): attribute, n, created, source_note.
        """
        if not self.manifest_path.exists():
            return {}
        import yaml

        with open(self.manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}

    def obj_ids(self) -> list[str]:
        """obj_ids for this dataset, read from predictions.jsonl (the file that
        seeds the golden obj_id set — mirrors re_score's contract). Falls back
        to labels.jsonl if predictions.jsonl is absent. Returns [] if neither
        exists. Order is preserved from the file for deterministic output."""
        for p in (self.predictions_path, self.labels_path):
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    return [
                        json.loads(line)["obj_id"]
                        for line in f
                        if line.strip()
                    ]
        return []

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Dataset({str(self.path)!r})"


def load_labels(
    dataset_dir: str | Path, attribute: str | None = None
) -> dict[str, str]:
    """labels.jsonl → {obj_id: label} — 단일/다속성 두 형식을 모두 수용.

    지원 형식 (한 파일 안에 혼재 가능):
      단일(legacy):  {"obj_id": "M3", "label": "male"}
                     → 어떤 attribute 요청에도 그 라벨을 반환 (기존 데이터셋 무변경)
      다속성:        {"obj_id": "M3", "labels": {"gender": "male", "upper_color": "black"}}
                     → labels[attribute]만 반환; 그 속성 라벨이 없는 행은 제외
                       (= 미라벨 크롭: 채점 조인에서 자연 탈락, unknown과 구분됨)

    입력/출력 예) load_labels(ds, "gender")
      → {"M3": "male", "M7": "unknown"}   (M9가 gender 키를 안 가지면 M9 없음)
    """
    path = Path(dataset_dir) / "labels.jsonl"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            oid = rec.get("obj_id")
            if oid is None:
                continue
            multi = rec.get("labels")
            if isinstance(multi, dict):
                if attribute is not None and multi.get(attribute) is not None:
                    out[str(oid)] = str(multi[attribute])
                continue
            single = rec.get("label") or rec.get("true")
            if single is not None:
                out[str(oid)] = str(single)
    return out


def load_object_types(dataset_dir: str | Path) -> dict[str, str]:
    """labels.jsonl 행의 선택 필드 object_type → {obj_id: "person"|"vehicle"}.

    사람/차량이 섞인 데이터셋의 크롭별 프롬프트 선택 근거 — 운영(core/ir)에서
    트래커 클래스가 객체마다 힌트를 주는 것의 lab 대응물. 필드가 없는 행은
    manifest/프리셋의 object_type_hint 폴백을 쓴다 (단일 종 데이터셋 무변경).

    입력/출력 예) {"obj_id":"V1","object_type":"vehicle","labels":{...}}
      → {"V1": "vehicle"}
    """
    path = Path(dataset_dir) / "labels.jsonl"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            oid, ot = rec.get("obj_id"), rec.get("object_type")
            if oid is not None and isinstance(ot, str) and ot.strip():
                out[str(oid)] = ot.strip().lower()
    return out


def labeled_attributes(dataset_dir: str | Path) -> list[str]:
    """labels.jsonl의 다속성 행들에 실제로 등장하는 속성 키 목록 (등장 순서).

    legacy 단일 label 행은 특정 속성에 묶이지 않으므로 여기 집계되지 않는다
    — 그런 데이터셋은 manifest의 attribute 선언이 기준.
    """
    path = Path(dataset_dir) / "labels.jsonl"
    seen: dict[str, None] = {}
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            multi = json.loads(line).get("labels")
            if isinstance(multi, dict):
                for k in multi:
                    seen.setdefault(str(k))
    return list(seen)


def eval_attributes(dataset_dir: str | Path) -> tuple[list[str], list[str], list[str]]:
    """"라벨이 실제로 있는 평가 속성"의 단일 정의 — lab eval-all과 평가 서버가
    같은 헬퍼를 쓴다 (선택 로직이 갈리면 lab↔서버 지표 비교가 무의미).

    Returns (attributes, skipped, undeclared):
      attributes — 평가할 속성 (manifest 선언 ∩ 라벨 등장; 선언 없으면 라벨 키,
                   다속성 라벨 없으면 선언 그대로 — legacy 단일 label 데이터셋)
      skipped    — 선언됐지만 라벨이 아직 없는 속성 (실패 아님 — 안내 대상)
      undeclared — 라벨에는 있는데 manifest 미선언 (오타 의심 — 경고 대상)
    """
    declared = declared_attributes(dataset_dir)
    labeled = labeled_attributes(dataset_dir)
    if declared and labeled:
        attributes = [a for a in declared if a in labeled]
        skipped = [a for a in declared if a not in attributes]
        undeclared = [a for a in labeled if a not in declared]
    elif declared:      # legacy 단일 label 행뿐 → 선언 기준
        attributes, skipped, undeclared = declared, [], []
    else:               # manifest 선언 없음 → 라벨 키 기준
        attributes, skipped, undeclared = labeled, [], []
    return attributes, skipped, undeclared


def declared_attributes(dataset_dir: str | Path) -> list[str]:
    """manifest가 선언한 평가 속성 목록 — `lab eval --attribute all`의 순회 대상.

    manifest.yaml의 `attributes:` 맵(다속성) 키들이 우선, 없으면 단일
    `attribute:` 필드, 둘 다 없으면 [].
    """
    manifest = Dataset(Path(dataset_dir)).manifest
    attrs = manifest.get("attributes")
    if isinstance(attrs, dict) and attrs:
        return [str(k) for k in attrs]
    single = manifest.get("attribute")
    return [str(single)] if single else []


def resolve_dataset_dir(
    lab_root: str | os.PathLike[str],
    attribute: str,
    dataset: str | None = None,
) -> Path:
    """Resolve the dataset directory for a command.

    If ``dataset`` is given, use it verbatim. Otherwise fall back to the
    backward-compatible ``<lab_root>/eval/golden/<attribute>/`` layout (which is
    itself a valid dataset dir), so existing commands and tests keep working.
    """
    if dataset:
        return Path(dataset)
    return Path(lab_root) / "eval" / "golden" / attribute


# =====================================================================
# Attribute spec — manifest-driven labels (generic) with PLR presets
# =====================================================================
# The dataset template fixes the STRUCTURE (crops/ + labels.jsonl +
# manifest.yaml); the LABEL SET is the user's choice. A dataset declares its
# own attribute in manifest.yaml:
#
#   attribute: helmet
#   labels: [helmet, no_helmet]                  # allowed label values
#   pred_path: attributes.equipment[0].type      # where the pred lives in PLR JSON
#   margin_path: attributes.gender_scores.decision_margin   # optional
#   bias_pair: [no_helmet, helmet]               # optional headline bias
#   object_type_hint: person                     # optional (default person)
#
# The three original PLR attributes (gender / vehicle_type / military) are
# built-in PRESETS — they work without any manifest declaration and serve as
# the reference examples of the scheme.

PRESET_SPECS: dict[str, dict[str, Any]] = {
    "gender": {
        "labels": ("male", "female", "unknown"),
        "pred_path": "attributes.gender_scores.selected",
        "margin_path": "attributes.gender_scores.decision_margin",
        "bias_pair": ("female", "male"),
        "object_type_hint": "person",
    },
    "vehicle_type": {
        "labels": None,  # validate.py keeps its preset vocabulary
        "pred_path": "attributes.type_topk[0].label",
        "margin_path": None,
        "bias_pair": None,
        "object_type_hint": "vehicle",
    },
    "military": {
        "labels": ("military", "civilian", "unknown"),
        "pred_path": "attributes.military",
        "margin_path": None,
        "bias_pair": None,
        "object_type_hint": "person",
    },
}


def resolve_json_path(data: Any, dotted: str) -> Any:
    """점표기+[인덱스] 경로를 중첩 dict/list에 적용 — manifest pred_path의 해석기.
    누락 단계는 None (모델 출력이 필드를 빼먹어도 무예외).

    입력/출력 예) resolve_json_path(plr_json, "attributes.equipment[0].type")
      → "helmet"   /   없는 경로 → None
    """
    cur = data
    for raw in dotted.split("."):
        seg = raw
        idxs: list[int] = []
        while seg.endswith("]") and "[" in seg:
            seg, _, tail = seg.rpartition("[")
            idxs.insert(0, int(tail[:-1]))
        if seg:
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
        for i in idxs:
            if not isinstance(cur, (list, tuple)) or i >= len(cur):
                return None
            cur = cur[i]
    return cur


def attribute_spec(dataset_dir: str | Path, attribute: str) -> dict[str, Any]:
    """Effective spec for one attribute: manifest declaration wins, preset
    fills the gaps, unknown attribute without a manifest declaration gets an
    empty spec (callers fail loudly where a field is required).

    다속성 manifest는 `attributes:` 맵으로 속성별 스펙을 선언한다::

        attributes:
          gender: {}                       # 프리셋 그대로 (빈 dict)
          upper_color:
            labels: [black, white, red]
            pred_path: attributes.upper_clothing.primary_color

    맵 항목이 있으면 그 속성에 한해 프리셋을 덮어쓴다. 단일 `attribute:` +
    최상위 labels/pred_path/... 필드(legacy)도 계속 동작.
    """
    base = dict(PRESET_SPECS.get(attribute) or {
        "labels": None, "pred_path": None, "margin_path": None,
        "bias_pair": None, "object_type_hint": "person",
    })
    try:
        manifest = Dataset(Path(dataset_dir)).manifest
    except Exception:  # noqa: BLE001 — malformed manifest is validate's job
        manifest = {}
    attrs_map = manifest.get("attributes")
    if isinstance(attrs_map, dict) and isinstance(attrs_map.get(attribute), dict):
        for key, val in attrs_map[attribute].items():
            if val is not None:
                base[key] = val
        return base
    if manifest.get("attribute") == attribute or "attribute" not in manifest:
        for key in ("labels", "pred_path", "margin_path", "bias_pair", "object_type_hint"):
            if manifest.get(key) is not None:
                base[key] = manifest[key]
    return base
