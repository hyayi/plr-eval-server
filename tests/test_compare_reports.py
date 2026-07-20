"""TASK-004/005 (REQ-002) — compare-reports 백엔드·UI 테스트.

claude CLI 를 실제로 호출하지 않는다: CLAUDE_BIN 을 가짜 실행 스크립트(fixture)로
바꿔 subprocess 실행부를 대체하고 상태 전이·dedupe·timeout·503 비활성·reconcile 을
검증한다. 실제 claude 호출 검증은 QA 단계 몫 (checklist verification.qa).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests.test_server_http import (  # noqa: E402 — 기존 인라인 ingest 헬퍼 재사용
    TOKEN,
    _attrs_bytes,
    _dataset_targz,
    _person,
    _surface_targz,
)

AGENT_NAMES = ("eval-run-fetcher", "eval-metric-differ",
               "eval-prompt-differ", "eval-why-analyst")

# 성공 스크립트 — 4종 md 생성 + env/args 덤프(보안 단언용). exit 0.
_OK_BODY = """\
env > "$WORKSPACE_DIR/env-dump.txt"
printf '%s\\n' "$@" > "$WORKSPACE_DIR/args-dump.txt"
for n in 00_run_summary 01_metric_diff 02_prompt_diff 03_analysis_result; do
  printf '# %s fixture\\n' "$n" > "$WORKSPACE_DIR/$n.md"
done
exit 0
"""

# 산출물 미완성 스크립트 — exit 0 이지만 2종만 생성 (가짜 완료 방지 검증).
_PARTIAL_BODY = """\
printf '# partial\\n' > "$WORKSPACE_DIR/00_run_summary.md"
printf '# partial\\n' > "$WORKSPACE_DIR/01_metric_diff.md"
exit 0
"""

# 비정상 종료 스크립트 — stderr 를 남기고 exit 3.
_FAIL_BODY = """\
echo "fixture stderr: boom" >&2
exit 3
"""

# 직렬화 검증 스크립트 — 시작/종료 epoch(ns) 기록 후 4종 md 생성.
_SLOW_BODY = """\
date +%s%N > "$WORKSPACE_DIR/start-ns.txt"
sleep 0.4
for n in 00_run_summary 01_metric_diff 02_prompt_diff 03_analysis_result; do
  printf '# %s fixture\\n' "$n" > "$WORKSPACE_DIR/$n.md"
done
date +%s%N > "$WORKSPACE_DIR/end-ns.txt"
exit 0
"""

_HANG_BODY = "sleep 30\nexit 0\n"


def _make_agents_src(tmp_path: Path) -> Path:
    """에이전트 정의 원본(plr-eval-server-master .claude/) 최소 fixture."""
    src = tmp_path / "master-checkout"
    agents = src / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name in AGENT_NAMES:
        (agents / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: fixture agent\n"
            f"tools: Read, Write, Bash, Glob\nmodel: sonnet\n---\n\n"
            f"# {name} 본문\ntools: 이 줄은 본문 — frontmatter 만 교체돼야 한다\n",
            encoding="utf-8")
    skill = src / ".claude" / "skills" / "compare-runs"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: compare-runs\n---\n\n# compare-runs\nPhase 0 원본 내용\n",
        encoding="utf-8")
    return src


def _make_fake_claude(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-claude"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    """compare env 를 구성한 TestClient 팩토리 — 테스트마다 스크립트/timeout 교체."""
    def _make(script_body: str = _OK_BODY, *, timeout: str = "60",
              agents_src: bool = True, claude_bin: str | None = None) -> TestClient:
        monkeypatch.setenv("EVAL_SERVER_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("EVAL_SERVER_TOKEN", "sekrit")
        if agents_src:
            monkeypatch.setenv("COMPARE_AGENTS_SRC", str(_make_agents_src(tmp_path)))
        else:
            monkeypatch.delenv("COMPARE_AGENTS_SRC", raising=False)
        monkeypatch.setenv(
            "CLAUDE_BIN", claude_bin or _make_fake_claude(tmp_path, script_body))
        monkeypatch.setenv("COMPARE_TIMEOUT_SEC", timeout)
        from server.app import app
        return TestClient(app)
    return _make


def _register_dataset(c: TestClient, name: str = "http_ds") -> None:
    r = c.post("/api/datasets", headers=TOKEN,
               files={"archive": ("d.tgz", _dataset_targz(), "application/gzip")},
               data={"name": name})
    assert r.status_code in (200, 201), r.text


def _submit(c: TestClient, version: str, dataset: str = "http_ds") -> str:
    rows = [{"obj_id": "a", "plr_json": _person("male", 0.9)},
            {"obj_id": "b", "plr_json": _person("female", 0.8)}]
    r = c.post("/api/runs", headers=TOKEN,
               data={"dataset": dataset, "version_label": version},
               files={"attributes": ("attributes.jsonl", _attrs_bytes(rows), "application/json"),
                      "surface": ("s.tgz", _surface_targz(), "application/gzip")})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def _wait_status(c: TestClient, pair: str, want: set[str], timeout: float = 15.0) -> dict:
    """상태 폴링 — TestClient 의 백그라운드 루프에서 job task 가 진행된다."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = c.get(f"/api/compare-reports/{pair}")
        if r.status_code == 200:
            last = r.json()
            if last["status"] in want:
                return last
        time.sleep(0.05)
    raise AssertionError(f"{pair} 가 {want} 에 도달하지 못함 — 마지막 상태: {last}")


