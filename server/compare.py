"""compare — REQ-002 compare-runs 보고서 생성 백엔드 (TASK-004).

두 run을 골라 claude CLI(/compare-runs 스킬)를 백그라운드 subprocess로 실행하고,
보고서 4종(00~03.md)과 작업 상태(job.json)를 데이터 볼륨에 영속한다.

설계 (improvement-plan.md §3 — 계획 단계에서 확정):
  - 파일이 진실: <DATA_ROOT>/compare_reports/<a>__<b>/job.json 이 상태의 단일 원천.
    산출물 md도 같은 디렉터리(=WORKSPACE_DIR) — pod 재시작에도 데이터 볼륨에
    잔존한다 (SRV-004 (b),(c)). 기동 시 reconcile로 orphan queued/running →
    failed 정리, 부분 쓰기로 손상된 job.json도 failed로 정리(비차단 관찰 2).
    쓰기는 tmp+rename 원자적.
  - 비블로킹 (SRV-004 (a)): asyncio.create_subprocess_exec + create_task —
    workers=1 계약에서 이벤트 루프를 막지 않는다. 동시 실행 상한 1
    (asyncio.Semaphore), 동일 (a,b) dedupe, COMPARE_TIMEOUT_SEC 초과 시
    kill → failed. exit 0이어도 4종 md가 모두 존재·비공백일 때만 done
    (가짜 완료 방지).
  - SRV-005 통제: 에이전트 정의는 원본(COMPARE_AGENTS_SRC 체크아웃, 읽기 전용 —
    절대 수정하지 않음)을 기동 시 <DATA_ROOT>/compare_reports/_agents/.claude/
    **서버 관리 사본**으로 복사하며 frontmatter tools를 최소로 축소한다.
    실행은 셸 미경유 exec 배열 + TASK-003 검증을 통과한 run id 인자만 +
    최소 env 화이트리스트(EVAL_SERVER_TOKEN 등 서버 비밀 미전달) +
    --allowedTools 최소 목록.
  - claude CLI 부재/미설정(k8s 기본 배포): 기동 시 가용성 판정 → 기능 비활성.
    POST는 503 + 명시적 안내, 서버의 다른 모든 기능은 정상 (SRV-004).
  - URL 계약 (INT-002): 신규 라우트는 /api/compare-reports/* prefix만 사용 —
    /api/runs/ 바로 아래 단일 세그먼트 literal 금지(GET /api/runs/{run_id}
    포획 회피). GET /api/runs/{run_id} 응답·기존 엔드포인트는 일절 수정하지
    않는다. POST는 token_guard 자동 편입, 상태 조회 GET은 토큰 면제.
    SKILL.md Phase 0의 대화형 게이트(AskUserQuestion)는 서버 결정으로 대체 —
    queued/running·done은 기존 job 반환(dedupe), done은 force=1일 때만 재생성,
    failed는 재-POST로 재생성 허용(plan review 비차단 관찰 1).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 완료 판정 대상 산출물 (compare-runs 파이프라인 4종 — 확장자 제외 이름)
REPORT_NAMES = ("00_run_summary", "01_metric_diff", "02_prompt_diff",
                "03_analysis_result")

# 서버 관리 사본의 frontmatter tools 축소 목록 (SRV-005 통제 1).
# 원칙: 각 에이전트 정의가 실제로 요구하는 최소만.
#   - eval-metric-differ 만 Bash 유지 — evalkit 재사용 스크립트 실행에 필요.
#   - eval-run-fetcher 는 파일 읽기·집계 전용(자체 문서: "오직 파일만 읽는다")
#     이라 Bash 를 제거해도 동작 요구를 침해하지 않는다 ("최소로 축소" 지시).
#   - eval-prompt-differ 에서 Bash 제거(Read/Glob/Write), eval-why-analyst 는
#     Read/Write 만 (checklist TASK-004 implementation_steps 2).
_AGENT_TOOLS = {
    "eval-run-fetcher": "Read, Glob, Write",
    "eval-metric-differ": "Read, Write, Bash, Glob",  # 원본 유지 — evalkit 스크립트
    "eval-prompt-differ": "Read, Glob, Write",
    "eval-why-analyst": "Read, Write",
}

# CLI --allowedTools: 축소된 에이전트 tools 의 합집합 + 오케스트레이터의 Task.
_ALLOWED_TOOLS = "Task,Read,Glob,Write,Bash"

# 에이전트 프로세스에 패스스루하는 env 화이트리스트 — PATH·HOME·로케일·모델
# 자격증명만. EVAL_SERVER_TOKEN 등 서버 비밀은 절대 전달하지 않는다 (SRV-005).
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "LANG", "LC_ALL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
)

# 서버 관리 사본 SKILL.md 에 덧붙이는 headless 부기 — 원본 SKILL.md Phase 0의
# 대화형 게이트(AskUserQuestion)·자체 워크스페이스 산정을 서버 결정으로 대체.
# 원본 파일은 수정하지 않고 사본에만 추가한다.
_SKILL_HEADLESS_NOTE = """

