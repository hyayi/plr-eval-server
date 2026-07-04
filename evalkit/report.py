#!/usr/bin/env python3
"""report — turn the eval ledger into ONE self-contained HTML report.

Reads ``eval/ledger.jsonl`` (the append-only version×attribute×combo record
stream written by ``eval/run_eval.py``) and emits a
single ``.html`` file that opens offline by double-click.

Self-contained is a HARD requirement: NO CDN, NO external URLs, NO network.
All charts are dependency-free **inline SVG** computed in pure Python (no
matplotlib / pandas / plotly / JS chart lib). The only ``http(s)`` string the
output may contain is the SVG XML namespace ``http://www.w3.org/2000/svg`` —
that is an identifier, not a network fetch.

Report sections:
  1. Header — record count, attributes, date range, uniform seed_hash.
  2. Trend charts — per attribute, accuracy (and bias/recall@k where present)
     over the ordered ledger sequence. Inline SVG line+point charts.
  3. Matrix heatmap — model × prompt(version) grid of a metric (accuracy for
     plr; legacy search records show recall@k), colored by value.
  4. Prompt-change tracking — for consecutive records of the same attribute
     where prompt_hash (or version) changed, the metric Δ alongside the
     hash/version transition. Only shows what the ledger supports.

GPU-free, network-free, stdlib only. Safe to import from lab.py.

Usage:
    python3 report.py --ledger eval/ledger.jsonl --out report.html
    from report import build_report; build_report(ledger_path, out_path)
"""
from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime

# SVG namespace — an identifier, NOT a network fetch. Kept as a named constant
# so the self-containment intent is explicit at the one place it appears.
_SVG_NS = "http://www.w3.org/2000/svg"

# Section marker ids (also used as anchors and asserted by tests).
SECTION_HEADER = "section-header"
SECTION_TREND = "section-trend"


# =====================================================================
# Ledger loading
# =====================================================================


def load_ledger(path: str) -> list[dict]:
    """Read a ledger.jsonl into a list of records (empty list if absent)."""
    if not path or not os.path.exists(path):
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate a corrupt line rather than crash the whole report.
                continue
    return records


def _combo(record: dict) -> tuple[str, str]:
    """Return (model, pipeline) with legacy records grouped as 'unknown'."""
    model = record.get("model") or "unknown"
    pipeline = record.get("pipeline") or "unknown"
    return model, pipeline


def _primary_metric(record: dict) -> float | None:
    """Headline metric: accuracy (plr); recall@k for legacy search records."""
    if record.get("attribute") == "search":
        v = record.get("recall_at_k")
    else:
        v = record.get("accuracy")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bias_rate(record: dict) -> float | None:
    bias = record.get("bias")
    if not isinstance(bias, dict):
        return None
    rate = bias.get("rate")
    try:
        return float(rate) if rate is not None else None
    except (TypeError, ValueError):
        return None


# =====================================================================
# Inline-SVG helpers (pure Python — no JS, no external libs)
# =====================================================================


