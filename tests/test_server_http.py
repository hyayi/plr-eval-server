"""서버 HTTP 스모크 — 클라이언트 표면 없이 인라인 plr_json 으로 ingest→채점→렌더 검증.
runners/plr_parse 등 lab 전용 모듈을 import 하지 않는다(서버 레포 self-contained 증명).
서버는 plr_json 시맨틱을 재검증하지 않으므로(P2-1) 인라인 dict 로 충분하다."""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

TOKEN = {"X-Auth-Token": "sekrit"}


def _person(gender: str, margin: float | None = None) -> dict:
    gs: dict = {"male": 0.0, "female": 0.0, "selected": gender}
    if margin is not None:
        gs["decision_margin"] = margin
    return {"object_type": "person", "attributes": {"gender_scores": gs}}


def _targz_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _dataset_targz() -> bytes:
    """crops + labels.jsonl + manifest.yaml 을 담은 데이터셋 아카이브(인라인)."""
    img = io.BytesIO()
    Image.new("RGB", (60, 90), (90, 90, 90)).save(img, format="JPEG")
    crop = img.getvalue()
    manifest = b"n: 2\ncreated: '2026-07-04'\nsource_note: http-smoke\nattributes:\n  gender: {}\n"
    labels = (b'{"obj_id": "a", "labels": {"gender": "male"}}\n'
              b'{"obj_id": "b", "labels": {"gender": "female"}}\n')
    return _targz_bytes({
        "crops/a.jpg": crop, "crops/b.jpg": crop,
        "manifest.yaml": manifest, "labels.jsonl": labels,
    })


def _attrs_bytes(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)).encode()


