"""gallery — self-contained HTML of crops vs labels (visual eval evidence).

Renders every crop in a dataset as a card: thumbnail (base64-inlined JPEG),
per-attribute prediction vs human label tags, and margin / quality scores.
Wrong predictions sort FIRST (most-wrong, lowest margin first) so the file
opens on the failure cases — the primary evidence source for the
improve-prompt skill and for humans reviewing an experiment.

다속성 데이터셋은 기본으로 **모든 라벨된 속성**을 카드마다 태그로 그리고,
필터바에서 속성 체크박스 + AND/OR 토글로 "선택 속성들이 (모두/하나라도)
오답인 카드"를 골라볼 수 있다. `--attribute a[,b]`로 부분 집합만 볼 수 있다.

GPU-free, network-free, no external assets: the HTML works from a file://
open or any static server.
"""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

_THUMB_MAX = 168  # px, longest side


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _thumb_b64(crop_path: Path) -> str:
    img = Image.open(crop_path).convert("RGB")
    img.thumbnail((_THUMB_MAX, _THUMB_MAX))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _fmt(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"


_CSS = """
body{margin:0;padding:24px;background:#0b1120;color:#dbe4f5;
     font-family:'Pretendard','Noto Sans KR','Malgun Gothic','Segoe UI',sans-serif}
h1{font-size:20px;margin:0 0 4px} .sub{color:#8fa1c0;font-size:13px;margin-bottom:18px}
.filters{margin:0 0 8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.filters button,.filters label{background:#16213b;border:1px solid #263452;color:#b8c6e4;
                border-radius:8px;padding:5px 12px;font-size:12.5px;cursor:pointer}
.filters button.on{border-color:#38bdf8;color:#e8f2ff}
.filters label{display:inline-flex;align-items:center;gap:5px}
.filters .sep{color:#3b4a6b;margin:0 2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.card{background:#111a2e;border:1px solid #263452;border-radius:12px;padding:10px;text-align:center}
.card.wrong{border-color:rgba(248,113,113,.65)}
.card img{max-width:100%;border-radius:8px;background:#000}
.oid{font-family:ui-monospace,monospace;font-size:11px;color:#8fa1c0;margin-top:6px}
.pl{font-size:12.5px;margin-top:3px}
.pl b{color:#e8f2ff}
.badge{display:inline-block;font-size:10.5px;font-weight:700;border-radius:5px;
       padding:1px 8px;margin-top:5px}
.badge.ok{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.4)}
.badge.no{background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.45)}
.badge.unl{background:rgba(148,163,184,.15);color:#94a3b8;border:1px solid rgba(148,163,184,.4)}
.sc{font-size:11px;color:#8fa1c0;margin-top:3px}
.tags{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.tag{font-size:11px;border-radius:5px;padding:2px 7px;text-align:left;
     font-family:ui-monospace,monospace}
.tag b{font-weight:700}
.tag.ok{background:rgba(52,211,153,.10);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.tag.no{background:rgba(248,113,113,.10);color:#f87171;border:1px solid rgba(248,113,113,.35)}
.tag.unl{background:rgba(148,163,184,.10);color:#94a3b8;border:1px solid rgba(148,163,184,.3)}
"""

_JS_SINGLE = """
// 축별(status/label/pred) 선택 집합. 축 내 다중선택은 OR, 세 축은 AND 로 결합하고
// 어느 축도 안 고르면 전체를 표시한다(기존 동작 보존). 축을 분리해 label 값과
// pred 값이 같아도(male vs male) 서로 다른 data-* 에만 대조 → 교차 오염이 없다.
const sel = {status:new Set(), label:new Set(), pred:new Set()};
function flt(axis, val, btn){
  if(axis==='status' && val==='all'){
    sel.status.clear();               // '전체' = 상태 축 초기화
  } else if(sel[axis].has(val)){
    sel[axis].delete(val);            // 재클릭 = 토글 해제
  } else {
    sel[axis].add(val);
  }
  render();
  syncButtons();
}
function render(){
  document.querySelectorAll('.card').forEach(c=>{
    const okStatus = sel.status.size===0 || sel.status.has(c.dataset.k);
    const okLabel  = sel.label.size===0  || sel.label.has(c.dataset.label);
    const okPred   = sel.pred.size===0   || sel.pred.has(c.dataset.pred);
    c.style.display = (okStatus && okLabel && okPred) ? '' : 'none';
  });
}
// 버튼 on 표시는 항상 sel 에서 파생 → 상태 드리프트 없음.
// '전체' 버튼은 상태 축이 비었을 때만 켠다.
function syncButtons(){
  document.querySelectorAll('.filters button').forEach(b=>{
    const ax = b.dataset.axis, v = b.dataset.val;
    const on = (ax==='status' && v==='all')
      ? sel.status.size===0
      : (sel[ax] && sel[ax].has(v));
    b.classList.toggle('on', !!on);
  });
}
"""

# 다속성 필터: 상태(전체/오답만/정답만) × 속성 체크박스 × AND/OR.
#   오답만 + OR  → 선택 속성 중 "하나라도" 틀린 카드
#   오답만 + AND → 선택 속성 "모두" 틀린 카드
_JS_MULTI = """
const state = {status:'all', mode:'or'};
function _sel(){return [...document.querySelectorAll('.aflt:checked')].map(c=>c.value);}
function upd(){
  const sel = _sel();
  document.querySelectorAll('.card').forEach(c=>{
    const wrong   = (c.dataset.wrong  ||'').split(' ').filter(Boolean);
    const correct = (c.dataset.correct||'').split(' ').filter(Boolean);
    let show = true;
    if(state.status==='wrong'){
      show = sel.length>0 && (state.mode==='and'
             ? sel.every(a=>wrong.includes(a))
             : sel.some(a=>wrong.includes(a)));
    } else if(state.status==='correct'){
      show = sel.length>0 && (state.mode==='and'
             ? sel.every(a=>correct.includes(a))
             : sel.some(a=>correct.includes(a)));
    }
    c.style.display = show ? '' : 'none';
  });
}
function setStatus(s,btn){state.status=s;
  document.querySelectorAll('.stbtn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');upd();}
function setMode(m,btn){state.mode=m;
  document.querySelectorAll('.mdbtn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');upd();}
"""


def _extracted_preds(ds: Path, attribute: str,
                     base_preds: dict[str, dict]) -> dict[str, dict]:
    """attributes.jsonl(plr_json 전체 캐시)에서 한 속성의 pred/margin을 재추출.
    None → "unknown" 강제는 run_eval 채점 규칙과 동일 — 배지와 지표가 항상
    같은 예측을 본다. quality는 크롭 속성이라 기존 예측 행에서 재사용."""
    from evalkit.dataset import attribute_spec, resolve_json_path

    spec = attribute_spec(ds, attribute)
    out: dict[str, dict] = {}
    if not spec.get("pred_path"):
        return out
    for r in _jsonl(ds / "attributes.jsonl"):
        pj = r.get("plr_json") or {}
        pred = resolve_json_path(pj, spec["pred_path"])
        row: dict = {"obj_id": r["obj_id"],
                     "pred": "unknown" if pred in (None, "") else pred}
        if spec.get("margin_path"):
            m = resolve_json_path(pj, spec["margin_path"])
            if isinstance(m, (int, float)):
                row["margin"] = float(m)
        q = (base_preds.get(r["obj_id"]) or {}).get("quality")
        if q is not None:
            row["quality"] = q
        out[r["obj_id"]] = row
    return out


def _preds_for(ds: Path, attribute: str | None,
               base_preds: dict[str, dict]) -> dict[str, dict]:
    """predictions.jsonl이 요청 속성의 추출물이면 그대로, 아니면 attributes.jsonl
    에서 재추출. predictions.jsonl이 아예 없어도(서버 pull 등) attributes.jsonl만
    있으면 추출한다."""
    stamped = {r.get("attribute") for r in base_preds.values() if r.get("attribute")}
    mismatch = bool(stamped) and attribute not in stamped
    absent = not base_preds and (ds / "attributes.jsonl").exists()
    if attribute and (mismatch or absent):
        return _extracted_preds(ds, attribute, base_preds)
    return base_preds


def _doc(ds: Path, sub: str, filters_html: str, cards: list[tuple[tuple, str]],
         js: str) -> str:
    cards.sort(key=lambda t: t[0])  # wrong first, low margin first
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        f"<title>gallery — {html.escape(ds.name)}</title><style>{_CSS}</style></head><body>"
        f"<h1>크롭 시각화 — {html.escape(str(ds))}</h1>"
        f'<div class="sub">{sub}</div>'
        f"{filters_html}"
        f'<div class="grid">{"".join(c for _r, c in cards)}</div>'
        f"<script>{js}</script></body></html>"
    )