# =====================================================================
# 비활성(503) — claude CLI/에이전트 정의 부재 환경 (k8s 기본 배포)
# =====================================================================

def test_post_disabled_returns_503_and_server_stays_up(make_client):
    """COMPARE_AGENTS_SRC 미설정 → POST 503 + 안내 본문, 다른 기능은 정상."""
    with make_client(agents_src=False) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert "비활성" in detail["error"]
        assert any("COMPARE_AGENTS_SRC" in x for x in detail["reasons"])
        assert "hint" in detail
        # 서버의 다른 기능은 정상 (SRV-004 acceptance)
        assert c.get("/").status_code == 200
        assert c.get("/d/http_ds").status_code == 200
        assert c.get(f"/api/runs/{a}", headers=TOKEN).status_code == 200


def test_post_disabled_when_claude_bin_missing(make_client):
    """에이전트 정의는 있어도 claude CLI 가 없으면 비활성 (shutil.which 판정)."""
    with make_client(claude_bin="/no/such/claude-bin") as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 503
        assert any("claude CLI 없음" in x for x in r.json()["detail"]["reasons"])


# =====================================================================
# 상태 전이·영속·실행 통제 (가짜 실행기)
# =====================================================================

def test_job_success_flow_and_persistence(make_client, tmp_path):
    """POST 202(queued) → done 전이, job.json·4종 md 데이터 볼륨 영속,
    에이전트 사본 tools 축소, env 화이트리스트(서버 토큰 미전달) 단언."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        pair = f"{a}__{b}"

        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued"
        assert body["run_a"] == a and body["run_b"] == b
        assert body["dataset"] == "http_ds"

        job = _wait_status(c, pair, {"done", "failed"})
        assert job["status"] == "done", job
        assert job["started_at"] and job["finished_at"]
        assert job["error"] is None

        # 파일이 진실 — job.json + 4종 md 가 데이터 볼륨에 영속
        job_dir = tmp_path / "data" / "compare_reports" / pair
        assert json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"] == "done"
        for n in ("00_run_summary", "01_metric_diff", "02_prompt_diff",
                  "03_analysis_result"):
            assert (job_dir / f"{n}.md").is_file()

        # 실행 인자: 셸 미경유 exec 배열 — 검증된 run id 만 인자로 전달
        args = (job_dir / "args-dump.txt").read_text(encoding="utf-8")
        assert f"/compare-runs {a} {b}" in args
        assert "--allowedTools" in args

        # env 화이트리스트: 서버 비밀 미전달 + 실행 컨텍스트 3종 주입 (SRV-005)
        env_dump = (job_dir / "env-dump.txt").read_text(encoding="utf-8")
        assert "EVAL_SERVER_TOKEN" not in env_dump
        assert f"EVAL_SERVER_DATA={tmp_path / 'data'}" in env_dump
        assert f"WORKSPACE_DIR={job_dir}" in env_dump
        assert "PYTHONPATH=" in env_dump

        # 서버 관리 사본: frontmatter tools 축소 (SRV-005 통제 1)
        agents_dir = tmp_path / "data" / "compare_reports" / "_agents" / ".claude"
        fm = {n: (agents_dir / "agents" / f"{n}.md").read_text(encoding="utf-8")
              for n in AGENT_NAMES}
        assert "tools: Read, Glob, Write\n" in fm["eval-prompt-differ"]   # Bash 제거
        assert "tools: Read, Write\n" in fm["eval-why-analyst"]           # Read/Write 만
        assert "tools: Read, Glob, Write\n" in fm["eval-run-fetcher"]     # Bash 제거
        assert "tools: Read, Write, Bash, Glob\n" in fm["eval-metric-differ"]  # Bash 유지
        # 본문의 'tools:' 줄은 교체되지 않아야 한다 (frontmatter 만)
        assert "이 줄은 본문" in fm["eval-run-fetcher"]
        # SKILL 사본 + headless 부기 (대화형 게이트 대체)
        skill = (agents_dir / "skills" / "compare-runs" / "SKILL.md").read_text(encoding="utf-8")
        assert "Phase 0 원본 내용" in skill
        assert "AskUserQuestion 등 대화형 질문을 일절 하지 않는다" in skill


def test_post_dedupe_same_pair(make_client):
    """동일 (a,b) 진행 중 재-POST 는 새 job 을 만들지 않고 현재 상태 반환(200)."""
    with make_client(_SLOW_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        r1 = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r1.status_code == 202
        created_at = r1.json()["created_at"]
        r2 = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r2.status_code == 200          # dedupe — 기존 job 재사용
        assert r2.json()["status"] in ("queued", "running")
        assert r2.json()["created_at"] == created_at
        _wait_status(c, f"{a}__{b}", {"done"})


def test_post_done_reuse_and_force_recreate(make_client, tmp_path):
    """done 재-POST 는 기존 결과 반환, force=1 이면 디렉터리 재생성 (SKILL Phase 0
    대화형 게이트의 서버 결정 대체)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        pair = f"{a}__{b}"
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        _wait_status(c, pair, {"done"})

        # 재사용 마커 — force 재생성 시 디렉터리가 비워지는지 확인용
        marker = tmp_path / "data" / "compare_reports" / pair / "stale-marker.txt"
        marker.write_text("stale", encoding="utf-8")

        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 200 and r.json()["status"] == "done"  # 기본 재사용
        assert marker.exists()

        r = c.post("/api/compare-reports?force=1", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 202 and r.json()["status"] == "queued"
        assert not marker.exists()            # 디렉터리 재생성(덮어쓰기)
        _wait_status(c, pair, {"done"})


def test_job_failed_on_nonzero_exit_then_retry_allowed(make_client):
    """비정상 종료 → failed + error·stderr_tail 기록. failed 재-POST 는 재생성
    허용 (plan review 비차단 관찰 1). 실행 중에도 서버는 정상."""
    with make_client(_FAIL_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        pair = f"{a}__{b}"
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        job = _wait_status(c, pair, {"done", "failed"})
        assert job["status"] == "failed"
        assert "exit code 3" in job["error"]
        assert "fixture stderr: boom" in (job["stderr_tail"] or "")
        # 서버 정상 동작 유지
        assert c.get("/d/http_ds").status_code == 200
        # failed → 재-POST 로 새 job 생성 (202)
        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 202 and r.json()["status"] == "queued"
        _wait_status(c, pair, {"failed"})


def test_job_failed_on_incomplete_outputs(make_client):
    """exit 0 이어도 4종 md 미완성이면 failed (가짜 완료 방지)."""
    with make_client(_PARTIAL_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        job = _wait_status(c, f"{a}__{b}", {"done", "failed"})
        assert job["status"] == "failed"
        assert "산출물 미완성" in job["error"]
        assert "02_prompt_diff.md" in job["error"]
        assert "03_analysis_result.md" in job["error"]


def test_job_timeout_kills_and_fails(make_client):
    """COMPARE_TIMEOUT_SEC 초과 → 프로세스 kill + status=failed(error=timeout)."""
    with make_client(_HANG_BODY, timeout="1") as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        job = _wait_status(c, f"{a}__{b}", {"done", "failed"}, timeout=20.0)
        assert job["status"] == "failed"
        assert "timeout" in job["error"]


def test_semaphore_serializes_concurrent_jobs(make_client):
    """동시 실행 상한 1 — 서로 다른 pair 2개를 연속 시작해도 실행 구간이 겹치지
    않는다 (가짜 실행기가 기록한 시작/종료 epoch-ns 로 판정)."""
    with make_client(_SLOW_BODY) as c:
        _register_dataset(c)
        r1, r2, r3 = _submit(c, "v1"), _submit(c, "v2"), _submit(c, "v3")
        assert c.post("/api/compare-reports", json={"a": r1, "b": r2},
                      headers=TOKEN).status_code == 202
        assert c.post("/api/compare-reports", json={"a": r1, "b": r3},
                      headers=TOKEN).status_code == 202
        _wait_status(c, f"{r1}__{r2}", {"done"})
        _wait_status(c, f"{r1}__{r3}", {"done"})

        from server.app import STATE
        base = STATE["root"] / "compare_reports"
        span1 = (int((base / f"{r1}__{r2}" / "start-ns.txt").read_text()),
                 int((base / f"{r1}__{r2}" / "end-ns.txt").read_text()))
        span2 = (int((base / f"{r1}__{r3}" / "start-ns.txt").read_text()),
                 int((base / f"{r1}__{r3}" / "end-ns.txt").read_text()))
        assert span1[1] <= span2[0] or span2[1] <= span1[0], \
            f"실행 구간 겹침 — 동시 상한 1 위반: {span1} vs {span2}"


def test_event_loop_not_blocked_while_running(make_client):
    """job 실행 중에도 기존 API/페이지가 정상 응답 (이벤트 루프 비블로킹)."""
    with make_client(_SLOW_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        # 실행이 끝나기 전(슬립 0.4s)에 다른 엔드포인트가 즉시 응답해야 한다
        assert c.get("/d/http_ds").status_code == 200
        assert c.get(f"/api/runs/{a}", headers=TOKEN).status_code == 200
        assert c.get("/api/runs", headers=TOKEN).status_code == 200
        _wait_status(c, f"{a}__{b}", {"done"})


# =====================================================================
# 입력 검증·계약 (TASK-003 헬퍼 / INT-002)
# =====================================================================

def test_post_rejects_traversal_run_ids(make_client):
    """'..'/절대경로/구분자 입력은 404 — TASK-003 require_valid_run_id 재사용."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        good = _submit(c, "v1")
        for bad in ("../quarantine/x", "/etc/passwd", "a/b",
                    "r20260101-000000-abcdef/../x"):
            for payload in ({"a": bad, "b": good}, {"a": good, "b": bad}):
                r = c.post("/api/compare-reports", json=payload, headers=TOKEN)
                assert r.status_code == 404, (payload, r.status_code)


def test_post_rejects_same_run_and_dataset_mismatch(make_client):
    """a==b 및 데이터셋 불일치 → 400."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c, "http_ds")
        _register_dataset(c, "other_ds")
        a = _submit(c, "v1", dataset="http_ds")
        b = _submit(c, "v1", dataset="other_ds")
        r = c.post("/api/compare-reports", json={"a": a, "b": a}, headers=TOKEN)
        assert r.status_code == 400
        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 400
        assert "데이터셋" in r.json()["detail"]


def test_post_requires_token(make_client):
    """POST 는 token_guard 자동 편입 — 토큰 없으면 401 (기존 변이 API 와 동일)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        r = c.post("/api/compare-reports", json={"a": a, "b": b})  # 토큰 없음
        assert r.status_code == 401


def test_status_get_reachable_and_404s(make_client):
    """GET /api/compare-reports/{pair} 라우트 도달성: 존재 job 200 / 미존재 404 /
    형식 불일치 pair 404. GET 은 토큰 면제."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        pair = f"{a}__{b}"
        assert c.get(f"/api/compare-reports/{pair}").status_code == 404  # job 없음
        assert c.post("/api/compare-reports", json={"a": a, "b": b},
                      headers=TOKEN).status_code == 202
        assert c.get(f"/api/compare-reports/{pair}").status_code == 200  # 토큰 없이
        for bad in ("evil__thing", f"{a}", f"{a}__{b}__x", "..__..",
                    f"{a}__..%2F..%2Fetc"):
            assert c.get(f"/api/compare-reports/{bad}").status_code == 404, bad
        _wait_status(c, pair, {"done"})


def test_run_detail_keys_frozen_with_compare_routes(make_client):
    """INT-002: compare 라우터 등록 후에도 GET /api/runs/{run_id} 최상위 키
    집합이 불변이고 단일 세그먼트 포획 회귀가 없다."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a = _submit(c, "v1")
        d = c.get(f"/api/runs/{a}", headers=TOKEN)
        assert d.status_code == 200
        assert set(d.json()) == {"meta", "metrics", "surface_files", "provenance"}


# =====================================================================
# 기동 reconcile — orphan·손상 job 정리 (SRV-004 (b))
# =====================================================================

def test_reconcile_marks_orphan_and_corrupt_jobs_failed(make_client, tmp_path):
    """재기동 시 queued/running orphan → failed, 파싱 불가 job.json → failed.
    done job 은 건드리지 않는다."""
    base = tmp_path / "data" / "compare_reports"
    orphan = "r20260101-000000-abcdef__r20260101-000001-abcdef"
    corrupt = "r20260101-000002-abcdef__r20260101-000003-abcdef"
    done = "r20260101-000004-abcdef__r20260101-000005-abcdef"
    for name, content in (
        (orphan, json.dumps({"run_a": orphan.split("__")[0],
                             "run_b": orphan.split("__")[1], "dataset": "d",
                             "status": "running", "created_at": "2026-01-01T00:00:00",
                             "started_at": "2026-01-01T00:00:01",
                             "finished_at": None, "error": None, "stderr_tail": None})),
        (corrupt, '{"status": "runn'),  # 부분 쓰기 손상 (비차단 관찰 2)
        (done, json.dumps({"run_a": done.split("__")[0],
                           "run_b": done.split("__")[1], "dataset": "d",
                           "status": "done", "created_at": "2026-01-01T00:00:00",
                           "started_at": "2026-01-01T00:00:01",
                           "finished_at": "2026-01-01T00:10:00",
                           "error": None, "stderr_tail": None})),
    ):
        d = base / name
        d.mkdir(parents=True)
        (d / "job.json").write_text(content, encoding="utf-8")

    with make_client(_OK_BODY) as c:   # TestClient 진입 = 재기동(lifespan startup)
        j = c.get(f"/api/compare-reports/{orphan}").json()
        assert j["status"] == "failed"
        assert "재시작" in j["error"]
        j = c.get(f"/api/compare-reports/{corrupt}").json()
        assert j["status"] == "failed"
        assert "손상" in j["error"]
        assert j["run_a"] == corrupt.split("__")[0]  # 디렉터리 이름에서 복원
        j = c.get(f"/api/compare-reports/{done}").json()
        assert j["status"] == "done"                 # 완료 job 불변


# =====================================================================
# TASK-005 — UI: /compare/* 상태 페이지·이스케이프 열람, 리더보드/diff 접점
# =====================================================================

def _plant_job(root: Path, a: str, b: str, status: str, *,
               error: str | None = None, stderr_tail: str | None = None,
               reports: dict[str, str] | None = None) -> Path:
    """데이터 볼륨에 job 을 직접 심는다 — 영속 상태에서의 열람 경로 검증."""
    d = root / "compare_reports" / f"{a}__{b}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({
        "run_a": a, "run_b": b, "dataset": "http_ds", "status": status,
        "created_at": "2026-07-20T00:00:00", "started_at": "2026-07-20T00:00:01",
        "finished_at": "2026-07-20T00:10:00" if status in ("done", "failed") else None,
        "error": error, "stderr_tail": stderr_tail,
    }, ensure_ascii=False), encoding="utf-8")
    for name, content in (reports or {}).items():
        (d / f"{name}.md").write_text(content, encoding="utf-8")
    return d


_FULL_REPORTS = {n: f"# {n} fixture\n" for n in (
    "00_run_summary", "01_metric_diff", "02_prompt_diff", "03_analysis_result")}


def test_compare_page_done_lists_reports(make_client, tmp_path):
    """done 상태: 200 + 00~03 보고서 링크 4종 + 재생성 버튼."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        _plant_job(tmp_path / "data", a, b, "done", reports=_FULL_REPORTS)
        page = c.get(f"/compare/{a}__{b}")
        assert page.status_code == 200
        for n in _FULL_REPORTS:
            assert f"/compare/{a}__{b}/{n}" in page.text
        assert "done" in page.text
        assert "다시 생성" in page.text


def test_compare_page_failed_shows_error(make_client, tmp_path):
    """failed 상태: error·stderr_tail 표시 + 다시 생성 버튼 (재시도 경로)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        _plant_job(tmp_path / "data", a, b, "failed",
                   error="claude 비정상 종료 (exit code 3)",
                   stderr_tail="fixture stderr: boom")
        page = c.get(f"/compare/{a}__{b}")
        assert page.status_code == 200
        assert "claude 비정상 종료 (exit code 3)" in page.text
        assert "fixture stderr: boom" in page.text
        assert "다시 생성" in page.text


def test_compare_page_polls_while_running(make_client, tmp_path):
    """queued/running 상태: 폴링 스크립트(setInterval + 상태 API URL) 포함."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        _plant_job(tmp_path / "data", a, b, "running")
        page = c.get(f"/compare/{a}__{b}")
        assert page.status_code == 200
        assert "생성 중" in page.text
        assert "setInterval" in page.text
        assert f"/api/compare-reports/{a}__{b}" in page.text


def test_compare_page_without_job_offers_create(make_client, tmp_path):
    """job 미존재: 200 + 생성 버튼 (직접 URL 진입 경로)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        page = c.get(f"/compare/{a}__{b}")
        assert page.status_code == 200
        assert "아직 생성된 보고서가 없습니다" in page.text
        assert "보고서 생성" in page.text


def test_report_view_escapes_malicious_md(make_client, tmp_path):
    """SRV-005: <script> 포함 md 가 이스케이프(&lt;script&gt;)되어 문자 그대로
    표시되고 실행 가능한 태그로 삽입되지 않는다."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        evil = dict(_FULL_REPORTS)
        evil["00_run_summary"] = ("# 요약\n<script>alert(1)</script>\n"
                                  '<img src=x onerror="alert(2)">\n')
        _plant_job(tmp_path / "data", a, b, "done", reports=evil)
        page = c.get(f"/compare/{a}__{b}/00_run_summary")
        assert page.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
        assert "<script>alert(1)</script>" not in page.text
        assert "<img src=x" not in page.text
        # 정상 md 열람도 확인
        assert c.get(f"/compare/{a}__{b}/03_analysis_result").status_code == 200


def test_report_view_name_whitelist(make_client, tmp_path):
    """name 은 00~03 고정 화이트리스트 — 그 외·미존재 파일은 404."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        d = _plant_job(tmp_path / "data", a, b, "done", reports=_FULL_REPORTS)
        (d / "job.json.bak").write_text("secret", encoding="utf-8")
        for bad in ("job", "job.json", "00_run_summary.md", "04_extra",
                    "..%2Fjob", "env-dump"):
            r = c.get(f"/compare/{a}__{b}/{bad}")
            assert r.status_code == 404, bad
            assert "secret" not in r.text
        # 화이트리스트 이름이라도 파일이 없으면 404
        (d / "01_metric_diff.md").unlink()
        assert c.get(f"/compare/{a}__{b}/01_metric_diff").status_code == 404


def test_compare_pages_reject_invalid_pair(make_client):
    """pair 형식 불일치·비실존 run → 404 (TASK-003 헬퍼 + split_pair)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a = _submit(c, "v1")
        for bad in ("evil__x", f"{a}", "..__..", f"{a}__{a}__x",
                    f"{a}__r20200101-000000-abcdef"):  # 마지막: 형식 합격·비실존
            assert c.get(f"/compare/{bad}").status_code == 404, bad
            assert c.get(f"/compare/{bad}/00_run_summary").status_code == 404, bad


def test_leaderboard_has_report_button(make_client):
    """리더보드: 두 run 선택 → 보고서 생성 버튼 + API 호출 스크립트 존재."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        _submit(c, "v1")
        page = c.get("/d/http_ds")
        assert page.status_code == 200
        assert "보고서 생성" in page.text
        assert "/api/compare-reports" in page.text
        assert "비교(diff)" in page.text     # 기존 diff 버튼 유지


def test_diff_page_links_report_with_status(make_client, tmp_path):
    """/diff 상단에 보고서 링크(있으면 상태 표시) — 기계적 diff 출력은 무수정
    (기존 diff 상세 회귀는 test_server_diff_and_delete.py 가 커버)."""
    with make_client(_OK_BODY) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        page = c.get(f"/diff?a={a}&b={b}")
        assert page.status_code == 200
        assert f"/compare/{a}__{b}" in page.text     # job 없어도 링크는 존재
        _plant_job(tmp_path / "data", a, b, "done", reports=_FULL_REPORTS)
        page = c.get(f"/diff?a={a}&b={b}")
        assert "(done)" in page.text                  # 상태 표시


def test_compare_view_works_when_generation_disabled(make_client, tmp_path):
    """비활성 서버(503)에서도 영속된 보고서 열람은 정상 — 생성만 막힌다."""
    with make_client(agents_src=False) as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        _plant_job(tmp_path / "data", a, b, "done", reports=_FULL_REPORTS)
        assert c.get(f"/compare/{a}__{b}").status_code == 200
        assert c.get(f"/compare/{a}__{b}/00_run_summary").status_code == 200
        r = c.post("/api/compare-reports", json={"a": a, "b": b}, headers=TOKEN)
        assert r.status_code == 503


def test_reconcile_function_direct(tmp_path):
    """reconcile 함수 직접 호출 (checklist verification) — 앱 기동 없이 단위 검증."""
    from server.compare import reconcile_compare_jobs

    root = tmp_path / "data"
    pair = "r20260101-000000-abcdef__r20260101-000001-abcdef"
    d = root / "compare_reports" / pair
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
    # _agents 디렉터리는 스캔 제외
    (root / "compare_reports" / "_agents").mkdir()

    fixed = reconcile_compare_jobs(root)
    assert fixed == [pair]
    job = json.loads((d / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    # 재실행은 멱등 — 이미 failed 인 job 은 건드리지 않음
    assert reconcile_compare_jobs(root) == []