<!-- 아래는 plr-eval-server 가 사본 설치 시 자동으로 덧붙인 부기다(원본 무수정). -->
## Server headless 모드 (plr-eval-server 자동 부기)

이 사본은 plr-eval-server 가 headless(-p)로 실행한다. 위 Phase 0 을 다음으로 대체한다:

- `DATA_ROOT` 는 env `EVAL_SERVER_DATA` 값을 그대로 사용한다 (서버가 주입).
- `WORKSPACE_DIR` 는 env `WORKSPACE_DIR` 값을 그대로 사용한다 — 서버가 이미
  생성·정리를 끝냈으므로 `_workspace/` 경로 산정·충돌 검사를 하지 않는다.
- **AskUserQuestion 등 대화형 질문을 일절 하지 않는다** — 덮어쓰기/재사용 결정은
  서버가 이미 내렸다(기존 job dedupe, force 시 디렉터리 재생성). 곧바로
  Phase 1~4 를 진행하고, 실패는 질문 없이 그대로 실패로 보고한다.
"""

# 모듈 상태 — init_compare()(lifespan startup)가 채운다. Semaphore 는 기동 시
# 이벤트 루프에서 생성해 TestClient 재기동(루프 교체)에도 안전하다.
_STATE: dict = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run_id_re() -> re.Pattern:
    """run id 형식 정규식 — TASK-003 헬퍼와 단일 원천 공유 (드리프트 방지)."""
    from server.web import _RUN_ID_RE
    return _RUN_ID_RE


def split_pair(pair: str) -> tuple[str, str]:
    """'<a>__<b>' 경로 세그먼트를 검증·분해. 형식 불일치는 404.

    형식 화이트리스트만으로 경로 구분자·'..'·절대경로가 원천 차단된다
    (경로 결합 전 검증 — SRV-003과 동일 원칙). 실존 run 여부는 묻지 않는다 —
    run이 나중에 삭제돼도 영속된 job/보고서 상태 조회는 가능해야 한다.
    """
    parts = pair.split("__")
    if len(parts) != 2 or not all(_run_id_re().fullmatch(p) for p in parts):
        raise HTTPException(404, f"compare report {pair!r} not found")
    return parts[0], parts[1]


def job_dir_of(root: Path, a: str, b: str) -> Path:
    return root / "compare_reports" / f"{a}__{b}"


def _write_job(job_dir: Path, job: dict) -> None:
    """job.json 원자적 쓰기 (tmp+rename) — 재시작 타이밍의 부분 쓰기 손상 방지."""
    tmp = job_dir / ".job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(job_dir / "job.json")


def _read_job(job_dir: Path) -> dict | None:
    from server.storage import read_json
    job = read_json(job_dir / "job.json")
    return job if isinstance(job, dict) else None


# =====================================================================
# 기동: 가용성 판정 · 에이전트 사본 설치 · reconcile
# =====================================================================

def _resolve_agents_src(src: str) -> Path | None:
    """COMPARE_AGENTS_SRC → 에이전트 정의 .claude 디렉터리. 체크아웃 루트/
    .claude 자체 어느 쪽을 가리켜도 수용. 4종 agent md + SKILL.md 가 모두
    있어야 유효(없으면 None → 기능 비활성)."""
    base = Path(src)
    for cand in (base / ".claude", base):
        if not (cand / "agents").is_dir():
            continue
        agents_ok = all((cand / "agents" / f"{n}.md").is_file() for n in _AGENT_TOOLS)
        skill_ok = (cand / "skills" / "compare-runs" / "SKILL.md").is_file()
        if agents_ok and skill_ok:
            return cand
    return None


def _install_agents_copy(claude_src: Path, root: Path) -> Path:
    """원본(읽기 전용)에서 서버 관리 사본을 설치하고 tools 를 축소한다.

    사본 위치: <DATA_ROOT>/compare_reports/_agents/.claude/ — 실행 cwd 는
    _agents/. 기동 때마다 재설치(서버가 사본의 단일 관리 주체 — 원본 갱신 반영).
    """
    agents_root = root / "compare_reports" / "_agents"
    dest = agents_root / ".claude"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "agents").mkdir(parents=True)
    for name, tools in _AGENT_TOOLS.items():
        text = (claude_src / "agents" / f"{name}.md").read_text(encoding="utf-8")
        # frontmatter 의 tools: 줄만 축소 목록으로 교체 (첫 1회 — frontmatter 전용)
        text = re.sub(r"(?m)^tools:.*$", f"tools: {tools}", text, count=1)
        (dest / "agents" / f"{name}.md").write_text(text, encoding="utf-8")
    skill_dest = dest / "skills" / "compare-runs"
    skill_dest.mkdir(parents=True)
    skill_text = (claude_src / "skills" / "compare-runs" / "SKILL.md").read_text(
        encoding="utf-8")
    (skill_dest / "SKILL.md").write_text(skill_text + _SKILL_HEADLESS_NOTE,
                                         encoding="utf-8")
    return agents_root


def reconcile_compare_jobs(root: Path) -> list[str]:
    """기동 reconcile (SRV-004 (b)) — reconcile_and_rebuild 패턴을 따른다.

    compare_reports/*/job.json 을 스캔해 (1) orphan queued/running(실행 주체가
    죽은 재시작 잔재) → failed, (2) 파싱 불가(부분 쓰기 손상) job.json →
    failed 로 정리한다. 정리한 pair 이름 목록 반환."""
    base = root / "compare_reports"
    fixed: list[str] = []
    if not base.is_dir():
        return fixed
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name == "_agents":
            continue
        jf = d / "job.json"
        if not jf.is_file():
            continue
        job = _read_job(d)
        if job is None:
            # 손상 job.json — 디렉터리 이름에서 run id 복원 시도 후 failed 기록
            parts = d.name.split("__")
            run_a, run_b = (parts + [None, None])[:2] if len(parts) == 2 else (None, None)
            _write_job(d, {
                "run_a": run_a, "run_b": run_b, "dataset": None,
                "status": "failed", "created_at": None, "started_at": None,
                "finished_at": _now(),
                "error": "job.json 손상(비원자적 쓰기 잔재) — 기동 시 failed 처리",
                "stderr_tail": None,
            })
            fixed.append(d.name)
        elif job.get("status") in ("queued", "running"):
            job["status"] = "failed"
            job["error"] = "서버 재시작으로 중단"
            job["finished_at"] = _now()
            _write_job(d, job)
            fixed.append(d.name)
    return fixed


def init_compare(root: Path) -> dict:
    """startup(lifespan)에서 호출 — 가용성 판정·사본 설치·reconcile.

    실패해도 서버 기동을 막지 않는다: 사유를 reasons 에 남기고 기능만 비활성.
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    src = os.environ.get("COMPARE_AGENTS_SRC", "")
    pythonpath = os.environ.get("COMPARE_PYTHONPATH", str(_REPO_ROOT))
    reasons: list[str] = []

    try:
        timeout = float(os.environ.get("COMPARE_TIMEOUT_SEC", "1800"))
    except ValueError:
        timeout = 1800.0
        reasons.append("COMPARE_TIMEOUT_SEC 값이 숫자가 아님")

    if shutil.which(claude_bin) is None:
        reasons.append(f"claude CLI 없음 (CLAUDE_BIN={claude_bin!r})")
    agents_src: Path | None = None
    if not src:
        reasons.append("COMPARE_AGENTS_SRC 미설정 (에이전트 정의 원본 경로)")
    else:
        agents_src = _resolve_agents_src(src)
        if agents_src is None:
            reasons.append(f"COMPARE_AGENTS_SRC 에 compare-runs 에이전트 정의 없음: {src}")

    enabled = not reasons
    if enabled and agents_src is not None:
        try:
            _install_agents_copy(agents_src, root)
        except OSError as exc:
            enabled = False
            reasons.append(f"에이전트 사본 설치 실패: {exc}")

    reconciled = reconcile_compare_jobs(root)

    _STATE.clear()
    _STATE.update({
        "root": root,
        "enabled": enabled,
        "reasons": reasons,
        "claude_bin": claude_bin,
        "timeout": timeout,
        "pythonpath": pythonpath,
        "sem": asyncio.Semaphore(1),   # 동시 실행 상한 1 (SRV-004 (a))
        "tasks": set(),                # 강한 참조 유지 (GC 로 인한 task 소실 방지)
        "proc": None,                  # 실행 중 subprocess (shutdown 시 kill)
    })
    return {"enabled": enabled, "reasons": reasons, "reconciled": reconciled}


def _kill_proc_tree(proc) -> None:
    """에이전트 프로세스 트리 전체 kill — start_new_session=True 로 띄웠으므로
    pgid == proc.pid. CLI 가 하위 프로세스를 남기면 그것들이 stdout/stderr
    파이프를 계속 쥐어 proc.wait() 가 영원히 안 끝난다(파이프 EOF 대기) —
    직계만 kill 하면 timeout 후에도 job task 가 매달리는 실측 확인된 함정."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()  # 프로세스 그룹이 이미 사라짐 등 — 직계라도 kill


def shutdown_compare() -> None:
    """lifespan 종료 시 호출 — 실행 task 취소·프로세스 트리 kill.
    상태 정리는 다음 기동의 reconcile 이 orphan → failed 로 수행한다."""
    for task in list(_STATE.get("tasks", ())):
        task.cancel()
    proc = _STATE.get("proc")
    if proc is not None and proc.returncode is None:
        _kill_proc_tree(proc)


# =====================================================================
# 실행
# =====================================================================

def _agent_env(root: Path, job_dir: Path) -> dict[str, str]:
    """에이전트 프로세스 env — 화이트리스트 패스스루 + 실행 컨텍스트 3종.
    PYTHONPATH 는 evalkit import(metric-differ) 충족 (SRV-004 (d))."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["EVAL_SERVER_DATA"] = str(root)
    env["WORKSPACE_DIR"] = str(job_dir)
    env["PYTHONPATH"] = _STATE["pythonpath"]
    return env


async def _run_job(job_dir: Path) -> None:
    """queued job 실행 — Semaphore(1) 아래 subprocess 실행 후 상태 기록.
    모든 실패 경로는 job.json 의 status=failed + error 로 표면화한다
    (예외 은폐 금지 — 파일이 관측 지점)."""
    async with _STATE["sem"]:
        job = _read_job(job_dir)
        if not job or job.get("status") != "queued":
            return  # 대기 중 삭제/재생성 경합 — 현재 파일 상태를 존중
        job.update(status="running", started_at=_now())
        _write_job(job_dir, job)

        cmd = [
            _STATE["claude_bin"], "-p",
            f"/compare-runs {job['run_a']} {job['run_b']}",
            "--output-format", "json",
            "--allowedTools", _ALLOWED_TOOLS,
        ]
        agents_cwd = _STATE["root"] / "compare_reports" / "_agents"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(agents_cwd),
                env=_agent_env(_STATE["root"], job_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # 전용 세션/프로세스 그룹 — 트리 단위 kill 용
            )
        except OSError as exc:
            job.update(status="failed", finished_at=_now(),
                       error=f"claude CLI 실행 불가: {exc}")
            _write_job(job_dir, job)
            return

        _STATE["proc"] = proc
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_STATE["timeout"])
        except (asyncio.TimeoutError, TimeoutError):
            _kill_proc_tree(proc)
            await proc.wait()
            job.update(status="failed", finished_at=_now(),
                       error=f"timeout: {_STATE['timeout']:g}s 초과 — 프로세스 kill")
            _write_job(job_dir, job)
            return
        except asyncio.CancelledError:
            _kill_proc_tree(proc)  # 서버 종료 — 다음 기동 reconcile 이 failed 로 정리
            raise
        finally:
            _STATE["proc"] = None

        stderr_tail = stderr.decode("utf-8", errors="replace")[-4096:] or None
        # 가짜 완료 방지: exit 0 이어도 4종 md 가 모두 존재·비공백일 때만 done.
        missing = [f"{n}.md" for n in REPORT_NAMES
                   if not (job_dir / f"{n}.md").is_file()
                   or (job_dir / f"{n}.md").stat().st_size == 0]
        if proc.returncode != 0:
            job.update(status="failed",
                       error=f"claude 비정상 종료 (exit code {proc.returncode})")
        elif missing:
            job.update(status="failed",
                       error="산출물 미완성: " + ", ".join(missing) + " 없음/공백")
        else:
            job.update(status="done", error=None)
        job.update(finished_at=_now(), stderr_tail=stderr_tail)
        _write_job(job_dir, job)


# =====================================================================
# API — /api/compare-reports/* prefix 전용 (INT-002)
# =====================================================================

class CompareRequest(BaseModel):
    a: str
    b: str


@router.post("/api/compare-reports")
async def create_compare_report(req: CompareRequest, force: int = 0):
    """보고서 생성 job 시작 (비동기). token_guard(변이 메서드) 자동 적용.

    응답: 202 = 새 job 생성(queued), 200 = 기존 job 반환(dedupe/완료 재사용).
    비활성 서버는 503 + 원인 안내. run id 불합격은 404 (TASK-003 헬퍼)."""
    if not _STATE.get("enabled"):
        reasons = _STATE.get("reasons") or ["compare 기능이 초기화되지 않음"]
        raise HTTPException(503, {
            "error": "compare-reports 기능이 이 서버에서 비활성화되어 있습니다",
            "reasons": reasons,
            "hint": "claude CLI 설치 + COMPARE_AGENTS_SRC(에이전트 정의 체크아웃 경로) "
                    "설정 후 재기동하세요. k8s/eval-server.example.yaml 및 README 의 "
                    "compare-reports 요구사항 참조.",
        })

    from server.web import require_valid_run_id  # TASK-003 단일 검증 지점 재사용
    dir_a = require_valid_run_id(req.a)
    dir_b = require_valid_run_id(req.b)
    if req.a == req.b:
        raise HTTPException(400, "같은 run 끼리는 비교할 수 없습니다 (a == b)")

    from server.storage import read_json
    ds_a = (read_json(dir_a / "meta.json") or {}).get("dataset")
    ds_b = (read_json(dir_b / "meta.json") or {}).get("dataset")
    if not ds_a or ds_a != ds_b:
        raise HTTPException(400, f"두 run 의 데이터셋이 다릅니다: {ds_a!r} vs {ds_b!r}")

    root: Path = _STATE["root"]
    job_dir = job_dir_of(root, req.a, req.b)
    existing = _read_job(job_dir)
    if existing is not None:
        status = existing.get("status")
        if status in ("queued", "running"):
            return JSONResponse(existing, status_code=200)  # dedupe — 진행 중 재사용
        if status == "done" and not force:
            return JSONResponse(existing, status_code=200)  # 완료 재사용 (force=1 로 재생성)
        # failed → 재-POST 로 재생성 허용 / done+force=1 → 덮어쓰기 재생성
        shutil.rmtree(job_dir)

    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "run_a": req.a, "run_b": req.b, "dataset": ds_a,
        "status": "queued", "created_at": _now(),
        "started_at": None, "finished_at": None,
        "error": None, "stderr_tail": None,
    }
    _write_job(job_dir, job)

    task = asyncio.get_running_loop().create_task(_run_job(job_dir))
    _STATE["tasks"].add(task)
    task.add_done_callback(_STATE["tasks"].discard)
    return JSONResponse(job, status_code=202)


@router.get("/api/compare-reports/{pair}")
def compare_report_status(pair: str) -> dict:
    """job 상태 조회 — job.json 내용 그대로 반환(없으면 404).
    GET 이므로 token_guard 면제(기존 인증 계약과 일관 — INT-002)."""
    a, b = split_pair(pair)
    job = _read_job(job_dir_of(_STATE["root"], a, b))
    if job is None:
        raise HTTPException(404, f"compare report {pair!r} not found")
    return job