def build_gallery(
    dataset_dir: str | Path,
    out_path: str | Path | None = None,
    attribute: str | None = None,
) -> str:
    """Build <dataset>/gallery.html (or out_path). Returns the output path.

    Needs labels.jsonl + crops/; predictions.jsonl / attributes.jsonl enrich
    each card with per-attribute pred / correct-wrong tags + margin / quality.

    attribute: 그릴 속성의 쉼표 목록. 미지정이면 라벨에 등장하는 속성 전부
    (eval -A all과 같은 기준). 속성이 2개 이상이면 카드마다 속성별 태그가
    붙고 필터바에 속성 체크박스 + AND/OR 토글이 생긴다.
    """
    ds = Path(dataset_dir)
    out = Path(out_path) if out_path else ds / "gallery.html"

    from evalkit.dataset import declared_attributes, labeled_attributes, load_labels

    if attribute:
        attrs = [a.strip() for a in str(attribute).split(",") if a.strip()]
    else:
        declared = declared_attributes(ds)
        labeled = labeled_attributes(ds)
        if labeled:
            attrs = [a for a in declared if a in labeled] or labeled
        else:
            attrs = declared[:1]  # legacy 단일 label 데이터셋 (익명 라벨)

    base_preds = {r["obj_id"]: r for r in _jsonl(ds / "predictions.jsonl")}

    if len(attrs) >= 2:
        return _build_multi(ds, out, attrs, base_preds)
    return _build_single(ds, out, attrs[0] if attrs else None, base_preds)


