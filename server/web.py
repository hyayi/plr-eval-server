"""web — Jinja2 서버 렌더 UI (외부 CDN 0, lab 다크 팔레트).

페이지: / 목록 · /d/{ds} 리더보드(클릭 정렬, 최신/전체 토글, ⚠배지)
       /r/{id} run 상세(지표·confusion·표면 파일 열람) · /diff 비교 · /upload 폼
파일이 진실: aggregate 등 구조 지표는 metrics.json에서 읽는다.
업로드된 surface 파일은 텍스트로만 서빙(이스케이프) — 실행 절대 없음.
"""
from __future__ import annotations

import difflib
import json
import re
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


# new_run_id()(storage.py)가 생성하는 유일한 형식 — r<YYYYMMDD-HHMMSS>-<6hex>.
# 실사 결과 레포 분리 커밋(1f224b0) 이래 형식 변경 이력 없음(legacy 형식 부재).
_RUN_ID_RE = re.compile(r"^r[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def require_valid_run_id(run_id: str) -> Path:
    """run id 화이트리스트 검증(SRV-003) — 통과 시 실존 run 디렉터리를 반환.

    쿼리/경로 파라미터로 들어온 run id 계열 입력을 파일시스템 경로에 결합하기
    전에 반드시 이 단일 지점을 거친다 (compare-reports 등 신규 엔드포인트도
    재사용 — TASK-004). 2중 방어:
      1) 형식: _RUN_ID_RE 화이트리스트만 수용 — 경로 구분자·'..'·절대경로가
         원천 차단된다. pathlib은 절대경로 인자가 오면 앞부분을 통째로
         대체하므로(`root/"runs"/"/x" == Path("/x")`) 결합 전 차단이 필수.
      2) 경계: resolve 후 runs/ 하위인지 + 실존 디렉터리인지 확인
         (1)이 뚫려도 데이터 루트 밖 접근이 불가한 방어 계층.
    불합격은 기존 관례(app.py run_detail)와 동일하게 404 'run ... not found'.
    """
    root = _state()["root"]
    runs_root = (root / "runs").resolve()
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, f"run {run_id!r} not found")
    run_dir = (runs_root / run_id).resolve()
    if not run_dir.is_relative_to(runs_root) or not run_dir.is_dir():
        raise HTTPException(404, f"run {run_id!r} not found")
    return run_dir


def _run_rows(dataset: str, all_history: bool) -> list[dict]:
    from server.aggregate import derive_macro_pr
    from server.app import list_runs
    from server.storage import read_json

    rows = list_runs(dataset=dataset, all_history=all_history)["runs"]
    root = _state()["root"]
    for r in rows:
        mj = read_json(root / "runs" / r["run_id"] / "metrics.json") or {}
        agg = mj.get("aggregate", {})
        # 구형 run 폴백(REQ-001): 채점 시점에 고정된 metrics.json aggregate에
        # macro_precision 키가 없으면 이미 읽은 mj["attributes"]에서 읽기 시점
        # 파생 — 추가 파일 IO 0 (run당 metrics.json 읽기 1회 유지, SRV-006 제약).
        # 파생 재료(attributes)가 없으면 키 없이 두어 '—' 표시.
        if "macro_precision" not in agg and mj.get("attributes"):
            agg = {**agg, **derive_macro_pr(mj["attributes"])}
        r["aggregate"] = agg
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


def _active_compare_jobs(dataset: str) -> list[dict]:
    """리더보드 진행 중(queued/running) compare job 안내 데이터 (USER-REQ-005).

    - 스캔은 TASK-007 iter_jobs 단일 원천을 재사용한다 (SRV-010 — 규칙 복제 없음).
    - 데이터셋 사전 필터: 리더보드 표시 rows 는 버전명당 최신 run 만일 수 있어
      매치 기준으로 부적합 — 해당 dataset 의 **전체 이력** run id 집합을 DB 에서
      직접 확보(list_runs(all_history=True) 와 동등, metrics 조인 없는 경량 조회)한
      뒤 pair 디렉터리명을 '__' 로 갈라 run id 매치로 무관 dataset job 을 배제한다.
    - SRV-011 흡수 근거: iter_jobs 는 job 당 job.json 1회만 읽으며 여기서 md 확인
      등 추가 파일 읽기를 하지 않는다. 이름-필터를 파일 읽기 전 단계로 내리는
      최적화는 현 규모(전 job 수십 건 이하)에서 불요 — 규모 초과 시 SRV-011
      backlog(인덱스/캐시)와 함께 검토.
    - 손상(None) job 은 reconcile 표현상 failed 라 진행 중 대상이 아님 — 제외.
    """
    from server.compare import iter_jobs

    run_ids = {r["run_id"] for r in _state()["conn"].execute(
        "SELECT run_id FROM runs WHERE dataset=?", (dataset,))}
    active = []
    for pair, job in iter_jobs(_state()["root"]):
        if job is None or job.get("status") not in ("queued", "running"):
            continue
        if not any(rid in run_ids for rid in pair.split("__")):
            continue
        active.append({"pair": pair, **job})
    # 정렬은 created_at 역순만 — finished_at 은 손상 job 에서 호출마다 변동하는
    # 값이라 정렬 키·안정 식별자로 쓰지 않는다 (final-verdict OBS-2).
    active.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return active


