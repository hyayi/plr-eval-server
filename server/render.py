"""render — 서버 데이터 모델(SQL 이력·분리 디렉터리)을 lab의 렌더러에 적응시키는 어댑터.

report.py/gallery.py는 lab의 로컬 산출물(ledger 레코드 리스트, 단일 데이터셋
디렉터리)을 전제로 태어났다. 서버는 metrics.json(속성별 중첩)+meta.json이
run별로 흩어져 있고 이력은 SQL/파일에 있다. 이 모듈이 그 간극을 메운다 —
렌더러 자체(report.py/gallery.py)는 손대지 않는다("적응", 재사용 아님).

  run_ledger_records(run_dir)      → 그 run의 (속성당) ledger-equivalent 레코드
  dataset_ledger_records(root, ds) → 데이터셋의 전 run×속성 레코드 시퀀스(트렌드)
  render_run_report / render_dataset_report → report.render_html HTML 문자열
  render_run_gallery(root, run_id) → gallery.build_gallery HTML (two-root 합성)
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from server.storage import read_json

# meta.json → ledger 레코드 필드 매핑 (tests/fixtures/ledger_record_schema.py와 동일 계약)
_META_MAP = {
    "version_label": "version",
    "submitted_at": "date",
    "surface_hash": "prompt_hash",
    "model": "model",
    "dataset": "dataset",
}


def _records_from(meta: dict, metrics: dict) -> list[dict]:
    """한 run의 meta + metrics.json → 속성당 ledger-equivalent 레코드 리스트."""
    per_attr = metrics.get("attributes") if isinstance(metrics.get("attributes"), dict) else metrics
    base = {ledger_k: meta.get(meta_k) for meta_k, ledger_k in _META_MAP.items()}
    base["pipeline"] = "plr"
    records = []
    for attribute, m in (per_attr or {}).items():
        rec = dict(base)
        rec["attribute"] = attribute
        # score() 결과 지표 — report.py가 읽는 top-level 키로 전개
        for k in ("n", "accuracy", "recall", "precision", "f1", "macro_f1",
                  "bias", "confusion", "pred_unknown", "n_label_unknown",
                  "margin_stats", "quality_stats"):
            rec[k] = m.get(k)
        records.append(rec)
    return records


def run_ledger_records(run_dir: Path) -> list[dict]:
    meta = read_json(run_dir / "meta.json") or {}
    metrics = read_json(run_dir / "metrics.json") or {}
    return _records_from(meta, metrics)


def dataset_ledger_records(root: Path, dataset: str) -> list[dict]:
    """데이터셋의 전 run을 제출 순으로 순회해 ledger 리스트 합성 (트렌드용).
    run 디렉터리 이름(정렬형 run_id)이 곧 시간 순서."""
    runs_dir = root / "runs"
    out: list[dict] = []
    if not runs_dir.is_dir():
        return out
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        meta = read_json(run_dir / "meta.json")
        if not meta or meta.get("dataset") != dataset:
            continue
        out.extend(run_ledger_records(run_dir))
    return out


def render_run_report(root: Path, run_id: str) -> str:
    from evalkit.report import render_html
    return render_html(run_ledger_records(root / "runs" / run_id))


def render_dataset_report(root: Path, dataset: str) -> str:
    from evalkit.report import render_html
    return render_html(dataset_ledger_records(root, dataset))


def render_run_gallery(root: Path, run_id: str) -> str:
    """two-root 합성: crops/labels(datasets/<name>/) + attributes(runs/<id>/)를
    임시 dir에 모아 build_gallery 호출. 업로드된 py는 건드리지 않는다(크롭/라벨/
    attributes만 합성)."""
    from evalkit.gallery import build_gallery

    run_dir = root / "runs" / run_id
    meta = read_json(run_dir / "meta.json") or {}
    ds_dir = root / "datasets" / str(meta.get("dataset", ""))
    if not (run_dir / "attributes.jsonl").exists() or not ds_dir.is_dir():
        raise FileNotFoundError(f"gallery inputs missing for run {run_id!r}")

    tmp = Path(tempfile.mkdtemp(prefix="gallery-"))
    try:
        # dataset 쪽: crops/, labels.jsonl, manifest.yaml (build_gallery가 attribute_spec에 필요)
        shutil.copytree(ds_dir / "crops", tmp / "crops")
        for name in ("labels.jsonl", "manifest.yaml"):
            src = ds_dir / name
            if src.exists():
                shutil.copy2(src, tmp / name)
        # run 쪽: attributes.jsonl (예측 재추출 원천)
        shutil.copy2(run_dir / "attributes.jsonl", tmp / "attributes.jsonl")
        out = tmp / "gallery.html"
        build_gallery(tmp, out_path=out)
        return out.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