def _fmt(x: float) -> str:
    """Compact number formatting for SVG coordinates/labels."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _svg_open(width: int, height: int, cls: str = "") -> str:
    cls_attr = f' class="{cls}"' if cls else ""
    return (
        f'<svg xmlns="{_SVG_NS}" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"{cls_attr} '
        f'role="img" preserveAspectRatio="xMidYMid meet">'
    )


def _line_chart(
    title: str,
    series: list[tuple[str, list[tuple[str, float]], str]],
    y_max: float = 1.0,
) -> str:
    """Build one inline-SVG line+point chart.

    series: list of (label, [(x_label, y_value), ...], color). All series share
    the same x index (record order). y in [0, y_max].
    """
    W, H = 520, 240
    pad_l, pad_r, pad_t, pad_b = 46, 12, 26, 46
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    # Longest series drives the x axis count.
    n = max((len(pts) for _, pts, _ in series), default=0)
    parts: list[str] = [_svg_open(W, H, "chart")]
    parts.append(
        f'<text x="{W/2}" y="16" text-anchor="middle" class="chart-title">'
        f"{html.escape(title)}</text>"
    )

    # Axes.
    x0, y0 = pad_l, pad_t + plot_h
    parts.append(
        f'<line x1="{x0}" y1="{pad_t}" x2="{x0}" y2="{y0}" class="axis"/>'
    )
    parts.append(
        f'<line x1="{x0}" y1="{y0}" x2="{x0 + plot_w}" y2="{y0}" class="axis"/>'
    )
    # Y gridlines / labels at 0, 0.5*y_max, y_max.
    for frac in (0.0, 0.5, 1.0):
        yv = frac * y_max
        y = y0 - frac * plot_h
        parts.append(
            f'<line x1="{x0}" y1="{_fmt(y)}" x2="{x0 + plot_w}" y2="{_fmt(y)}" '
            f'class="grid"/>'
        )
        parts.append(
            f'<text x="{x0 - 6}" y="{_fmt(y + 3)}" text-anchor="end" '
            f'class="tick">{_fmt(yv)}</text>'
        )

    def _x(i: int) -> float:
        if n <= 1:
            return x0 + plot_w / 2
        return x0 + (i / (n - 1)) * plot_w

    def _y(v: float) -> float:
        v = max(0.0, min(y_max, v))
        return y0 - (v / y_max if y_max else 0) * plot_h

    # X tick labels (from the longest series).
    x_labels: list[str] = []
    for _, pts, _c in series:
        if len(pts) == n:
            x_labels = [xl for xl, _yv in pts]
            break
    for i, xl in enumerate(x_labels):
        parts.append(
            f'<text x="{_fmt(_x(i))}" y="{y0 + 16}" text-anchor="middle" '
            f'class="xtick">{html.escape(str(xl))}</text>'
        )

    # Series.
    for label, pts, color in series:
        if not pts:
            continue
        coords = [(_x(i), _y(v)) for i, (_xl, v) in enumerate(pts)]
        if len(coords) >= 2:
            d = " ".join(f"{_fmt(cx)},{_fmt(cy)}" for cx, cy in coords)
            parts.append(
                f'<polyline points="{d}" fill="none" stroke="{color}" '
                f'stroke-width="2" class="series-line"/>'
            )
        for (cx, cy), (_xl, v) in zip(coords, pts):
            parts.append(
                f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="3.5" '
                f'fill="{color}" class="series-pt"><title>'
                f"{html.escape(label)}: {_fmt(v)}</title></circle>"
            )

    # Legend.
    lx = x0 + 6
    ly = pad_t + 10
    for label, _pts, color in series:
        parts.append(
            f'<rect x="{lx}" y="{ly - 8}" width="10" height="10" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{lx + 14}" y="{ly + 1}" class="legend">'
            f"{html.escape(label)}</text>"
        )
        lx += 14 + 9 * len(label) + 18

    parts.append("</svg>")
    return "".join(parts)


def _heat_color(frac: float) -> str:
    """Map a fraction in [0,1] to a red→yellow→green background color."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        # red -> yellow
        t = frac / 0.5
        r, g, b = 220, int(60 + t * (200 - 60)), 60
    else:
        # yellow -> green
        t = (frac - 0.5) / 0.5
        r, g, b = int(220 - t * (220 - 60)), 200, int(60 + t * (110 - 60))
    return f"#{r:02x}{g:02x}{b:02x}"


# =====================================================================
# Section builders
# =====================================================================



_CMP_CSS = (
    "<style>table.cmp{border-collapse:collapse;margin:12px 0 24px;font-size:13px}"
    "table.cmp th,table.cmp td{border:1px solid #ccc;padding:4px 10px;text-align:center}"
    "table.cmp th{background:#f2f4f8}</style>"
)


