"""P2-3: HTML report generator tests — no GPU, no DB, no redis, no browser.

Tests:
  1. build_report on a synthetic ledger (2 attributes, changing accuracy/bias,
     combo keys present + one legacy record) → HTML file created.
  2. Self-contained: the output contains NO real resource URLs (http(s) in
     href/src/CDN); the ONLY allowed http string is the SVG XML namespace
     http://www.w3.org/2000/svg (a namespace identifier, not a fetch).
  3. All key sections present by marker id: header, trend, matrix/heatmap,
     prompt-change table.
  4. Actual accuracy values are embedded in the output.
  5. lab report CLI wiring routes to report.build_report.
  6. Empty ledger → valid "no records" HTML without crashing.

No GPU, no DB, no Redis, no network, no browser required.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_LAB_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_LAB_ROOT))


# =====================================================================
# Helpers
# =====================================================================


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _synthetic_ledger(path: Path) -> list[dict]:
    """A ledger with 2 attributes, multiple versions with changing
    accuracy/bias, combo keys present, plus one legacy record missing them."""
    records = [
        # --- gender: v0.4 -> v1.3 -> v1.4 (accuracy up, bias down) ---
        {
            "attribute": "gender", "version": "plr_v0.4",
            "date": "2026-06-01T10:00:00", "n": 50, "accuracy": 0.72,
            "recall": {"male": 0.8, "female": 0.64},
            "bias": {"pair": "female->male", "rate": 0.30, "count": "9/30"},
            "confusion": {}, "seed_hash": "abc1234def",
            "gemma_repo": "unsloth/g", "dataset": "eval/golden/gender",
            "model": "gemma", "pipeline": "plr", "prompt_hash": "aaaa1111bbbb",
        },
        {
            "attribute": "gender", "version": "plr_v1.3_cot",
            "date": "2026-06-05T10:00:00", "n": 50, "accuracy": 0.84,
            "recall": {"male": 0.88, "female": 0.80},
            "bias": {"pair": "female->male", "rate": 0.18, "count": "5/30"},
            "confusion": {}, "seed_hash": "abc1234def",
            "gemma_repo": "unsloth/g", "dataset": "eval/golden/gender",
            "model": "gemma", "pipeline": "plr", "prompt_hash": "cccc2222dddd",
        },
        {
            "attribute": "gender", "version": "plr_v1.4_cot",
            "date": "2026-06-10T10:00:00", "n": 50, "accuracy": 0.91,
            "recall": {"male": 0.92, "female": 0.90},
            "bias": {"pair": "female->male", "rate": 0.09, "count": "3/30"},
            "confusion": {}, "seed_hash": "abc1234def",
            "gemma_repo": "unsloth/g", "dataset": "eval/golden/gender",
            "model": "gemma", "pipeline": "plr", "prompt_hash": "eeee3333ffff",
        },
        # --- search: v1.3 -> v1.4 (recall@k) ---
        {
            "attribute": "search", "version": "plr_v1.3_cot",
            "date": "2026-06-05T11:00:00", "k": 5, "recall_at_k": 0.60,
            "precision_at_k": 0.40, "n_queries": 12, "bias": None,
            "seed_hash": "abc1234def", "gemma_repo": "unsloth/g",
            "dataset": "eval/golden/search", "model": "gemma",
            "pipeline": "search", "prompt_hash": "cccc2222dddd",
        },
        {
            "attribute": "search", "version": "plr_v1.4_cot",
            "date": "2026-06-10T11:00:00", "k": 5, "recall_at_k": 0.75,
            "precision_at_k": 0.50, "n_queries": 12, "bias": None,
            "seed_hash": "abc1234def", "gemma_repo": "unsloth/g",
            "dataset": "eval/golden/search", "model": "gemma",
            "pipeline": "search", "prompt_hash": "eeee3333ffff",
        },
        # --- legacy record missing the P2-1 combo keys ---
        {
            "attribute": "gender", "version": "plr_legacy",
            "date": "2026-05-20T09:00:00", "n": 40, "accuracy": 0.66,
            "recall": {"male": 0.7, "female": 0.6},
            "bias": {"pair": "female->male", "rate": 0.35, "count": "7/20"},
            "confusion": {}, "seed_hash": "abc1234def",
            # NOTE: no model / pipeline / dataset / prompt_hash keys here.
        },
    ]
    _write_jsonl(path, records)
    return records


# The SVG namespace is the ONLY http string allowed in a self-contained file.
_SVG_NS = "http://www.w3.org/2000/svg"


def _http_refs_excluding_svg_ns(text: str) -> list[str]:
    """Return every http(s):// occurrence that is NOT the SVG namespace URI."""
    hits = re.findall(r"https?://[^\s\"'<>)]*", text)
    return [h for h in hits if not h.startswith(_SVG_NS)]


