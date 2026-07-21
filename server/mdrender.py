"""mdrender — 무의존성 안전 서브셋 markdown 렌더러 (SRV-014 / TASK-009).

compare 보고서 md 는 신뢰 불가 업로드 표면 텍스트에서 파생된다(SRV-005). raw
markdown/HTML 렌더는 저장형 XSS 로 이어질 수 있어 run-001 이 의도적으로 막았다.
이 모듈은 외부 의존성 없이(§12 dep 승인 불요) 보고서 서브셋만 안전하게 렌더한다.

불변식 (closed by construction — 이 세 규칙이 SRV-005 차단을 보장한다):
  1) 입력의 모든 문자는 출력에 앞서 반드시 HTML escape 를 거친다
     (markupsafe.escape — jinja2 기존 의존성 재사용, 신규 의존성 아님).
     블록/인라인 파서는 escape 완료 텍스트 위에서만 동작하므로 `<`,`>`,`&`,
     `"`,`'` 가 재해석되지 않는다.
  2) 출력에 나타나는 태그는 이 렌더러가 스스로 방출하는 화이트리스트 리터럴
     (h1~h4, table/thead/tbody/tr/th/td, ul/ol/li, pre/code, strong/em, p, br)
     뿐이며, 입력에서 유래한 태그·속성·URL 은 하나도 방출하지 않는다.
  3) 우리가 붙이는 고정 리터럴 외 어떤 속성도 입력에서 취하지 않는다(속성 미방출).

결과적으로 script/on*=/javascript:/data: URL 은 결과 HTML 에 구조상 나타날 수
없다. 링크·이미지·원시 HTML 은 지원하지 않으며(보고서 미사용 — server-analysis
§2.1 실측: 링크 0, 원시 HTML 0), 미지원 문법은 escape 텍스트로 안전 열화한다.
HTML 주석(`<!-- -->`)은 표시에서 제거한다.
"""
from __future__ import annotations

import re

import markupsafe
from markupsafe import Markup

# --- 블록 인식용 정규식 (raw 줄 위에서만 구조 판별; 내용은 이후 escape) ---
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SEP_CELL_RE = re.compile(r":?-+:?")

# --- 인라인 정규식 (escape 완료 텍스트 위에서만 적용) ---
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# 기울임: 단어 내부(예: 파일명 00_run_summary, plr_core.py)를 훼손하지 않도록
# 구분자가 단어 경계에 있을 때만 매치한다(GFM intra-word `_` 강조 억제 규칙).
_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)")
_ITALIC_US_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")

# 인라인 코드 보호용 sentinel — 널바이트(\x00). _strip_controls 가 입력의 모든
# 제어문자(널 포함)를 제거하므로 escape 완료 텍스트에는 절대 나타나지 않는다
# → placeholder 위조/충돌이 구조상 불가하다.
_STASH = "\x00"
_STASH_RE = re.compile(_STASH + r"(\d+)" + _STASH)


def _strip_controls(text: str) -> str:
    """제어 문자(널바이트 포함) 제거 — \n, \t 만 허용. 널바이트 제거는 sentinel
    위조 차단과 벡터 7(널바이트 우회) 대응을 겸한다."""
    return "".join(
        ch for ch in text
        if ch in ("\n", "\t") or ord(ch) >= 0x20
    )


def _esc(text: str) -> str:
    """모든 문자를 HTML escape (불변식 1). markupsafe.escape 는 & < > \" ' 를
    엔티티로 바꾼다 — 입력에서 유래한 어떤 문자도 태그 경계를 형성할 수 없다."""
    return str(markupsafe.escape(text))


def _render_inline(escaped: str) -> str:
    """escape 완료 텍스트 위에서 강조/인라인코드만 적용. 새 태그 경계를 만드는
    문자(`<`,`>`)는 이미 엔티티라 여기서 생성되는 태그는 리터럴 화이트리스트뿐."""
    stash: list[str] = []

    def _hold(m: re.Match) -> str:
        stash.append("<code>" + m.group(1) + "</code>")
        return f"{_STASH}{len(stash) - 1}{_STASH}"

    s = _CODE_RE.sub(_hold, escaped)
    s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _ITALIC_STAR_RE.sub(r"<em>\1</em>", s)
    s = _ITALIC_US_RE.sub(r"<em>\1</em>", s)
    s = _STASH_RE.sub(lambda m: stash[int(m.group(1))], s)
    return s