def _latest_per_combo(records: list[dict]) -> list[dict]:
    """Most recent record per (attribute, model, version, dataset)."""
    latest: dict[tuple, dict] = {}
    for r in records:
        key = (r.get("attribute"), r.get("model"), r.get("version"), r.get("dataset"))
        latest[key] = r  # ledger is append-only chronological -> last wins
    return list(latest.values())


def _build_summary_table(records: list[dict], title: str = "전체 실험 비교") -> str:
    """Overall comparison table — the default cross-experiment view: one row
    per latest (attribute, model, version, dataset) combo with the headline
    metrics side by side."""
    rows = _latest_per_combo(records)
    if not rows:
        return ""
    out = [_CMP_CSS, f"<h2>{html.escape(title)}</h2>",
           '<table class="cmp"><tr><th>attribute</th><th>version</th><th>model</th>'
           '<th>pipeline</th><th>dataset</th><th>n</th><th>accuracy</th><th>macro F1</th>'
           '<th>bias</th><th>pred unknown</th><th>prompt</th><th>date</th></tr>']

    def _m(r, k):
        v = r.get(k)
        return _fmt(v) if isinstance(v, (int, float)) else "&mdash;"

    for r in sorted(rows, key=lambda x: (str(x.get("attribute")), str(x.get("version")))):
        bias = r.get("bias") or {}
        pu = r.get("pred_unknown") or {}
        ds = str(r.get("dataset") or "")
        ds = ds.rsplit("/", 1)[-1] if "/" in ds else ds
        out.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('attribute') or ''))}</td>"
            f"<td>{html.escape(str(r.get('version') or ''))}</td>"
            f"<td>{html.escape(str(r.get('model') or ''))}</td>"
            f"<td>{html.escape(str(r.get('pipeline') or ''))}</td>"
            f"<td>{html.escape(ds)}</td>"
            f"<td>{r.get('n') if r.get('n') is not None else r.get('n_queries', '')}</td>"
            f"<td>{_m(r, 'accuracy')}</td>"
            f"<td>{_m(r, 'macro_f1')}</td>"
            f"<td>{_fmt(bias['rate']) if isinstance(bias.get('rate'), (int, float)) else '&mdash;'}</td>"
            f"<td>{_fmt(pu['rate']) if isinstance(pu.get('rate'), (int, float)) else '&mdash;'}</td>"
            f"<td>{html.escape(str(r.get('prompt_hash') or '')[:8]) or '&mdash;'}</td>"
            f"<td>{html.escape(str(r.get('date') or '')[:10]) or '&mdash;'}</td>"
            "</tr>")
    out.append("</table>")
    return "\n".join(out)


def _build_confusions(records: list[dict]) -> str:
    """Confusion matrix of the LATEST record per attribute (rows=true,
    cols=pred), with per-class recall/precision/F1 alongside."""
    latest: dict[str, dict] = {}
    for r in records:
        if r.get("confusion"):
            latest[str(r.get("attribute"))] = r
    if not latest:
        return ""
    out = ["<h2>Confusion (attribute별 최신 레코드)</h2>"]
    for attr, r in sorted(latest.items()):
        conf = r["confusion"]
        classes = sorted({*conf.keys(), *(c for row in conf.values() for c in row)})
        recall = r.get("recall") or {}
        precision = r.get("precision") or {}
        f1 = r.get("f1") or {}
        out.append(f"<h3>{html.escape(attr)} &middot; {html.escape(str(r.get('version') or ''))}</h3>")
        out.append('<table class="cmp"><tr><th>true &#92; pred</th>'
                   + "".join(f"<th>{html.escape(c)}</th>" for c in classes)
                   + "<th>recall</th><th>precision</th><th>F1</th></tr>")
        for t in classes:
            row = conf.get(t, {})
            total = sum(row.values()) or 1
            cells = []
            for c in classes:
                v = row.get(c, 0)
                shade = f' style="background:{_heat_color(v / total)}"' if v else ""
                cells.append(f"<td{shade}>{v or ''}</td>")

            def _p(d, key=t):
                return _fmt(d[key]) if isinstance(d.get(key), (int, float)) else "&mdash;"

            out.append(f"<tr><th>{html.escape(t)}</th>" + "".join(cells)
                       + f"<td>{_p(recall)}</td><td>{_p(precision)}</td><td>{_p(f1)}</td></tr>")
        out.append("</table>")
    return "\n".join(out)