def _surface_targz() -> bytes:
    # 서버가 해제·해시만 하는 최소 표면 번들(내용 무관, provenance 생략 시 해시대조 스킵).
    return _targz_bytes({"prompts/v/person.yaml": b"system: x\n",
                         "plr_prompts.py": b"# surface stub\n"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_SERVER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("EVAL_SERVER_TOKEN", "sekrit")
    from server.app import app
    with TestClient(app) as c:
        # 데이터셋 등록
        r = c.post("/api/datasets", headers=TOKEN,
                   files={"archive": ("d.tgz", _dataset_targz(), "application/gzip")},
                   data={"name": "http_ds"})
        assert r.status_code in (200, 201), r.text
        yield c


def _submit(c, version: str, rows: list[dict]):
    return c.post("/api/runs", headers=TOKEN,
                  data={"dataset": "http_ds", "version_label": version},
                  files={"attributes": ("attributes.jsonl", _attrs_bytes(rows), "application/json"),
                         "surface": ("s.tgz", _surface_targz(), "application/gzip")})


def test_submit_scores_and_renders(client):
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    # 지표 — run_detail은 {"meta": ..., "metrics": {"attributes": {attr: ...}, ...}}
    m = client.get(f"/api/runs/{run_id}", headers=TOKEN)
    assert m.status_code == 200
    assert m.json()["metrics"]["attributes"]["gender"]["n"] == 2
    # 렌더 산출물
    assert client.get(f"/api/runs/{run_id}/report.html", headers=TOKEN).status_code == 200
    assert client.get(f"/api/runs/{run_id}/gallery.html", headers=TOKEN).status_code == 200


def test_structurally_broken_row_rejected(client):
    # plr_json 키 누락 → 422 (서버는 시맨틱은 안 보지만 구조는 지킴)
    bad = _attrs_bytes([{"obj_id": "a", "object_type": "person"}])
    r = client.post("/api/runs", headers=TOKEN,
                    data={"dataset": "http_ds", "version_label": "bad"},
                    files={"attributes": ("attributes.jsonl", bad, "application/json"),
                           "surface": ("s.tgz", _surface_targz(), "application/gzip")})
    assert r.status_code == 422


def test_run_page_links_report_and_gallery(client):
    """S1/AC3: run 페이지에 report/gallery 링크가 있고, 그 라우트는 200;
    존재하지 않는 run의 report.html은 404 유지."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    page = client.get(f"/r/{run_id}")
    assert page.status_code == 200
    assert f"/api/runs/{run_id}/report.html" in page.text
    assert f"/api/runs/{run_id}/gallery.html" in page.text

    assert client.get(f"/api/runs/{run_id}/report.html").status_code == 200
    assert client.get(f"/api/runs/{run_id}/gallery.html").status_code == 200
    assert client.get("/api/runs/no-such-run/report.html").status_code == 404


def test_leaderboard_slim_columns(client):
    """S2/AC1 + REQ-001: 리더보드는 집계 컬럼 4개(macro F1/accuracy/
    macro precision/macro recall)만; 속성별 컬럼 없음."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text

    page = client.get("/d/http_ds")
    assert page.status_code == 200
    assert "macro F1" in page.text
    assert "accuracy" in page.text
    assert "macro precision" in page.text
    assert "macro recall" in page.text
    assert "gender acc" not in page.text
    assert "gender F1" not in page.text


def test_aggregate_macro_pr_derivation():
    """TASK-002: macro P/R 파생이 evalkit macro_f1 관례와 동일 —
    gt_classes(=recall 키 집합) 기준, 결측 클래스 0.0, round 4, 속성 단순 평균.
    aggregate()(채점 시점)와 derive_macro_pr(폴백 공유 헬퍼) 양 경로 단언."""
    from server.aggregate import aggregate, derive_macro_pr

    metrics = {
        # gt_classes = [female, male] (recall 키 집합). precision의 female 결측 → 0.0.
        # macro_recall = (0.5+1.0)/2 = 0.75, macro_precision = (0.0+0.5)/2 = 0.25
        "gender": {
            "classes": ["female", "male", "unknown"],
            "recall": {"female": 0.5, "male": 1.0},
            "precision": {"male": 0.5, "unknown": 0.9},  # unknown은 gt 아님 → 제외
            "macro_f1": 0.6, "accuracy": 0.75, "n": 4, "correct": 3,
        },
        # 완전 일치 속성 — macro_recall = macro_precision = 1.0
        "hat": {
            "classes": ["no", "yes"],
            "recall": {"no": 1.0, "yes": 1.0},
            "precision": {"no": 1.0, "yes": 1.0},
            "macro_f1": 1.0, "accuracy": 1.0, "n": 2, "correct": 2,
        },
    }
    d = derive_macro_pr(metrics)
    assert d["macro_recall"] == 0.875      # (0.75 + 1.0) / 2
    assert d["macro_precision"] == 0.625   # (0.25 + 1.0) / 2

    agg = aggregate(metrics)
    assert agg["macro_recall"] == 0.875
    assert agg["macro_precision"] == 0.625
    # 기존 키 유지 + 값 불변 (additive 변경만)
    assert agg["macro_f1"] == 0.8
    assert agg["macro_acc"] == 0.875
    assert agg["micro_acc"] == round(5 / 6, 4)
    assert agg["n_total"] == 6

    # 파생 재료 없음(recall 빈 dict) → None (리더보드 '—')
    empty = derive_macro_pr({"g": {"recall": {}, "precision": {}}})
    assert empty == {"macro_precision": None, "macro_recall": None}


def test_leaderboard_macro_pr_fallback_for_legacy_run(client, tmp_path):
    """TASK-002: aggregate에 macro P/R 키가 없는 구형 metrics.json도
    리더보드가 attributes에서 읽기 시점 파생해 값을 표시(재채점 없음)."""
    # 정답 a=male, b=female / 예측 둘 다 male →
    # recall {female:0.0, male:1.0}, precision {male:0.5} (female 예측 없음)
    # macro_recall = 0.5, macro_precision = (0.0+0.5)/2 = 0.25
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("male", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    # 구형 run 시뮬레이션: 채점 시점 파일에서 신규 키 제거
    mpath = tmp_path / "data" / "runs" / run_id / "metrics.json"
    mj = json.loads(mpath.read_text(encoding="utf-8"))
    for k in ("macro_precision", "macro_recall"):
        mj["aggregate"].pop(k, None)
    mpath.write_text(json.dumps(mj, ensure_ascii=False), encoding="utf-8")

    page = client.get("/d/http_ds")
    assert page.status_code == 200
    assert 'data-v="0.25"' in page.text   # macro precision (파생)
    assert 'data-v="0.5"' in page.text    # macro recall (파생)
    assert 'data-v="-1"' not in page.text  # 파생 성공 — '—'(data-v=-1) 셀 없음


def test_run_detail_top_level_keys_frozen(client):
    """INT-002 회귀 가드: GET /api/runs/{id} 최상위 키 집합 불변 —
    REQ-001 신규 키는 metrics.aggregate 내부에만 나타난다. submit 응답의
    기존 키도 유지(additive 변경만)."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text
    body = r.json()
    # submit 응답: 기존 키 유지 + aggregate 내부에 신규 키 additive
    assert {"run_id", "hash_verified", "git_dirty", "aggregate",
            "attributes", "skipped"} <= set(body)
    assert "macro_precision" in body["aggregate"]
    assert "macro_recall" in body["aggregate"]

    d = client.get(f"/api/runs/{body['run_id']}", headers=TOKEN)
    assert d.status_code == 200
    detail = d.json()
    assert set(detail) == {"meta", "metrics", "surface_files", "provenance"}
    agg = detail["metrics"]["aggregate"]
    for k in ("macro_f1", "macro_acc", "micro_acc", "n_total"):
        assert k in agg, f"기존 aggregate 키 누락: {k}"
    assert "macro_precision" in agg and "macro_recall" in agg


def test_run_page_class_detail(client):
    """S3/AC2: 라벨된 속성마다 체크박스 + class별(f1/precision/recall/acc) 표."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    page = client.get(f"/r/{run_id}")
    assert page.status_code == 200
    text = page.text
    # 속성 체크박스 (라벨된 속성 = gender 하나)
    assert 'data-target="attr-detail-gender"' in text
    assert 'type="checkbox"' in text
    # class별 표: 열 헤더 + class(male/female) 행
    assert "<th>f1</th>" in text
    assert "<th>precision</th>" in text
    assert "<th>recall</th>" in text
    assert "<th>acc</th>" in text
    assert "<td>male</td>" in text
    assert "<td>female</td>" in text


def test_dataset_push_structure_guard_rejects_missing_labels(tmp_path, monkeypatch):
    """데이터셋 push 구조 가드: labels.jsonl 이 없으면 422. 라벨 어휘(enum) 검증은
    클라이언트 `lab validate-dataset` 소관이라 서버는 재검증하지 않지만(plr_schema/
    vocab vendoring 없음), 채점에 필요한 구조는 지킨다."""
    monkeypatch.setenv("EVAL_SERVER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("EVAL_SERVER_TOKEN", "sekrit")
    from server.app import app

    img = io.BytesIO()
    Image.new("RGB", (60, 90), (90, 90, 90)).save(img, format="JPEG")
    no_labels = _targz_bytes({  # manifest + crops 있으나 labels.jsonl 누락
        "manifest.yaml": b"n: 1\ncreated: '2026-07-04'\nsource_note: t\nattributes:\n  gender: {}\n",
        "crops/a.jpg": img.getvalue(),
    })
    with TestClient(app) as c:
        r = c.post("/api/datasets", headers=TOKEN,
                   files={"archive": ("d.tgz", no_labels, "application/gzip")},
                   data={"name": "broken_ds"})
        assert r.status_code == 422, f"labels.jsonl 없는 데이터셋은 거부돼야: {r.status_code}"


# =====================================================================
# TASK-003 (SRV-003) — run id 화이트리스트 검증: 헬퍼 단위 + /diff 통합
# =====================================================================

def test_require_valid_run_id_accepts_real_run(client):
    """정상 케이스: 실제 submit 된 run id 는 통과하고 실존 run 디렉터리를 반환."""
    from server.web import require_valid_run_id

    r = _submit(client, "v1", [{"obj_id": "a", "plr_json": _person("male", 0.9)},
                               {"obj_id": "b", "plr_json": _person("female", 0.8)}])
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    run_dir = require_valid_run_id(run_id)
    assert run_dir.is_dir()
    assert run_dir.name == run_id
    assert run_dir.parent.name == "runs"


@pytest.mark.parametrize("bad_id", [
    "../quarantine/x",              # 상대 경로 순회
    "..",                           # 부모 참조 단독
    ".",                            # 현재 디렉터리
    "",                             # 빈 문자열
    "/etc",                         # 절대경로 — pathlib 결합 시 앞부분 통째 대체
    "/etc/passwd",
    "a/b",                          # 경로 구분자 포함
    "..\\quarantine\\x",            # 백슬래시 변형
    "r20260101-000000-abcdef/../x",  # 정상 형식 + 순회 접미
    "r20260101-000000-ABCDEF",      # hex 대문자 — new_run_id 는 소문자만 생성
    "r20260101-000000-abcdeg",      # hex 아님
    "no-such-run",                  # 형식 불일치
    "r20260101-000000-abcdef\n",    # 개행 주입
])
def test_require_valid_run_id_rejects_bad_input(client, bad_id):
    """거부 케이스: 경로 순회·절대경로·형식 불일치 전부 HTTPException 404."""
    from fastapi import HTTPException

    from server.web import require_valid_run_id

    with pytest.raises(HTTPException) as exc:
        require_valid_run_id(bad_id)
    assert exc.value.status_code == 404


def test_require_valid_run_id_rejects_wellformed_but_missing(client):
    """형식은 맞지만 runs/ 에 실존하지 않는 id 는 404 (실존 디렉터리 확인 계층)."""
    from fastapi import HTTPException

    from server.web import require_valid_run_id

    with pytest.raises(HTTPException) as exc:
        require_valid_run_id("r20200101-000000-abcdef")
    assert exc.value.status_code == 404


def test_diff_rejects_traversal_and_hides_quarantine(client, tmp_path):
    """/diff 통합: '..' 순회로 quarantine 격리본 surface 를 열람할 수 없어야 한다
    (SRV-003 재현 시나리오). 404 + 응답 본문에 파일 내용 미노출."""
    r = _submit(client, "v1", [{"obj_id": "a", "plr_json": _person("male", 0.9)},
                               {"obj_id": "b", "plr_json": _person("female", 0.8)}])
    assert r.status_code == 201, r.text
    good = r.json()["run_id"]

    # 격리본을 흉내낸 파일을 데이터 루트의 quarantine/ 에 심는다.
    marker = "QUARANTINE-LEAK-MARKER-6f2a"
    qdir = tmp_path / "data" / "quarantine" / "qrun" / "surface"
    qdir.mkdir(parents=True)
    (qdir / "leak.yaml").write_text(f"secret: {marker}\n", encoding="utf-8")

    for bad in ("../quarantine/qrun", "/etc", "a/b"):
        for params in ({"a": bad, "b": good}, {"a": good, "b": bad}):
            resp = client.get("/diff", params=params)
            assert resp.status_code == 404, (params, resp.status_code)
            assert marker not in resp.text, f"격리본 내용이 노출됨: {params}"

    # 절대경로로 quarantine surface 부모를 직접 지정하는 변형도 차단.
    resp = client.get("/diff", params={"a": str(qdir.parent), "b": good})
    assert resp.status_code == 404
    assert marker not in resp.text


def test_diff_normal_pair_still_works(client):
    """정상 run id 쌍의 /diff 는 기존과 동일하게 200 (동작 불변 —
    상세 diff 회귀는 test_server_diff_and_delete.py 가 커버)."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    ra = _submit(client, "v1", rows)
    rb = _submit(client, "v2", rows)
    assert ra.status_code == 201 and rb.status_code == 201
    run_a, run_b = ra.json()["run_id"], rb.json()["run_id"]

    resp = client.get("/diff", params={"a": run_a, "b": run_b})
    assert resp.status_code == 200
    assert run_a in resp.text and run_b in resp.text
