"""Gallery HTML (crops vs labels) + precision/F1 metrics.

No GPU, no DB, no Redis.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image

_LAB_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_LAB_ROOT))


def _make_ds(base: Path) -> Path:
    (base / "crops").mkdir(parents=True)
    rows = [
        ("a", "female", "female", 0.9),
        ("b", "female", "male", 0.2),   # wrong
        ("c", "male", "male", 0.8),
    ]
    with open(base / "labels.jsonl", "w", encoding="utf-8") as f:
        for oid, lab, _p, _m in rows:
            f.write(json.dumps({"obj_id": oid, "label": lab}) + "\n")
    with open(base / "predictions.jsonl", "w", encoding="utf-8") as f:
        for oid, _l, pred, m in rows:
            f.write(json.dumps({"obj_id": oid, "pred": pred, "margin": m,
                                "quality": 0.5}) + "\n")
    for oid, *_ in rows:
        Image.new("RGB", (60, 90), (100, 100, 100)).save(
            str(base / "crops" / f"{oid}.jpg"), format="JPEG")
    return base


def test_gallery_builds_wrong_first(tmp_path: Path) -> None:
    from evalkit.gallery import build_gallery

    ds = _make_ds(tmp_path / "ds")
    out = Path(build_gallery(ds))
    html_text = out.read_text(encoding="utf-8")

    assert "data:image/jpeg;base64," in html_text, "thumbnails must be inlined"
    assert html_text.count('class="card') == 3
    # wrong card (obj b) must appear BEFORE the correct ones
    assert html_text.index('>b<') < html_text.index('>a<'), "wrong-first ordering"
    assert "WRONG" in html_text and "CORRECT" in html_text


def test_run_eval_precision_f1(tmp_path: Path) -> None:
    ds = _make_ds(tmp_path / "ds")
    from tests.scoring_helper import score_record
    rec = score_record(ds, "gender")
    # labels: a=female b=female c=male; preds: a=female b=male c=male
    # female: recall 1/2, precision 1/1 -> f1 = 2*0.5*1/(1.5) = 0.6667
    # male:   recall 1/1, precision 1/2 -> f1 = 0.6667
    assert rec["precision"] == {"female": 1.0, "male": 0.5}
    assert rec["recall"] == {"female": 0.5, "male": 1.0}
    assert abs(rec["f1"]["female"] - 0.6667) < 1e-3
    assert abs(rec["macro_f1"] - 0.6667) < 1e-3


def test_report_renders_summary_and_confusion(tmp_path: Path) -> None:
    from evalkit.report import build_report

    ledger = tmp_path / "ledger.jsonl"
    rec = {"attribute": "gender", "version": "v1", "date": "2026-07-02T00:00:00",
           "n": 3, "accuracy": 0.66, "macro_f1": 0.6667,
           "recall": {"female": 0.5}, "precision": {"female": 1.0},
           "f1": {"female": 0.6667},
           "confusion": {"female": {"female": 1, "male": 1}, "male": {"male": 1}},
           "bias": None, "dataset": "ds", "model": "mock", "pipeline": "plr",
           "prompt_hash": "x"}
    ledger.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = tmp_path / "r.html"
    build_report(str(ledger), str(out))
    text = out.read_text(encoding="utf-8")
    assert "전체 실험 비교" in text and "Confusion" in text and "macro F1" in text


def test_report_compare_two_ledgers(tmp_path: Path) -> None:
    from evalkit.report import build_report

    base = {"attribute": "gender", "date": "2026-07-02T00:00:00", "n": 3,
            "accuracy": 0.5, "dataset": "ds", "model": "mock",
            "pipeline": "plr", "prompt_hash": "x", "recall": {}, "confusion": {}}
    a = tmp_path / "a.jsonl"; b = tmp_path / "b.jsonl"
    a.write_text(json.dumps({**base, "version": "vA"}) + "\n", encoding="utf-8")
    b.write_text(json.dumps({**base, "version": "vB"}) + "\n", encoding="utf-8")
    out = tmp_path / "r.html"
    build_report(str(a), str(out), compare_ledger=str(b))
    text = out.read_text(encoding="utf-8")
    assert "vA" in text and "vB" in text and "비교 대상 실험군" in text