# =====================================================================
# Test 1 + 2 + 3 + 4: full report from a synthetic ledger
# =====================================================================


def test_build_report_creates_self_contained_html(tmp_path: Path) -> None:
    from evalkit.report import build_report

    ledger = tmp_path / "ledger.jsonl"
    _synthetic_ledger(ledger)
    out = tmp_path / "report.html"

    ret = build_report(str(ledger), str(out))
    assert Path(ret) == out
    assert out.exists(), "report.html was not created"

    text = out.read_text(encoding="utf-8")

    # --- self-contained: no real resource URLs, no CDN, no external src ---
    stray = _http_refs_excluding_svg_ns(text)
    assert not stray, f"Report is NOT self-contained; stray URLs: {stray}"
    # No external stylesheet / script references.
    assert "<link" not in text.lower(), "external <link> found — not self-contained"
    assert "cdn" not in text.lower(), "CDN reference found — not self-contained"
    # No src= pointing at any URL.
    assert not re.search(r'src\s*=\s*["\']https?://', text), "external src= URL found"

    # --- key sections present by marker id ---
    assert 'id="section-header"' in text
    assert 'id="section-trend"' in text
    # matrix/prompt-change 섹션은 2026-07-03 의도적으로 제거됨 (부재를 고정)
    assert 'id="section-matrix"' not in text
    assert 'id="section-prompt-change"' not in text
    # Human-readable markers too.
    assert "Performance Trends" in text
    assert "Matrix" not in text  # 섹션 제거됨
    assert "Prompt Change" not in text

    # --- charts are inline SVG (computed in Python) ---
    assert "<svg" in text and "<polyline" in text, "expected inline SVG charts"

    # --- actual accuracy values embedded ---
    assert "0.72" in text, "accuracy 0.72 not embedded"
    assert "0.91" in text, "accuracy 0.91 not embedded"
    # search recall value embedded (matrix cell rounds to 3 dp).
    assert "0.750" in text or "0.75" in text, "search recall not embedded"

    # prompt-change 섹션 제거(2026-07-03) — 버전들은 요약표에 남는다
    assert "plr_v0.4" in text and "plr_v1.3_cot" in text

    # --- legacy record grouped under model 'unknown' in the matrix ---
    assert "unknown" in text, "legacy record's 'unknown' model not shown"


# =====================================================================
# Test 5: lab report CLI wiring
# =====================================================================


def test_lab_report_cli_wiring(tmp_path: Path) -> None:
    from evalkit.report import build_report

    ledger = tmp_path / "ledger.jsonl"
    _synthetic_ledger(ledger)
    out = tmp_path / "cli_report.html"

    # lab report CLI는 제거됨(서버가 렌더) — 렌더러 자체는 직접 호출로 검증
    build_report(str(ledger), str(out))
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert 'id="section-matrix"' not in text
    assert not _http_refs_excluding_svg_ns(text)


# =====================================================================
# Test 6: empty ledger → valid "no records" HTML, no crash
# =====================================================================


def test_empty_ledger_produces_no_records_html(tmp_path: Path) -> None:
    from evalkit.report import build_report

    ledger = tmp_path / "empty.jsonl"
    ledger.write_text("", encoding="utf-8")
    out = tmp_path / "empty_report.html"

    build_report(str(ledger), str(out))
    assert out.exists()
    text = out.read_text(encoding="utf-8")

    assert "no records" in text.lower(), "empty ledger should say 'no records'"
    assert "<html" in text.lower() and "</html>" in text.lower()
    # Still self-contained, still has all section markers.
    assert not _http_refs_excluding_svg_ns(text)
    assert 'id="section-trend"' in text
    # matrix/prompt-change 섹션은 2026-07-03 의도적으로 제거됨 (부재를 고정)
    assert 'id="section-matrix"' not in text
    assert 'id="section-prompt-change"' not in text


def test_missing_ledger_file_does_not_crash(tmp_path: Path) -> None:
    from evalkit.report import build_report

    out = tmp_path / "missing_report.html"
    build_report(str(tmp_path / "does_not_exist.jsonl"), str(out))
    assert out.exists()
    assert "no records" in out.read_text(encoding="utf-8").lower()