def _split_row(line: str) -> list[str]:
    """GFM 파이프 행을 셀 문자열 리스트로 분해(양끝 `|` 제거). 셀 내용은 아직
    escape 하지 않은 raw — 호출부가 _cell 로 escape+인라인 처리한다."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _is_separator(line: str) -> bool:
    """GFM 표 구분행(`|---|:--:|`) 판별 — 모든 셀이 `:?-+:?`."""
    cells = [c.strip() for c in _split_row(line)]
    if not cells or any(c == "" for c in cells):
        return False
    return all(_SEP_CELL_RE.fullmatch(c) for c in cells)


def _cell(raw: str) -> str:
    return _render_inline(_esc(raw.strip()))


def _consume_fence(lines: list[str], i: int) -> tuple[int, str]:
    """펜스 코드블록 — 언어 힌트는 무시. 내용은 escape 만(인라인 미적용) 하여
    <pre><code> 에 담는다. 닫는 펜스가 없으면 파일 끝까지. 코드 내부 탈출/속성
    주입은 전량 escape 로 무력화(벡터 6)."""
    i += 1  # 여는 ``` 줄 건너뜀
    buf: list[str] = []
    while i < len(lines) and not lines[i].strip().startswith("```"):
        buf.append(lines[i])
        i += 1
    if i < len(lines):
        i += 1  # 닫는 ``` 줄 건너뜀
    content = _esc("\n".join(buf))
    return i, f"<pre><code>{content}</code></pre>"


def _consume_table(lines: list[str], i: int) -> tuple[int, str]:
    header = _split_row(lines[i])
    i += 2  # 헤더 + 구분행 건너뜀
    rows: list[list[str]] = []
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(_split_row(lines[i]))
        i += 1
    ths = "".join(f"<th>{_cell(c)}</th>" for c in header)
    thead = f"<thead><tr>{ths}</tr></thead>"
    trs = "".join(
        "<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return i, f"<table>{thead}<tbody>{trs}</tbody></table>"


def _consume_list(lines: list[str], i: int, regex: re.Pattern[str],
                  tag: str) -> tuple[int, str]:
    items: list[str] = []
    while i < len(lines):
        m = regex.match(lines[i])
        if not m:
            break
        items.append(_render_inline(_esc(m.group(1).strip())))
        i += 1
    lis = "".join(f"<li>{it}</li>" for it in items)
    return i, f"<{tag}>{lis}</{tag}>"


def _starts_block(line: str, lines: list[str], i: int) -> bool:
    stripped = line.strip()
    if (stripped.startswith("```") or _HEADER_RE.match(line)
            or _UL_RE.match(line) or _OL_RE.match(line)):
        return True
    return "|" in line and i + 1 < len(lines) and _is_separator(lines[i + 1])


def _consume_paragraph(lines: list[str], i: int) -> tuple[int, str]:
    buf: list[str] = []
    while i < len(lines):
        line = lines[i]
        if not line.strip() or (buf and _starts_block(line, lines, i)):
            break
        buf.append(_render_inline(_esc(line.strip())))
        i += 1
    return i, "<p>" + "<br>".join(buf) + "</p>"


def render_markdown_safe(text: str) -> Markup:
    """보고서 md 를 안전 서브셋 HTML 로 렌더한다. 반환은 markupsafe.Markup —
    렌더러 불변식(모듈 docstring)이 안전을 보장하는 화이트리스트 HTML 이므로
    템플릿에서 |safe(또는 Markup 자동 신뢰)로 표시해도 된다. 이 함수 밖의 원시
    md 에는 절대 |safe 를 적용하지 말 것."""
    text = _strip_controls(text)
    text = _COMMENT_RE.sub("", text)          # HTML 주석 표시 제거(벡터 4)
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.strip().startswith("```"):
            i, block = _consume_fence(lines, i)
        elif (m := _HEADER_RE.match(line)):
            level = min(len(m.group(1)), 4)   # 화이트리스트 h1~h4 로 클램프
            content = _render_inline(_esc(m.group(2).strip()))
            i, block = i + 1, f"<h{level}>{content}</h{level}>"
        elif "|" in line and i + 1 < n and _is_separator(lines[i + 1]):
            i, block = _consume_table(lines, i)
        elif _UL_RE.match(line):
            i, block = _consume_list(lines, i, _UL_RE, "ul")
        elif _OL_RE.match(line):
            i, block = _consume_list(lines, i, _OL_RE, "ol")
        else:
            i, block = _consume_paragraph(lines, i)
        out.append(block)
    return Markup("\n".join(out))