def _build_header(records: list[dict]) -> str:
    attrs = sorted({r.get("attribute", "unknown") for r in records})
    dates = sorted(str(r.get("date", "")) for r in records if r.get("date"))
    date_range = f"{dates[0]} → {dates[-1]}" if dates else "n/a"
    seeds = {r.get("seed_hash", "") for r in records if r.get("seed_hash")}
    seed_line = (
        next(iter(seeds)) if len(seeds) == 1 else f"{len(seeds)} distinct"
    )
    generated = datetime.now().isoformat(timespec="seconds")

    rows = [
        ("Records", str(len(records))),
        ("Attributes", ", ".join(attrs) if attrs else "none"),
        ("Date range", date_range),
        ("Seed hash", seed_line if seeds else "n/a"),
        ("Generated", generated),
    ]
    body = "".join(
        f'<div class="meta-row"><span class="meta-k">{html.escape(k)}</span>'
        f'<span class="meta-v">{html.escape(v)}</span></div>'
        for k, v in rows
    )
    return (
        f'<section id="{SECTION_HEADER}">'
        f"<h1>PLR Prompt Lab — Eval Report</h1>"
        f'<div class="meta">{body}</div>'
        f"</section>"
    )


def _build_trends(records: list[dict]) -> str:
    by_attr: dict[str, list[dict]] = {}
    for r in records:
        by_attr.setdefault(r.get("attribute", "unknown"), []).append(r)

    charts: list[str] = []
    for attr in sorted(by_attr):
        recs = by_attr[attr]
        # Order by (date, version) — date first, stable within the ledger.
        recs = sorted(recs, key=lambda r: (str(r.get("date", "")), str(r.get("version", ""))))

        def _xlabel(r: dict) -> str:
            v = str(r.get("version", ""))
            d = str(r.get("date", ""))[:10]
            return v or d or "?"

        primary_pts = [
            (_xlabel(r), m)
            for r in recs
            if (m := _primary_metric(r)) is not None
        ]
        if not primary_pts:
            continue

        is_search = attr == "search"
        primary_name = "recall@k" if is_search else "accuracy"
        series = [(primary_name, primary_pts, "#2b6cb0")]

        # Add bias trend for gender (or any attr carrying a bias rate).
        bias_pts = [
            (_xlabel(r), b) for r in recs if (b := _bias_rate(r)) is not None
        ]
        if bias_pts:
            series.append(("bias", bias_pts, "#c53030"))

        charts.append(
            f'<div class="chart-card">'
            f'<h3>{html.escape(attr)}</h3>'
            f"{_line_chart(attr, series)}"
            f"</div>"
        )

    inner = (
        "".join(charts)
        if charts
        else '<p class="empty">No trend data available.</p>'
    )
    return (
        f'<section id="{SECTION_TREND}">'
        f"<h2>Performance Trends</h2>"
        f'<div class="chart-grid">{inner}</div>'
        f"</section>"
    )


# =====================================================================
# CSS (inlined — no external stylesheet)
# =====================================================================

