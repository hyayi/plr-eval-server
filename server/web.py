"""web — Jinja2 서버 렌더 UI (외부 CDN 0, lab 다크 팔레트).

페이지: / 목록 · /d/{ds} 리더보드(클릭 정렬, 최신/전체 토글, ⚠배지)
       /r/{id} run 상세(지표·confusion·표면 파일 열람) · /diff 비교 · /upload 폼
파일이 진실: aggregate 등 구조 지표는 metrics.json에서 읽는다.
업로드된 surface 파일은 텍스트로만 서빙(이스케이프) — 실행 절대 없음.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fromjson"] = json.loads


def _state():
    from server.app import STATE
    return STATE


def _run_rows(dataset: str, all_history: bool) -> list[dict]:
    from server.app import list_runs
    from server.storage import read_json

    rows = list_runs(dataset=dataset, all_history=all_history)["runs"]
    root = _state()["root"]
    for r in rows:
        mj = read_json(root / "runs" / r["run_id"] / "metrics.json") or {}
        r["aggregate"] = mj.get("aggregate", {})
    return rows


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = _state()["conn"]
    datasets = [dict(x) | {"attributes": json.loads(x["attrs_json"])}
                for x in conn.execute(
                    "SELECT * FROM datasets ORDER BY name").fetchall()]
    counts = {r["dataset"]: r["c"] for r in conn.execute(
        "SELECT dataset, COUNT(*) c FROM runs GROUP BY dataset")}
    return templates.TemplateResponse(request, "index.html",
                                      {"datasets": datasets, "counts": counts})


@router.get("/d/{dataset}", response_class=HTMLResponse)
def leaderboard(request: Request, dataset: str, all: int = 0):
    rows = _run_rows(dataset, all_history=bool(all))
    audit = [dict(x) for x in _state()["conn"].execute(
        "SELECT * FROM label_audit WHERE dataset=? ORDER BY id DESC LIMIT 5",
        (dataset,))]
    return templates.TemplateResponse(request, "leaderboard.html", {
        "dataset": dataset, "rows": rows,
        "all_history": bool(all), "audit": audit,
    })


def _with_per_class_acc(metrics: dict[str, dict]) -> dict[str, dict]:
    """attribute별 metrics(m)에 class별 accuracy를 얹는다 — confusion(행=정답,열=예측)
    에서 one-vs-rest 정확도 (TP+TN)/N 로 유도. recall(행 정규화)과 구별되는 값이며,
    새 채점 로직이 아니라 render 계층에서만 기존 confusion 을 재사용해 파생한다."""
    for m in metrics.values():
        confusion = m.get("confusion") or {}
        n_total = sum(sum(row.values()) for row in confusion.values())
        per_class_acc: dict[str, float | None] = {}
        for c in m.get("classes", []):
            if not n_total:
                per_class_acc[c] = None
                continue
            tp = (confusion.get(c) or {}).get(c, 0)
            row_total = sum((confusion.get(c) or {}).values())            # true==c
            col_total = sum((confusion.get(t) or {}).get(c, 0) for t in confusion)  # pred==c
            fn = row_total - tp
            fp = col_total - tp
            tn = n_total - tp - fp - fn
            per_class_acc[c] = round((tp + tn) / n_total, 4)
        m["per_class_acc"] = per_class_acc
    return metrics


@router.get("/r/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    from server.app import run_detail
    d = run_detail(run_id)
    metrics = _with_per_class_acc(d["metrics"].get("attributes", {}))
    return templates.TemplateResponse(request, "run.html", {
        "run_id": run_id, "meta": d["meta"],
        "metrics": metrics,
        "aggregate": d["metrics"].get("aggregate", {}),
        "skipped": d["metrics"].get("skipped", []),
        "surface_files": d["surface_files"], "prov": d.get("provenance"),
    })


@router.get("/r/{run_id}/surface/{path:path}", response_class=PlainTextResponse)
def surface_file(run_id: str, path: str):
    root = _state()["root"]
    base = (root / "runs" / run_id / "surface").resolve()
    target = (base / path).resolve()
    # is_relative_to: startswith 문자열 비교는 sibling-prefix(…/surface_evil)를
    # 통과시키는 고전적 약점이 있다 — 경로 경계로 검사.
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "no such surface file")
    return target.read_text(encoding="utf-8", errors="replace")


def _diff_norm_key(rel: str) -> str:
    """prompts/<version>/foo/bar.yaml 의 버전 세그먼트를 정규화 키에서 제거해
    다른 버전 dir에 있는 동명 프롬프트가 하나의 diff 항목으로 정렬되게 한다.
    non-prompt 파일(py/vocab/configs)은 상대경로 그대로 키가 된다."""
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "prompts":
        return "/".join([parts[0]] + parts[2:])
    return rel


@router.get("/diff", response_class=HTMLResponse)
def diff_page(request: Request, a: str, b: str):
    root = _state()["root"]
    from server.storage import read_json

    def _files(run_id: str) -> dict[str, tuple[str, str]]:
        """{norm_key: (actual_relpath, content)} — 실제 경로는 diff 헤더 표시용."""
        base = root / "runs" / run_id / "surface"
        if not base.is_dir():
            raise HTTPException(404, f"run {run_id!r} has no surface")
        out: dict[str, tuple[str, str]] = {}
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(base))
            # last-write-wins: 한 run이 같은 이름 프롬프트를 여러 버전 담은 경우
            # (S4 이전 전-버전 번들) 정규화 키가 충돌해 마지막 것만 남는다 — S4 이후
            # run은 단일 버전이라 무해. 레거시 번들에서만 발생.
            out[_diff_norm_key(rel)] = (rel, p.read_text(encoding="utf-8", errors="replace"))
        return out

    fa, fb = _files(a), _files(b)
    diffs = []
    for key in sorted(set(fa) | set(fb)):
        rel_a, content_a = fa.get(key, (None, ""))
        rel_b, content_b = fb.get(key, (None, ""))
        la = content_a.splitlines(keepends=True)
        lb = content_b.splitlines(keepends=True)
        d = list(difflib.unified_diff(la, lb, fromfile=f"{a}/{rel_a or key}",
                                      tofile=f"{b}/{rel_b or key}"))
        if d:
            diffs.append({"path": key, "diff": "".join(d)})
    prov_a = read_json(root / "runs" / a / "run_provenance.json") or {}
    prov_b = read_json(root / "runs" / b / "run_provenance.json") or {}
    param_keys = sorted(set(prov_a) | set(prov_b))
    return templates.TemplateResponse(request, "diff.html", {
        "a": a, "b": b, "diffs": diffs,
        "params": [(k, prov_a.get(k), prov_b.get(k)) for k in param_keys],
    })


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


# =====================================================================
# 다운로드 API — 서버가 report/gallery를 렌더해 돌려준다 (lab submit --pull 소비)
# report.py/gallery.py는 server/render.py 어댑터로 적응(재사용 아님).
# =====================================================================

@router.get("/api/runs/{run_id}/report.html", response_class=HTMLResponse)
def run_report(run_id: str):
    from server.render import render_run_report

    root = _state()["root"]
    if not (root / "runs" / run_id / "meta.json").exists():
        raise HTTPException(404, f"run {run_id!r} not found")
    return render_run_report(root, run_id)


@router.get("/api/datasets/{dataset}/report.html", response_class=HTMLResponse)
def dataset_report(dataset: str):
    from server.render import render_dataset_report

    root = _state()["root"]
    if not (root / "datasets" / dataset).is_dir():
        raise HTTPException(404, f"dataset {dataset!r} not found")
    return render_dataset_report(root, dataset)


@router.get("/api/runs/{run_id}/gallery.html", response_class=HTMLResponse)
def run_gallery(run_id: str):
    from server.render import render_run_gallery

    root = _state()["root"]
    try:
        return render_run_gallery(root, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
