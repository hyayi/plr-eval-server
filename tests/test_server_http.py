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
    """S2/AC1: 리더보드는 macro F1 + accuracy 2개 지표 컬럼만; 속성별 컬럼 없음."""
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = _submit(client, "v1", rows)
    assert r.status_code == 201, r.text

    page = client.get("/d/http_ds")
    assert page.status_code == 200
    assert "macro F1" in page.text
    assert "accuracy" in page.text
    assert "gender acc" not in page.text
    assert "gender F1" not in page.text


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
