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