@router.get("/d/{dataset}", response_class=HTMLResponse)
def leaderboard(request: Request, dataset: str, all: int = 0):
    rows = _run_rows(dataset, all_history=bool(all))
    audit = [dict(x) for x in _state()["conn"].execute(
        "SELECT * FROM label_audit WHERE dataset=? ORDER BY id DESC LIMIT 5",
        (dataset,))]
    return templates.TemplateResponse(request, "leaderboard.html", {
        "dataset": dataset, "rows": rows,
        "all_history": bool(all), "audit": audit,
        "active_jobs": _active_compare_jobs(dataset),
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
    from server.storage import read_json

    # SRV-003: a·b는 쿼리 파라미터라 '..'/절대경로를 담을 수 있다 —
    # 경로 결합·파일 읽기 전에 화이트리스트 검증(불합격 404).
    dir_a = require_valid_run_id(a)
    dir_b = require_valid_run_id(b)

    def _files(run_id: str, run_dir: Path) -> dict[str, tuple[str, str]]:
        """{norm_key: (actual_relpath, content)} — 실제 경로는 diff 헤더 표시용."""
        base = run_dir / "surface"
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

    fa, fb = _files(a, dir_a), _files(b, dir_b)
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
    prov_a = read_json(dir_a / "run_provenance.json") or {}
    prov_b = read_json(dir_b / "run_provenance.json") or {}
    param_keys = sorted(set(prov_a) | set(prov_b))
    # REQ-002(TASK-005): 보고서 페이지 링크용 상태만 조회 — 기계적 diff 출력
    # 로직은 무수정 (있으면 링크에 상태 표시, 없으면 링크만).
    from server.compare import job_dir_of
    report_job = read_json(job_dir_of(_state()["root"], a, b) / "job.json") or {}
    return templates.TemplateResponse(request, "diff.html", {
        "a": a, "b": b, "diffs": diffs,
        "params": [(k, prov_a.get(k), prov_b.get(k)) for k in param_keys],
        "report_status": report_job.get("status"),
    })


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


# =====================================================================
# compare-runs 보고서 열람 (REQ-002 / TASK-005) — /compare/* prefix 전용
# (INT-002: 기존 client 소비 URL·/api/runs/ 하위와 무교차)
# =====================================================================

@router.get("/compare/", response_class=HTMLResponse)
def compare_list(request: Request):
    """전체 compare job 목록 페이지 (USER-REQ-006 페이지 절반, TASK-008).

    - literal 라우트 — /compare/{pair}({pair}=[^/]+ 는 빈 세그먼트 불매치)보다
      위에 등록해 명시성 확보(순서 무관하나 의도 고정). /compare (슬래시 없음)는
      FastAPI redirect_slashes 가 307 로 이곳에 도달시킨다 — 라우트 해석 테스트로
      실증 (INT-002 caveat).
    - 데이터는 TASK-007 iter_jobs + _corrupt_job_repr 재사용 — 목록 API
      (GET /api/compare-reports)와 동일 표현·동일 정렬(created_at 역순, null 맨 뒤).
      손상 job 의 finished_at 은 호출마다 변동(OBS-2) — 표시만 하고 정렬 키로
      쓰지 않는다. 자동 폴링 없음(수동 새로고침 — non_goal).
    """
    from server.compare import _corrupt_job_repr, iter_jobs

    jobs = [{"pair": pair, **(job if job is not None else _corrupt_job_repr(pair))}
            for pair, job in iter_jobs(_state()["root"])]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return templates.TemplateResponse(request, "compare_list.html", {"jobs": jobs})


@router.get("/compare/{pair}", response_class=HTMLResponse)
def compare_page(request: Request, pair: str):
    """job 상태 페이지 — queued/running 폴링, failed 오류 표시, done 보고서 링크.
    job 이 없어도 200 (생성 버튼 표시) — run id 는 TASK-003 헬퍼로 검증."""
    from server.compare import REPORT_NAMES, job_dir_of, split_pair
    from server.storage import read_json

    a, b = split_pair(pair)
    require_valid_run_id(a)
    require_valid_run_id(b)
    job = read_json(job_dir_of(_state()["root"], a, b) / "job.json")
    return templates.TemplateResponse(request, "compare.html", {
        "a": a, "b": b, "job": job if isinstance(job, dict) else None,
        "report_names": REPORT_NAMES,
        "report_name": None, "report_content": None, "report_html": None,
    })


@router.get("/compare/{pair}/{name}", response_class=HTMLResponse)
def compare_report_view(request: Request, pair: str, name: str):
    """보고서 md 열람 — name 은 고정 화이트리스트(그 외 404).

    에이전트 산출 md 는 신뢰 불가 업로드 표면 텍스트에서 파생된 내용이라
    raw HTML 렌더 시 스크립트 주입이 가능하다 (SRV-005). 가독성을 위해
    mdrender.render_markdown_safe 로 안전 서브셋만 렌더한다 — 입력을 전량
    escape 한 뒤 화이트리스트 태그만 방출하는 closed-by-construction 렌더러라
    script/이벤트핸들러/위험 URL 이 결과에 나타날 수 없다(TASK-009). 원문(raw)
    md 는 <details> 안 autoescape <pre> 로 함께 서빙하되 |safe 는 적용하지
    않는다(SRV-005 불변식 유지)."""
    from server.compare import REPORT_NAMES, job_dir_of, split_pair
    from server.mdrender import render_markdown_safe

    a, b = split_pair(pair)
    require_valid_run_id(a)
    require_valid_run_id(b)
    if name not in REPORT_NAMES:
        raise HTTPException(404, f"no such report {name!r}")
    path = job_dir_of(_state()["root"], a, b) / f"{name}.md"
    if not path.is_file():
        raise HTTPException(404, f"no such report {name!r}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(request, "compare.html", {
        "a": a, "b": b, "job": None, "report_names": REPORT_NAMES,
        "report_name": name,
        "report_content": raw,                       # <details> 원문 — |safe 금지
        "report_html": render_markdown_safe(raw),    # 안전 렌더 HTML(Markup)
    })


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