# ---------------------------------------------------------------------
# 단일 속성 (기존 모습 유지 — 단순 배지 + label 값 필터)
# ---------------------------------------------------------------------

def _build_single(ds: Path, out: Path, attribute: str | None,
                  base_preds: dict[str, dict]) -> str:
    from evalkit.dataset import load_labels

    labels: dict[str, str | None] = dict(load_labels(ds, attribute))
    preds = _preds_for(ds, attribute, base_preds)
    obj_ids = sorted(set(labels) | set(preds))
    if not obj_ids:
        raise FileNotFoundError(
            f"No labels.jsonl / predictions.jsonl content in {ds} — nothing to render."
        )

    cards: list[tuple[tuple, str]] = []
    n_ok = n_wrong = n_nolabel = 0
    classes: set[str] = set()        # label 축 버튼 재료 (label 값만)
    pred_classes: set[str] = set()   # pred 축 버튼 재료 (정규화된 pred 값)
    for oid in obj_ids:
        crop = ds / "crops" / f"{oid}.jpg"
        if not crop.exists():
            continue
        label = labels.get(oid)
        p = preds.get(oid) or {}
        pred = p.get("pred")
        # SRV-018: pred=None/빈값을 카드 생성 단일 지점에서 'unknown' 으로 정규화해
        #   data-pred 와 pred 버튼 버킷 양쪽에 동일 적용 → 소스(재추출 vs 직접
        #   predictions.jsonl)와 무관하게 같은 버킷으로 모인다.
        pred_norm = str(pred) if pred not in (None, "") else "unknown"
        # kind/badge 분기 밖에서 수집 → unlabeled·오답 카드의 pred 도 버킷에 포함(SRV-016).
        pred_classes.add(pred_norm)
        margin, quality = p.get("margin"), p.get("quality")
        if label is None or pred is None or label == "unknown":
            kind, badge, cls = "unlabeled", '<span class="badge unl">UNSCORED</span>', ""
            n_nolabel += 1
            rank = (2, 0.0)
        elif pred == label:
            kind, badge, cls = "correct", '<span class="badge ok">CORRECT</span>', ""
            n_ok += 1
            rank = (1, margin if isinstance(margin, (int, float)) else 1.0)
        else:
            kind, badge, cls = "wrong", '<span class="badge no">WRONG</span>', " wrong"
            n_wrong += 1
            rank = (0, margin if isinstance(margin, (int, float)) else 0.0)
        if label:
            classes.add(str(label))
        cards.append((rank, (
            f'<div class="card{cls}" data-k="{kind}" '
            f'data-label="{html.escape(str(label or ""))}" data-pred="{html.escape(pred_norm)}">'
            f'<img src="data:image/jpeg;base64,{_thumb_b64(crop)}" alt="{html.escape(oid)}">'
            f'<div class="oid">{html.escape(oid)}</div>'
            f'<div class="pl">pred <b>{html.escape(pred_norm)}</b> · '
            f'label <b>{html.escape(str(label or "—"))}</b></div>'
            f"{badge}"
            f'<div class="sc">margin {_fmt(margin)} · quality {_fmt(quality)}</div>'
            f"</div>"
        )))

    n = n_ok + n_wrong
    acc = f"{n_ok / n:.3f}" if n else "—"
    # 각 버튼에 축 식별자(data-axis)·값(data-val)을 실어 flt() 가 축을 구분하고
    # syncButtons() 가 on 표시를 sel 에서 파생하게 한다(축 교차 오염 원천 제거).
    filters = ['<button class="on" data-axis="status" data-val="all" onclick="flt(\'status\',\'all\',this)">전체</button>',
               '<button data-axis="status" data-val="wrong" onclick="flt(\'status\',\'wrong\',this)">오답만</button>',
               '<button data-axis="status" data-val="correct" onclick="flt(\'status\',\'correct\',this)">정답만</button>']
    for c in sorted(classes):
        filters.append(
            f'<button data-axis="label" data-val="{html.escape(c)}" '
            f'onclick="flt(\'label\',\'{html.escape(c)}\',this)">label={html.escape(c)}</button>')
    # SRV-019: 다중-클래스 도메인에서는 pred 클래스가 많아 pred 버튼이 필터바를 비대하게
    #   만들 수 있다. 지금은 가시성 우선으로 값별 1버튼을 유지하고, 상위 K개+'기타' 묶음/
    #   접이(details) 등 밀도 개선은 후속 run 으로 미룬다(non_goals).
    for c in sorted(pred_classes):
        filters.append(
            f'<button data-axis="pred" data-val="{html.escape(c)}" '
            f'onclick="flt(\'pred\',\'{html.escape(c)}\',this)">pred={html.escape(c)}</button>')
    sub = (f"scored {n} (correct {n_ok} · wrong {n_wrong} · unscored {n_nolabel})"
           f" · accuracy {acc} · 오답이 먼저, 저-margin 순")
    out.write_text(
        _doc(ds, sub, f'<div class="filters">{"".join(filters)}</div>', cards, _JS_SINGLE),
        encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------------
# 다속성 (속성별 태그 + 체크박스 × AND/OR 필터)
# ---------------------------------------------------------------------

def _build_multi(ds: Path, out: Path, attrs: list[str],
                 base_preds: dict[str, dict]) -> str:
    from evalkit.dataset import load_labels

    labels_by = {a: load_labels(ds, a) for a in attrs}
    preds_by = {a: _preds_for(ds, a, base_preds) for a in attrs}
    obj_ids = sorted({oid for m in labels_by.values() for oid in m}
                     | {oid for m in preds_by.values() for oid in m})
    if not obj_ids:
        raise FileNotFoundError(
            f"No labels.jsonl / predictions.jsonl content in {ds} — nothing to render."
        )

    cards: list[tuple[tuple, str]] = []
    stat = {a: {"ok": 0, "wrong": 0} for a in attrs}
    for oid in obj_ids:
        crop = ds / "crops" / f"{oid}.jpg"
        if not crop.exists():
            continue
        tags: list[str] = []
        wrong_attrs: list[str] = []
        correct_attrs: list[str] = []
        min_margin = 1.0
        quality = None
        for a in attrs:
            label = labels_by[a].get(oid)
            p = preds_by[a].get(oid) or {}
            pred, margin = p.get("pred"), p.get("margin")
            if quality is None:
                quality = p.get("quality")
            if label is None or pred is None or label == "unknown":
                tags.append(f'<div class="tag unl">{html.escape(a)}: '
                            f'<b>{html.escape(str(pred or "—"))}</b> · unscored</div>')
                continue
            if pred == label:
                correct_attrs.append(a)
                stat[a]["ok"] += 1
                tags.append(f'<div class="tag ok">{html.escape(a)}: '
                            f'<b>{html.escape(str(pred))}</b> ✓</div>')
            else:
                wrong_attrs.append(a)
                stat[a]["wrong"] += 1
                if isinstance(margin, (int, float)):
                    min_margin = min(min_margin, margin)
                tags.append(f'<div class="tag no">{html.escape(a)}: '
                            f'<b>{html.escape(str(pred))}</b> ✗ (label {html.escape(str(label))})</div>')
        # 정렬: 오답 있는 카드 먼저 → 오답 많은 순 → 저-margin 순
        rank = (0 if wrong_attrs else (1 if correct_attrs else 2),
                -len(wrong_attrs), min_margin)
        cls = " wrong" if wrong_attrs else ""
        cards.append((rank, (
            f'<div class="card{cls}" '
            f'data-wrong="{html.escape(" ".join(wrong_attrs))}" '
            f'data-correct="{html.escape(" ".join(correct_attrs))}">'
            f'<img src="data:image/jpeg;base64,{_thumb_b64(crop)}" alt="{html.escape(oid)}">'
            f'<div class="oid">{html.escape(oid)}</div>'
            f'<div class="tags">{"".join(tags)}</div>'
            f'<div class="sc">quality {_fmt(quality)}</div>'
            f"</div>"
        )))

    per_attr = " · ".join(
        f"{a} {s['ok']}/{s['ok'] + s['wrong']}" if (s["ok"] + s["wrong"]) else f"{a} —"
        for a, s in stat.items())
    sub = f"속성별 정답률: {per_attr} · 오답 많은 카드 먼저, 저-margin 순"

    checkboxes = "".join(
        f'<label><input type="checkbox" class="aflt" value="{html.escape(a)}" '
        f'checked onchange="upd()">{html.escape(a)}</label>'
        for a in attrs)
    filters_html = (
        '<div class="filters">'
        '<button class="stbtn on" onclick="setStatus(\'all\',this)">전체</button>'
        '<button class="stbtn" onclick="setStatus(\'wrong\',this)">오답만</button>'
        '<button class="stbtn" onclick="setStatus(\'correct\',this)">정답만</button>'
        '<span class="sep">|</span>' + checkboxes +
        '<span class="sep">|</span>'
        '<button class="mdbtn on" onclick="setMode(\'or\',this)">OR (하나라도)</button>'
        '<button class="mdbtn" onclick="setMode(\'and\',this)">AND (모두)</button>'
        "</div>")
    out.write_text(_doc(ds, sub, filters_html, cards, _JS_MULTI), encoding="utf-8")
    return str(out)
