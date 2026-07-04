"""S5 (diff 버전정렬) + S6 (run 삭제) — 서버 standalone 스모크.
test_server_http.py 와 동일한 인라인 plr_json/헬퍼 스타일(클라이언트 표면 없이 ingest)."""
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


def _surface_targz(version_dir: str, person_yaml: bytes) -> bytes:
    """prompts/<version_dir>/person.yaml + 공통 py/vocab (버전 무관, 내용 동일)."""
    return _targz_bytes({
        f"prompts/{version_dir}/person.yaml": person_yaml,
        "plr_prompts.py": b"# surface stub\n",
        "vocab.yaml": b"gender: [male, female]\n",
    })


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_SERVER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("EVAL_SERVER_TOKEN", "sekrit")
    from server.app import app
    with TestClient(app) as c:
        r = c.post("/api/datasets", headers=TOKEN,
                   files={"archive": ("d.tgz", _dataset_targz(), "application/gzip")},
                   data={"name": "http_ds"})
        assert r.status_code in (200, 201), r.text
        yield c


def _submit(c, version: str, rows: list[dict], surface: bytes):
    return c.post("/api/runs", headers=TOKEN,
                  data={"dataset": "http_ds", "version_label": version},
                  files={"attributes": ("attributes.jsonl", _attrs_bytes(rows), "application/json"),
                         "surface": ("s.tgz", surface, "application/gzip")})


def _default_rows():
    return [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]


# =====================================================================
# S5 — diff 버전정렬
# =====================================================================

def test_diff_aligns_prompt_versions_as_single_entry(client):
    surface_a = _surface_targz("v1_5", b"system: prompt A\n")
    surface_b = _surface_targz("v1_3", b"system: prompt B\n")
    ra = _submit(client, "v1.5", _default_rows(), surface_a)
    rb = _submit(client, "v1.3", _default_rows(), surface_b)
    assert ra.status_code == 201, ra.text
    assert rb.status_code == 201, rb.text
    run_a, run_b = ra.json()["run_id"], rb.json()["run_id"]

    page = client.get(f"/diff?a={run_a}&b={run_b}")
    assert page.status_code == 200
    text = page.text

    # person.yaml: 단일 diff 항목(헤더 1개)의 내용 diff (양면), 추가/삭제 2개로 갈라지지 않음
    assert text.count("<h2><code>prompts/person.yaml</code></h2>") == 1, \
        f"person.yaml 은 하나의 diff 항목이어야 함 (버전 세그먼트 무시 정렬): {text}"
    assert "prompt A" in text and "prompt B" in text
    # 헤더는 실제 버전 경로를 보여줘야 함(어느 버전인지 식별 가능)
    assert f"{run_a}/prompts/v1_5/person.yaml" in text
    assert f"{run_b}/prompts/v1_3/person.yaml" in text
    # non-prompt 파일(py/vocab)은 내용 동일 — diff 항목 없음(회귀 없음)
    assert "plr_prompts.py" not in text
    assert "vocab.yaml" not in text


def test_diff_non_prompt_files_unchanged_path_matching(client):
    """non-prompt 파일은 상대경로 그대로 정렬 — 내용이 다르면 정상적으로 diff 됨."""
    surface_a = _targz_bytes({
        "prompts/v1_5/person.yaml": b"system: same\n",
        "plr_prompts.py": b"# version A\n",
    })
    surface_b = _targz_bytes({
        "prompts/v1_3/person.yaml": b"system: same\n",
        "plr_prompts.py": b"# version B\n",
    })
    ra = _submit(client, "v1.5", _default_rows(), surface_a)
    rb = _submit(client, "v1.3", _default_rows(), surface_b)
    assert ra.status_code == 201, ra.text
    assert rb.status_code == 201, rb.text
    run_a, run_b = ra.json()["run_id"], rb.json()["run_id"]

    page = client.get(f"/diff?a={run_a}&b={run_b}")
    assert page.status_code == 200
    text = page.text
    assert "plr_prompts.py" in text
    assert "version A" in text and "version B" in text
    # 동일 내용의 person.yaml (버전 세그먼트만 다름) — diff 항목 없음
    assert "person.yaml" not in text


# =====================================================================
# S6 — run 삭제
# =====================================================================

def test_delete_run_removes_dir_and_db_rows(client, tmp_path):
    r = _submit(client, "v1", _default_rows(), _surface_targz("v1", b"system: x\n"))
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    d = client.delete(f"/api/runs/{run_id}", headers=TOKEN)
    assert d.status_code in (200, 204), d.text
    if d.status_code == 200:
        assert d.json().get("deleted") == run_id

    # /api/runs 목록에서 사라짐
    listed = client.get("/api/runs", headers=TOKEN).json()["runs"]
    assert all(row["run_id"] != run_id for row in listed)

    # /r/{id} 404
    assert client.get(f"/r/{run_id}").status_code == 404


def test_delete_bogus_run_404(client):
    d = client.delete("/api/runs/no-such-run", headers=TOKEN)
    assert d.status_code == 404


def test_delete_without_token_rejected(client):
    r = _submit(client, "v1", _default_rows(), _surface_targz("v1", b"system: x\n"))
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    d = client.delete(f"/api/runs/{run_id}")  # no X-Auth-Token
    assert d.status_code == 401

    # 삭제되지 않았어야 함
    listed = client.get("/api/runs", headers=TOKEN).json()["runs"]
    assert any(row["run_id"] == run_id for row in listed)


def test_delete_survives_reconcile_on_restart(tmp_path, monkeypatch):
    """삭제 후 재기동(reconcile_and_rebuild)에도 되살아나지 않아야 함 —
    디렉터리 자체를 지우므로 리플레이할 파일이 없다."""
    monkeypatch.setenv("EVAL_SERVER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("EVAL_SERVER_TOKEN", "sekrit")
    from server.app import app

    with TestClient(app) as c:
        c.post("/api/datasets", headers=TOKEN,
               files={"archive": ("d.tgz", _dataset_targz(), "application/gzip")},
               data={"name": "http_ds"})
        r = _submit(c, "v1", _default_rows(), _surface_targz("v1", b"system: x\n"))
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]

        d = c.delete(f"/api/runs/{run_id}", headers=TOKEN)
        assert d.status_code in (200, 204), d.text

    # 새 TestClient == 재기동 (lifespan startup 이 reconcile_and_rebuild 재실행)
    with TestClient(app) as c2:
        listed = c2.get("/api/runs", headers=TOKEN).json()["runs"]
        assert all(row["run_id"] != run_id for row in listed)
        assert c2.get(f"/r/{run_id}").status_code == 404
