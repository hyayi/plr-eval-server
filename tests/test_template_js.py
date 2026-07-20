"""TASK-006 (REQ-004/007, SRV-008·009·012) — 템플릿 JS 공용 헬퍼·폴링 검증.

브라우저 e2e 없이 검증한다 (run-001 SRV-007 backlog 유지):
  1. base.html 의 mutateFetch 헬퍼 <script> 소스를 추출해 node 서브프로세스로
     실행 — fetch/prompt/alert 전역 목킹으로 성공/401 재시도/401 캡/403/503/
     기타 오류/fetch 예외/진행 중 disabled 각 경로를 단언 (run-001 TASK-001
     comparator node 검증 선례의 확장). node 부재 시 skip + 사유 표기.
  2. 렌더된 leaderboard/compare 페이지에 무조건 prompt 패턴이 남아 있지 않고
     헬퍼 배선·폴링 try/catch·중단 처리가 존재함을 단언.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# make_client 는 fixture — import 로 이 모듈에도 등록된다 (기존 헬퍼 재사용,
# 검토 비차단 관찰: TASK-006 테스트는 신규 파일로 분리해 TASK-007 과 겹침 회피).
from tests.test_compare_reports import (  # noqa: E402, F401
    _plant_job,
    _register_dataset,
    _submit,
    make_client,
)

_BASE_HTML = _ROOT / "server" / "templates" / "base.html"


def _base_script_src() -> str:
    """base.html 의 공용 <script> 블록(sortable + mutateFetch) 추출.
    이 블록은 Jinja 문법이 없어 원문 그대로 node 에서 실행 가능하다."""
    blocks = re.findall(r"<script>(.*?)</script>", _BASE_HTML.read_text(encoding="utf-8"),
                        flags=re.S)
    src = "\n".join(blocks)
    assert "mutateFetch" in src, "base.html 에 mutateFetch 헬퍼가 없음"
    return src


# node 하니스 — 헬퍼 소스 뒤에 붙여 실행. fetch/prompt/alert 를 전역으로 목킹해
# 각 경로를 실행하고 결과를 JSON 한 줄로 출력한다.
_HARNESS = r"""
function mkBtn(){return {disabled:false,textContent:'생성'};}
function res(status){return {status:status,ok:status>=200&&status<300};}
const results={};
async function main(){
  // 1. 성공(2xx): prompt 미호출, 요청 중 disabled+'요청 중…', ok 후 복원 생략
  {
    let promptCalls=0,duringDisabled=null,duringLabel=null;
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'x';};
    globalThis.alert=()=>{};
    globalThis.fetch=async()=>{duringDisabled=btn.disabled;duringLabel=btn.textContent;return res(202);};
    const r=await mutateFetch(btn,'/u',{method:'POST',headers:{'Content-Type':'application/json'}});
    results.success={status:r&&r.status,promptCalls,duringDisabled,duringLabel,afterDisabled:btn.disabled};
  }
  // 2. 401 → prompt 1회 → 토큰 헤더로 재시도 성공
  {
    let promptCalls=0;const headersSeen=[];
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'tok123';};
    globalThis.alert=()=>{};
    let call=0;
    globalThis.fetch=async(url,opts)=>{headersSeen.push(opts.headers);return res(++call===1?401:202);};
    const r=await mutateFetch(btn,'/u',{method:'POST',headers:{'Content-Type':'application/json'}});
    results.retry401={status:r&&r.status,promptCalls,
      firstHasToken:Object.prototype.hasOwnProperty.call(headersSeen[0]||{},'X-Auth-Token'),
      secondToken:(headersSeen[1]||{})['X-Auth-Token'],
      secondKeepsCT:(headersSeen[1]||{})['Content-Type'],
      afterDisabled:btn.disabled};
  }
  // 3. 401 → prompt → 재차 401: 오류 alert 로 종결 (재질문 1회 캡), 버튼 복원
  {
    let promptCalls=0;const alerts=[];
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'bad';};
    globalThis.alert=m=>alerts.push(String(m));
    globalThis.fetch=async()=>res(401);
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.cap401={ret:r,promptCalls,alerts,afterDisabled:btn.disabled,afterLabel:btn.textContent};
  }
  // 4. 403 도 401 과 동일 취급 (방어적) — prompt 후 재시도 성공
  {
    let promptCalls=0;
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'tok';};
    globalThis.alert=()=>{};
    let call=0;
    globalThis.fetch=async()=>res(++call===1?403:200);
    const r=await mutateFetch(btn,'/u',{method:'DELETE'});
    results.forbidden403={status:r&&r.status,promptCalls};
  }
  // 5. 503: 그대로 반환 (호출부 분기 몫), prompt 미호출, 버튼 복원
  {
    let promptCalls=0;
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'x';};
    globalThis.alert=()=>{};
    globalThis.fetch=async()=>res(503);
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.unavailable503={status:r&&r.status,promptCalls,afterDisabled:btn.disabled,afterLabel:btn.textContent};
  }
  // 6. 기타 HTTP 오류(400): 그대로 반환, 버튼 복원
  {
    const btn=mkBtn();
    globalThis.prompt=()=>'x';
    globalThis.alert=()=>{};
    globalThis.fetch=async()=>res(400);
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.badRequest400={status:r&&r.status,afterDisabled:btn.disabled};
  }
  // 7. fetch 예외(서버 다운): 원인 alert + null + 버튼 복원 — 무반응 금지
  {
    const alerts=[];let promptCalls=0;
    const btn=mkBtn();
    globalThis.prompt=()=>{promptCalls++;return 'x';};
    globalThis.alert=m=>alerts.push(String(m));
    globalThis.fetch=async()=>{throw new TypeError('Failed to fetch');};
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.fetchReject={ret:r,alerts,promptCalls,afterDisabled:btn.disabled,afterLabel:btn.textContent};
  }
  // 8. prompt 취소(null): 조용히 종료 — fetch 1회만, 버튼 복원
  {
    let fetchCalls=0;const alerts=[];
    const btn=mkBtn();
    globalThis.prompt=()=>null;
    globalThis.alert=m=>alerts.push(String(m));
    globalThis.fetch=async()=>{fetchCalls++;return res(401);};
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.promptCancel={ret:r,fetchCalls,alerts,afterDisabled:btn.disabled};
  }
  // 9. 401 → prompt → 재시도 fetch 예외: 재시도도 try/catch — alert + null + 복원
  {
    const alerts=[];
    const btn=mkBtn();
    globalThis.prompt=()=>'tok';
    globalThis.alert=m=>alerts.push(String(m));
    let call=0;
    globalThis.fetch=async()=>{if(++call===1)return res(401);throw new TypeError('down');};
    const r=await mutateFetch(btn,'/u',{method:'POST'});
    results.retryReject={ret:r,alerts,afterDisabled:btn.disabled};
  }
  console.log(JSON.stringify(results));
}
main().catch(e=>{console.error(e);process.exit(1);});
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 없음 — mutateFetch 경로 검증 생략 (checklist: skip + 사유 표기)")
def test_mutate_fetch_all_paths_node(tmp_path):
    """헬퍼 8+1 경로 단언 — 성공/401 재시도 성공/401 캡/403/503/기타 오류/
    fetch 예외/취소/재시도 예외 (반복 실행 가능 — USER-REQ-004 합격 조건)."""
    harness = tmp_path / "mutate_fetch_harness.js"
    harness.write_text(_base_script_src() + _HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(harness)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout.strip().splitlines()[-1])

    # 1. 성공: prompt 미호출, 진행 중 비활성+표기, ok 후 복원 생략(이동/reload 몫)
    assert r["success"]["status"] == 202
    assert r["success"]["promptCalls"] == 0
    assert r["success"]["duringDisabled"] is True
    assert r["success"]["duringLabel"] == "요청 중…"
    assert r["success"]["afterDisabled"] is True

    # 2. 401 → prompt 1회 → 토큰 재시도 성공 (1차는 토큰 헤더 없음 — 지연 질의)
    assert r["retry401"]["status"] == 202
    assert r["retry401"]["promptCalls"] == 1
    assert r["retry401"]["firstHasToken"] is False
    assert r["retry401"]["secondToken"] == "tok123"
    assert r["retry401"]["secondKeepsCT"] == "application/json"

    # 3. 재차 401 → 오류 alert 종결, prompt 정확히 1회, 버튼 복원
    assert r["cap401"]["ret"] is None
    assert r["cap401"]["promptCalls"] == 1
    assert any("토큰이 올바르지 않습니다" in a for a in r["cap401"]["alerts"])
    assert r["cap401"]["afterDisabled"] is False
    assert r["cap401"]["afterLabel"] == "생성"

    # 4. 403 도 동일 취급
    assert r["forbidden403"]["status"] == 200
    assert r["forbidden403"]["promptCalls"] == 1

    # 5. 503 그대로 반환 + 복원 (호출부가 비활성 안내)
    assert r["unavailable503"]["status"] == 503
    assert r["unavailable503"]["promptCalls"] == 0
    assert r["unavailable503"]["afterDisabled"] is False
    assert r["unavailable503"]["afterLabel"] == "생성"

    # 6. 기타 오류 그대로 반환 + 복원
    assert r["badRequest400"]["status"] == 400
    assert r["badRequest400"]["afterDisabled"] is False

    # 7. fetch 예외: 원인 alert + null + 복원 (SRV-008 무반응 금지)
    assert r["fetchReject"]["ret"] is None
    assert any("서버에 연결할 수 없습니다" in a for a in r["fetchReject"]["alerts"])
    assert r["fetchReject"]["promptCalls"] == 0
    assert r["fetchReject"]["afterDisabled"] is False
    assert r["fetchReject"]["afterLabel"] == "생성"

    # 8. prompt 취소: 추가 요청 없이 조용히 종료 + 복원
    assert r["promptCancel"]["ret"] is None
    assert r["promptCancel"]["fetchCalls"] == 1
    assert r["promptCancel"]["alerts"] == []
    assert r["promptCancel"]["afterDisabled"] is False

    # 9. 재시도 fetch 예외도 try/catch — alert + null + 복원
    assert r["retryReject"]["ret"] is None
    assert any("서버에 연결할 수 없습니다" in a for a in r["retryReject"]["alerts"])
    assert r["retryReject"]["afterDisabled"] is False