_CSS = """
:root{color-scheme:light dark;}
*{box-sizing:border-box;}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  margin:0;padding:24px;line-height:1.45;color:#1a202c;background:#f7fafc;}
h1{font-size:22px;margin:0 0 12px;}
h2{font-size:18px;margin:28px 0 10px;border-bottom:2px solid #cbd5e0;padding-bottom:4px;}
h3{font-size:14px;margin:0 0 4px;}
section{background:#fff;border:1px solid #e2e8f0;border-radius:8px;
  padding:16px 20px;margin:0 0 20px;max-width:1100px;}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px 24px;}
.meta-row{display:flex;justify-content:space-between;border-bottom:1px dotted #e2e8f0;padding:2px 0;}
.meta-k{color:#4a5568;font-weight:600;}
.meta-v{font-family:ui-monospace,Menlo,monospace;font-size:13px;}
.chart-grid{display:flex;flex-wrap:wrap;gap:16px;}
.chart-card{border:1px solid #edf2f7;border-radius:6px;padding:8px;background:#fff;}
svg.chart{display:block;}
.chart-title{font-size:12px;fill:#2d3748;font-weight:600;}
.axis{stroke:#a0aec0;stroke-width:1;}
.grid{stroke:#edf2f7;stroke-width:1;}
.tick{font-size:10px;fill:#718096;}
.xtick{font-size:9px;fill:#718096;}
.legend{font-size:10px;fill:#2d3748;}
table{border-collapse:collapse;margin-top:8px;font-size:13px;}
.matrix th,.matrix td{border:1px solid #cbd5e0;padding:6px 10px;text-align:center;}
.matrix th{background:#edf2f7;}
.cell{font-family:ui-monospace,Menlo,monospace;color:#1a202c;}
.cell.blank{background:#f7fafc;}
.heat-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:#4a5568;margin:6px 0;}
.swatch{width:22px;height:12px;display:inline-block;border:1px solid #cbd5e0;}
.prompt-change th,.prompt-change td{border:1px solid #cbd5e0;padding:6px 10px;text-align:left;}
.prompt-change th{background:#edf2f7;}
.delta.up{color:#276749;font-weight:600;}
.delta.down{color:#c53030;font-weight:600;}
.delta.flat{color:#718096;}
.empty{color:#718096;font-style:italic;}
""".strip()


# =====================================================================
# Top-level assembly
# =====================================================================


def render_html(records: list[dict], compare_ledger: str | None = None) -> str:
    """Render the full self-contained HTML string from ledger records."""
    if not records:
        body = (
            f'<section id="{SECTION_HEADER}"><h1>PLR Prompt Lab — Eval Report</h1>'
            f'<p class="empty" id="no-records">No records yet — the ledger is '
            f"empty. Run an eval to populate it.</p></section>"
            f'<section id="{SECTION_TREND}"><h2>Performance Trends</h2>'
            f'<p class="empty">No trend data available.</p></section>'
        )
    else:
        body = (
            _build_header(records)
            + _build_summary_table(records)
            + (_build_summary_table(load_ledger(compare_ledger),
                                    title=f"비교 대상 실험군 — {compare_ledger}")
               if compare_ledger else "")
            + _build_trends(records)
            + _build_confusions(records)
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        "<title>PLR Prompt Lab — Eval Report</title>\n"
        f"<style>\n{_CSS}\n</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def build_report(ledger_path: str, out_path: str, compare_ledger: str | None = None) -> str:
    """Load the ledger, render, and write the self-contained HTML file.

    Returns the output path. Empty/missing ledger produces a valid "no records"
    report rather than crashing.
    """
    records = load_ledger(ledger_path)
    html_str = render_html(records, compare_ledger=compare_ledger)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_str)
    print(
        f"[report] wrote {out_path} ({len(records)} record(s), "
        f"{os.path.getsize(out_path)} bytes)"
    )
    return out_path


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # lab root
    ap = argparse.ArgumentParser(
        description="Generate a self-contained HTML report from the eval ledger."
    )
    ap.add_argument(
        "--ledger",
        default=os.path.join(here, "eval", "ledger.jsonl"),
        help="ledger.jsonl path (default: eval/ledger.jsonl)",
    )
    ap.add_argument(
        "--out",
        default=os.path.join(here, "report.html"),
        help="output HTML path (default: report.html)",
    )
    args = ap.parse_args()
    build_report(args.ledger, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
