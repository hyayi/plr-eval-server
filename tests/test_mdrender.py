"""TASK-009 (REQ-009 / SRV-014) — 무의존 안전 서브셋 markdown 렌더러 테스트.

server-analysis §3 위협모델 8벡터를 각각 개별 단언으로 차단(1~7)/보존(8)하고,
정상 문법(헤더·표·목록·코드블록·강조)의 긍정 렌더를 단언한다. 렌더러는
closed-by-construction — 입력 전량 escape 후 화이트리스트 태그만 방출하므로
script/이벤트핸들러/위험 URL(href)이 결과 HTML 에 구조상 나타날 수 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from server.mdrender import render_markdown_safe  # noqa: E402


def _r(md: str) -> str:
    return str(render_markdown_safe(md))


# =====================================================================
# 위협모델 8벡터 (server-analysis §3) — 각 벡터 개별 테스트
# =====================================================================

def test_vector1_raw_script_tag_escaped():
    """벡터1: 원시 <script> 는 escape 텍스트가 되고 실행 태그로 방출되지 않는다."""
    out = _r("# 요약\n<script>alert(1)</script>\n")
    assert "<script" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_vector2_img_onerror_not_emitted():
    """벡터2: <img onerror> 는 실제 img 태그로 방출되지 않는다 — 태그 경계
    전체가 escape 되어 이벤트 핸들러가 활성 속성이 될 수 없다(inert 텍스트)."""
    out = _r('본문 <img src=x onerror="alert(1)"> 끝\n')
    assert "<img" not in out                       # 실제 img 태그 미방출
    # 태그 전체가 escape 리터럴 — onerror 는 활성 속성이 아니라 텍스트다
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in out


def test_vector3_dangerous_url_never_becomes_link():
    """벡터3: javascript:/data: URL 은 링크 미지원이라 href 로 방출되지 않는다.
    (§3: '링크 미지원 → 리터럴' — 활성 URL/href 가 결과에 나타나지 않는다.)"""
    out = _r("[x](javascript:alert(1)) 그리고 [y](data:text/html,<script>)\n")
    assert "<a" not in out           # anchor 태그 미방출
    assert "href" not in out         # href 속성 미방출 → 위험 URL 비활성
    # 대괄호 텍스트는 escape 된 리터럴로만 남는다(링크 생성 안 됨)
    assert "[x](javascript:alert(1))" in out


def test_vector4_html_comment_removed_from_display():
    """벡터4: HTML 주석(실보고서 machine-readable 주석 포함)은 표시에서 제거된다."""
    out = _r("앞\n<!-- machine-readable: {\"k\": 1} -->\n뒤\n")
    assert "machine-readable" not in out
    assert "<!--" not in out and "&lt;!--" not in out


def test_vector5_html_in_table_cell_escaped():
    """벡터5: 표 셀 안 HTML 이 escape 되어 셀 렌더 결과에 <script 가 없다."""
    out = _r("| 이름 | 값 |\n|---|---|\n| <script>alert(1)</script> | ok |\n")
    assert "<table>" in out          # 표는 정상 렌더
    assert "<script" not in out
    assert "&lt;script&gt;" in out   # 셀 내용은 escape 리터럴


def test_vector6_code_fence_and_inline_escape_payload():
    """벡터6: 코드펜스/인라인코드 안 속성 주입이 <pre>/<code> 안에서 escape 된다."""
    fenced = _r("```json\n<img src=x onerror=alert(1)>\n```\n")
    assert "<pre><code>" in fenced
    assert "<img" not in fenced                     # 실제 태그 미방출
    assert "&lt;img src=x onerror=alert(1)&gt;" in fenced   # escape 리터럴

    inline = _r("실행 `<img onerror=x>` 예시\n")
    assert "<code>" in inline
    assert "<img" not in inline
    assert "<code>&lt;img onerror=x&gt;</code>" in inline


def test_vector6_code_fence_escape_attempt_stays_safe():
    """벡터6 보강: 코드펜스 탈출 시도(중간에 펜스 닫고 raw 태그 삽입)도 후속
    블록이 다시 전량 escape 되므로 raw 태그가 방출되지 않는다."""
    out = _r("```\ncode\n```\n<script>alert(1)</script>\n```\nmore\n```\n")
    assert "<script" not in out
    assert "&lt;script&gt;" in out


def test_vector7_encoding_bypass_stays_literal():
    """벡터7: 인코딩 우회(수치 엔티티·엔티티화 꺾쇠·널바이트)가 리터럴 escape 로
    표시되고 디코드된 태그/스킴이 방출되지 않는다."""
    out = _r("&#x6a;avascript &lt;script&gt;alert(1)&lt;/script&gt; null\x00byte\n")
    assert "<script" not in out
    # 앰퍼샌드가 재-escape 되어 엔티티가 브라우저에서 디코드되지 않는다
    assert "&amp;#x6a;avascript" in out
    assert "&amp;lt;script&amp;gt;" in out
    # 널바이트는 제거되고 태그 경계를 만들지 않는다
    assert "\x00" not in out
    assert "nullbyte" in out


def test_vector8_prose_angle_brackets_preserved():
    """벡터8(회귀): prose 꺾쇠 텍스트가 삭제되지 않고 &lt;...&gt; 리터럴로 보존된다."""
    out = _r("설정은 <version> 과 <id> 와 <PROMPT_VERSION_YAML_COT> 를 참조\n")
    assert "&lt;version&gt;" in out
    assert "&lt;id&gt;" in out
    assert "&lt;PROMPT_VERSION_YAML_COT&gt;" in out


# =====================================================================
# 출력 화이트리스트 불변식 — 어떤 입력에도 위험 구성이 나타날 수 없음
# =====================================================================

def test_no_dangerous_constructs_across_vectors():
    """혼합 페이로드에서 script/on*=/javascript: href/data: href 가 결과에 없다."""
    payload = (
        "# hi\n<script>x</script>\n<img onerror=y>\n"
        "[a](javascript:alert(1))\n<!-- c -->\n"
        "| <b onmouseover=z> | ok |\n|---|---|\n| <svg/onload=1> | v |\n"
    )
    out = _r(payload)
    # 입력에서 유래한 실제 위험 태그·앵커가 하나도 방출되지 않는다
    assert "<script" not in out
    assert "<img" not in out
    assert "<svg" not in out
    assert "<a" not in out and "href" not in out
    # 이벤트 핸들러는 escape 된 태그 리터럴 안에만 남아 활성 속성이 될 수 없다
    assert "&lt;img onerror=y&gt;" in out
    assert "&lt;b onmouseover=z&gt;" in out
    assert "&lt;svg/onload=1&gt;" in out


# =====================================================================
# 긍정 렌더 — 정상 문법이 화이트리스트 태그로 렌더된다 (USER-REQ-009)
# =====================================================================

def test_headers_render_h1_to_h4():
    out = _r("# A\n## B\n### C\n#### D\n")
    assert "<h1>A</h1>" in out
    assert "<h2>B</h2>" in out
    assert "<h3>C</h3>" in out
    assert "<h4>D</h4>" in out


def test_pipe_table_renders():
    out = _r("| 지표 | v1 | v2 |\n|---|---|---|\n| acc | 0.9 | 0.8 |\n")
    assert "<table>" in out and "</table>" in out
    assert "<thead>" in out and "<tbody>" in out
    assert "<th>지표</th>" in out
    assert "<td>acc</td>" in out


def test_unordered_and_ordered_lists_render():
    ul = _r("- 하나\n- 둘\n")
    assert ul.count("<li>") == 2 and "<ul>" in ul
    ol = _r("1. 첫째\n2. 둘째\n")
    assert ol.count("<li>") == 2 and "<ol>" in ol


def test_fenced_code_block_renders():
    out = _r('```json\n{"k": 1}\n```\n')
    assert "<pre><code>" in out and "</code></pre>" in out
    assert "&#34;k&#34;" in out or "\"k\"" in out or "&quot;k&quot;" in out


def test_inline_emphasis_and_code_render():
    out = _r("**굵게** 와 *기울임* 와 _밑줄기울임_ 와 `코드`\n")
    assert "<strong>굵게</strong>" in out
    assert "<em>기울임</em>" in out
    assert "<em>밑줄기울임</em>" in out
    assert "<code>코드</code>" in out


def test_underscore_in_identifier_not_italicized():
    """파일명/식별자의 단어 내부 밑줄은 기울임으로 오인되지 않는다(회귀)."""
    out = _r("보고서 00_run_summary 와 03_analysis_result 참조\n")
    assert "00_run_summary" in out
    assert "03_analysis_result" in out
    assert "<em>" not in out
