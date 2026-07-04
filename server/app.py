"""plr-eval-server — FastAPI 엔트리.

배포 계약: uvicorn --workers 1 **고정** (쓰기 직렬화가 프로세스 내
asyncio.Lock이므로 다중 워커 금지). 기동 복구(reconcile_and_rebuild)는
startup에서 요청 수락 전에 완료된다.

실행: uvicorn server.app:app --host 0.0.0.0 --port 8890 --workers 1
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# 이 레포 루트를 import 경로에 — vendored evalkit/plr_schema(공유 계약 복본)를
# 로드하기 위함. lab 레포를 역참조하지 않는다(self-contained).
# 업로드된 표면 코드는 절대 import하지 않는다(RCE 방지).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server import db as dbm
from server.storage import data_root, reconcile_and_rebuild

STATE: dict = {}  # {"root": Path, "conn": sqlite3.Connection, "rebuild": dict}


@asynccontextmanager
async def lifespan(app: FastAPI):
    root = data_root()
    conn = dbm.connect(root / "db.sqlite")
    dbm.init_schema(conn)
    # 요청 수락 전 정합 복구 — DB는 파일 리플레이로 재구축되는 파생 캐시.
    STATE["rebuild"] = reconcile_and_rebuild(root, conn)
    STATE["root"], STATE["conn"] = root, conn
    yield
    conn.close()
    STATE.clear()


app = FastAPI(title="plr-eval-server", lifespan=lifespan)

# 웹 UI 라우터는 앱 정의 후 등록 (순환 import 회피를 위해 지연 include — 파일 하단).


@app.middleware("http")
async def token_guard(request: Request, call_next):
    """변이 메서드는 X-Auth-Token 요구 (EVAL_SERVER_TOKEN 설정 시).
    조회(GET/HEAD)는 면제 — 사내 LAN 열람용."""
    token = os.environ.get("EVAL_SERVER_TOKEN", "")
    if token and request.method not in ("GET", "HEAD"):
        if request.headers.get("X-Auth-Token") != token:
            return JSONResponse({"error": "invalid or missing X-Auth-Token"},
                                status_code=401)
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "data_root": str(STATE.get("root", "")),
        "rebuild": STATE.get("rebuild", {}),
    }


# =====================================================================
# Datasets API (AC1, AC12)
# =====================================================================

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402

from fastapi import File, Form, HTTPException, UploadFile  # noqa: E402

from server.storage import (  # noqa: E402
    ExtractError,
    find_dataset_root,
    safe_extract_targz,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{0,63}$")


def _require_valid_dataset_structure(ds_dir) -> None:
    """데이터셋 구조 가드 — 채점에 필요한 형식만 확인(manifest 파싱 · labels.jsonl
    의 obj_id 행 · crops 이미지 존재). 라벨 값의 어휘(enum) 검증은 클라이언트
    `lab validate-dataset` 소관이며 서버는 그 검증을 신뢰하고 재검증하지 않는다
    (attributes.jsonl 과 동일한 신뢰 모델 — SPEC:41). 그래서 서버는 plr_schema/
    vocab.yaml 을 vendoring 하지 않는다."""
    import yaml

    try:
        meta = yaml.safe_load((ds_dir / "manifest.yaml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"manifest.yaml parse 실패: {exc}")
    if not isinstance(meta, dict):
        raise HTTPException(422, "manifest.yaml 이 매핑이 아님")

    labels = ds_dir / "labels.jsonl"
    if not labels.exists():
        raise HTTPException(422, "labels.jsonl 없음")
    n = 0
    for lineno, line in enumerate(labels.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            if "obj_id" not in json.loads(line):
                raise KeyError("obj_id 누락")
        except Exception as exc:  # noqa: BLE001 — 첫 위반 지점 그대로 보고
            raise HTTPException(422, f"labels.jsonl line {lineno}: {exc}")
        n += 1
    if n == 0:
        raise HTTPException(422, "labels.jsonl 이 비어있음")

    crops = ds_dir / "crops"
    if not crops.is_dir() or not any(crops.glob("*.jpg")):
        raise HTTPException(422, "crops/ 에 이미지(.jpg)가 없음")


async def _save_upload(upload: UploadFile, dest) -> None:
    """multipart를 디스크로 스트리밍 (메모리 버퍼링 금지)."""
    with open(dest, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            f.write(chunk)


@app.get("/api/datasets")
def list_datasets() -> dict:
    rows = STATE["conn"].execute(
        "SELECT name, created_at, created_by, n_crops, attrs_json FROM datasets "
        "ORDER BY name").fetchall()
    return {"datasets": [dict(r) | {"attributes": json.loads(r["attrs_json"])}
                         for r in rows]}


@app.post("/api/datasets", status_code=201)
async def register_dataset(
    name: str = Form(...),
    created_by: str = Form(""),
    archive: UploadFile = File(...),
) -> dict:
    root, conn = STATE["root"], STATE["conn"]
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(400, "invalid dataset name (영숫자/._- 만, 64자 이내)")
    dest = root / "datasets" / name
    if dest.exists():
        raise HTTPException(
            409, f"dataset {name!r} already exists — 크롭/구성이 바뀌었으면 "
                 f"새 이름(예: {name}_v2)으로 등록하세요 (등록 후 크롭은 불변, "
                 f"라벨만 PATCH로 정정 가능)")

    tmp = Path(tempfile.mkdtemp(dir=root, prefix=".ingest-"))
    try:
        tar_path = tmp / "upload.tgz"
        await _save_upload(archive, tar_path)
        try:
            safe_extract_targz(tar_path, tmp / "x")
        except ExtractError as exc:
            raise HTTPException(400, f"archive rejected: {exc}")
        ds_root = find_dataset_root(tmp / "x")
        if ds_root is None:
            raise HTTPException(400, "manifest.yaml not found in archive "
                                     "(최상위 또는 단일 하위 디렉터리에 있어야 함)")
        _require_valid_dataset_structure(ds_root)

        created_at = datetime.now().isoformat(timespec="seconds")
        (ds_root / "registered.json").write_text(json.dumps(
            {"created_at": created_at, "created_by": created_by},
            ensure_ascii=False), encoding="utf-8")

        from evalkit.dataset import declared_attributes
        async with dbm.WRITE_LOCK:
            if dest.exists():
                raise HTTPException(409, f"dataset {name!r} already exists")
            shutil.move(str(ds_root), str(dest))  # 원자 이동 (같은 볼륨)
            n_crops = len(list((dest / "crops").glob("*.jpg")))
            dbm.upsert_dataset(conn, name, created_at, created_by,
                               n_crops, declared_attributes(dest))
        return {"name": name, "n_crops": n_crops}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rescore_dataset_runs(root, conn, dataset: str) -> list[str]:
    """등록 데이터셋의 전 run 재채점 — 라벨 정정 후 호출 (WRITE_LOCK 안에서).
    예측 원본(attributes.jsonl) 재사용이라 GPU 불필요."""
    from server.scoring import score_run
    from server.storage import read_json

    ds_dir = root / "datasets" / dataset
    rescored: list[str] = []
    rows = conn.execute("SELECT run_id FROM runs WHERE dataset=?", (dataset,)).fetchall()
    for row in rows:
        run_dir = root / "runs" / row["run_id"]
        attrs = run_dir / "attributes.jsonl"
        meta = read_json(run_dir / "meta.json")
        if not meta or not attrs.exists():
            continue
        res = score_run(ds_dir, attrs)
        (run_dir / "metrics.json").write_text(
            json.dumps(res, ensure_ascii=False), encoding="utf-8")
        dbm.upsert_run(conn, meta, res["attributes"])
        rescored.append(row["run_id"])
    return rescored


@app.patch("/api/datasets/{name}/labels")
async def correct_labels(
    name: str,
    labels: UploadFile = File(...),
    changed_by: str = Form(""),
) -> dict:
    root, conn = STATE["root"], STATE["conn"]
    ds_dir = root / "datasets" / name
    if not ds_dir.is_dir():
        raise HTTPException(404, f"dataset {name!r} not found")

    changed_at = datetime.now().isoformat(timespec="seconds")
    labels_path = ds_dir / "labels.jsonl"
    backup = ds_dir / f"labels.jsonl.bak-{changed_at.replace(':', '')}"

    async with dbm.WRITE_LOCK:
        shutil.copy2(labels_path, backup)
        await _save_upload(labels, labels_path)
        ok, report = _validate_report(ds_dir)
        if not ok:
            shutil.copy2(backup, labels_path)  # 원복
            backup.unlink(missing_ok=True)
            raise HTTPException(422, f"correction rejected (원복됨):\n{report}")

        old = set(backup.read_text(encoding="utf-8").splitlines())
        new = set(labels_path.read_text(encoding="utf-8").splitlines())
        diff_summary = f"-{len(old - new)} +{len(new - old)} lines"

        rescored = _rescore_dataset_runs(root, conn, name)

        dbm.add_label_audit(conn, name, changed_at, changed_by, diff_summary, rescored)
        with open(ds_dir / "label_audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "changed_at": changed_at, "changed_by": changed_by,
                "diff_summary": diff_summary, "rescored_runs": rescored,
                "backup": backup.name,
            }, ensure_ascii=False) + "\n")

    return {"dataset": name, "diff_summary": diff_summary,
            "rescored_runs": rescored, "report": report}


# =====================================================================
# Runs API (AC2, AC3, AC7)
# =====================================================================

from server.storage import new_run_id, read_json, surface_hash as _surface_hash  # noqa: E402


def _require_nonempty_attributes_jsonl(path) -> int:
    """attributes.jsonl 구조 가드 — 각 행이 obj_id+plr_json 을 가진 JSON 인지만
    확인하고 빈 파일을 거부. 행 수 반환.

    plr_json 의 *시맨틱* 검증(스키마/어휘)은 클라이언트 re_score 가 쓰기 전에
    plr_schema.validate_plr 로 fail-loud 수행한다(RE-003) — 서버는 그 검증을
    신뢰하고 재검증하지 않는다(SPEC:41). 따라서 여기서 plr_schema 를 import 하지
    않으며, 서버는 채점에 필요한 형식(파싱 가능·키 존재·비어있지 않음)만 본다."""
    n = 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if "obj_id" not in rec or "plr_json" not in rec:
                    raise KeyError("obj_id/plr_json 필드 누락")
            except Exception as exc:  # noqa: BLE001 — 첫 위반 지점을 그대로 보고
                raise HTTPException(
                    422, f"attributes.jsonl line {lineno}: {exc}")
            n += 1
    if n == 0:
        raise HTTPException(422, "attributes.jsonl is empty")
    return n


@app.post("/api/runs", status_code=201)
async def submit_run(
    dataset: str = Form(...),
    version_label: str = Form(...),
    submitted_by: str = Form(""),
    attributes: UploadFile = File(...),
    surface: UploadFile = File(...),
    provenance: UploadFile | None = File(None),
) -> dict:
    root, conn = STATE["root"], STATE["conn"]
    ds_dir = root / "datasets" / dataset
    if not ds_dir.is_dir():
        raise HTTPException(404, f"dataset {dataset!r} not found — 먼저 등록하세요")

    run_id = new_run_id()
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    try:
        # 1) 파일 수신 (스트리밍) — meta.json은 맨 마지막에 쓴다:
        #    meta+metrics가 모두 있어야 완결 run (크래시 잔재는 기동 시 격리).
        await _save_upload(attributes, run_dir / "attributes.jsonl")
        tar_path = run_dir / ".surface.tgz"
        await _save_upload(surface, tar_path)
        prov: dict = {}
        if provenance is not None:
            await _save_upload(provenance, run_dir / "run_provenance.json")
            prov = read_json(run_dir / "run_provenance.json") or {}

        _require_nonempty_attributes_jsonl(run_dir / "attributes.jsonl")

        # 2) 표면 번들 해제 + 해시 재계산·대조 (run 시점 지문 vs 업로드 번들).
        #    업로드된 py는 저장·열람·diff 전용 — 여기서든 어디서든 절대
        #    import/실행하지 않는다 (RCE 방지 하드 규칙).
        try:
            safe_extract_targz(tar_path, run_dir / "surface")
        except ExtractError as exc:
            raise HTTPException(400, f"surface bundle rejected: {exc}")
        tar_path.unlink(missing_ok=True)
        # 번들 루트: 평평한 번들(surface_relpaths 그대로)이 표준, 단일
        # 최상위 디렉터리로 감싼 번들도 수용.
        bundle_root = run_dir / "surface"
        if not (bundle_root / "prompts").is_dir():
            subs = [d for d in bundle_root.iterdir() if d.is_dir()]
            if len(subs) == 1 and (subs[0] / "prompts").is_dir():
                bundle_root = subs[0]
        recomputed = _surface_hash(bundle_root)
        claimed = str(prov.get("surface_hash", ""))
        hash_verified = bool(claimed) and (claimed == recomputed)

        # 3) 채점 (lab과 동일 함수) — 쓰기 락 아래 직렬화.
        from evalkit.scoring import ScoringError
        from server.scoring import score_run

        async with dbm.WRITE_LOCK:
            try:
                res = score_run(ds_dir, run_dir / "attributes.jsonl")
            except ScoringError as exc:
                raise HTTPException(422, f"scoring failed: {exc}")

            (run_dir / "metrics.json").write_text(
                json.dumps(res, ensure_ascii=False), encoding="utf-8")
            meta = {
                "run_id": run_id, "dataset": dataset,
                "version_label": version_label,
                "surface_hash": recomputed,
                "hash_verified": hash_verified,
                "claimed_surface_hash": claimed,
                "submitter_lab_sha": str(prov.get("lab_sha", "")),
                "git_dirty": bool(prov.get("git_dirty", False)),
                "model": str(prov.get("model", "")),
                "max_tokens": prov.get("max_tokens"),
                "temperature": prov.get("temperature"),
                "reason_toggle": str(prov.get("reason", "")),
                "submitted_by": submitted_by,
                # microseconds: 같은 초 다중 제출도 재기동 리플레이 후 순서 보존
                "submitted_at": datetime.now().isoformat(timespec="microseconds"),
                "status": "scored",
            }
            (run_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            dbm.upsert_run(conn, meta, res["attributes"])

        return {
            "run_id": run_id,
            "hash_verified": hash_verified,
            "git_dirty": meta["git_dirty"],
            "aggregate": res["aggregate"],
            "attributes": {a: {"accuracy": m["accuracy"], "macro_f1": m["macro_f1"],
                               "n": m["n"]}
                           for a, m in res["attributes"].items()},
            "skipped": res["skipped"],
        }
    except HTTPException:
        shutil.rmtree(run_dir, ignore_errors=True)  # 불완전 run 즉시 정리
        raise
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


@app.get("/api/runs")
def list_runs(dataset: str | None = None, all_history: bool = False) -> dict:
    """리더보드 데이터 — 기본은 version_label당 최신 run, all_history=True면 전체."""
    conn = STATE["conn"]
    q = "SELECT * FROM runs"
    params: tuple = ()
    if dataset:
        q += " WHERE dataset=?"
        params = (dataset,)
    # 같은 초에 생성된 run은 run_id 정렬이 hex 꼬리로 갈릴 수 있어
    # 삽입 순서(rowid)를 최종 타이브레이커로 — "최신" 선택을 결정적으로.
    q += " ORDER BY submitted_at DESC, rowid DESC"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if not all_history:
        latest: dict[tuple, dict] = {}
        for r in rows:  # run_id 내림차순 → 첫 등장이 최신
            latest.setdefault((r["dataset"], r["version_label"]), r)
        rows = list(latest.values())
    for r in rows:
        r["metrics"] = {}
        for m in STATE["conn"].execute(
                "SELECT attribute, metric, value FROM metrics WHERE run_id=?",
                (r["run_id"],)):
            r["metrics"].setdefault(m["attribute"], {})[m["metric"]] = m["value"]
    return {"runs": rows}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    root = STATE["root"]
    run_dir = root / "runs" / run_id
    meta = read_json(run_dir / "meta.json")
    if not meta:
        raise HTTPException(404, f"run {run_id!r} not found")
    metrics = read_json(run_dir / "metrics.json") or {}
    surface_files = sorted(
        str(p.relative_to(run_dir / "surface"))
        for p in (run_dir / "surface").rglob("*") if p.is_file()
    ) if (run_dir / "surface").is_dir() else []
    return {"meta": meta, "metrics": metrics, "surface_files": surface_files,
            "provenance": read_json(run_dir / "run_provenance.json")}


# =====================================================================
# Web UI (server/web.py) — API 정의 뒤에 include (web이 app.list_runs를 참조)
# =====================================================================

from server.web import router as _web_router  # noqa: E402

app.include_router(_web_router)