# =====================================================================
# 렌더 단언 — 무조건 prompt 패턴 부재 + 헬퍼 배선 + 폴링 정비
# =====================================================================

_UNCONDITIONAL_PROMPT = "prompt('X-Auth-Token')||''"


def test_leaderboard_uses_helper_without_unconditional_prompt(make_client):
    """리더보드: 무조건 prompt 제거, createReport/deleteRun 이 mutateFetch 경유,
    기존 gotoDiff·sortable 배선 유지."""
    with make_client() as c:
        _register_dataset(c)
        _submit(c, "v1")
        page = c.get("/d/http_ds").text
        assert _UNCONDITIONAL_PROMPT not in page
        assert "mutateFetch(" in page                    # base.html 헬퍼 정의 포함
        assert "createReport(this)" in page              # 버튼 요소 전달 배선
        assert "deleteRun('" in page and ", this)" in page
        assert "mutateFetch(btn,'/api/compare-reports'" in page
        assert "mutateFetch(btn,'/api/runs/'+runId" in page
        assert "confirm(" in page                        # 삭제 확인 유지
        assert "두 run을 선택하세요" in page             # 사전 검사 유지
        assert "gotoDiff()" in page and "sortable('lb')" in page


def test_compare_page_uses_helper_without_unconditional_prompt(make_client):
    """compare 페이지(job 없음): 무조건 prompt 제거 + 헬퍼 배선 + 버튼 전달."""
    with make_client() as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        page = c.get(f"/compare/{a}__{b}").text
        assert _UNCONDITIONAL_PROMPT not in page
        assert "mutateFetch(" in page
        assert "createReport(0, this)" in page
        assert "503" in page                             # 비활성 안내 분기 유지


def test_compare_polling_has_exception_and_stop_handling(make_client, tmp_path):
    """SRV-012: queued/running 폴링 블록에 try/catch·404 reload·연속 실패 중단
    안내가 존재한다 (영구 무음 폴링 제거)."""
    with make_client() as c:
        _register_dataset(c)
        a, b = _submit(c, "v1"), _submit(c, "v2")
        _plant_job(tmp_path / "data", a, b, "running")
        page = c.get(f"/compare/{a}__{b}").text
        assert "setInterval" in page                     # 폴링 자체 유지
        assert "try{" in page and "catch" in page        # 예외 수거
        assert "r.status===404" in page                  # 404 → reload 분기
        assert "clearInterval" in page                   # 연속 실패 중단
        assert 'id="poll-note"' in page                  # 중단 안내 표시 위치
        assert "자동 갱신 중단" in page
        assert _UNCONDITIONAL_PROMPT not in page
