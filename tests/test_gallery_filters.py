"""TASK-011 (REQ-011/012, SRV-016~021) — 단일 속성 갤러리 필터 재구성 검증.

두 갈래로 검증한다(브라우저 e2e 없이, test_template_js.py node 선례 확장):
  1. (HTML 단언) build_gallery 산출 HTML 에 pred=<값> 버튼이 label 버튼과 대칭으로
     생기고, 기존 전체/오답만/정답만·label 버튼이 유지되며, 각 버튼이 축 식별자
     flt('status'|'label'|'pred',...) 로 호출되고, pred=None 카드가
     data-pred="unknown" 으로 정규화됨을 단언(SRV-016 / SRV-018).
  2. (node flt 단언) _JS_SINGLE 의 sel/flt/render/syncButtons 소스를 추출해 node
     서브프로세스로 실행 — 최소 DOM(카드 dataset/style, 버튼 dataset/classList)
     목킹 후 축 내 OR·축 간 AND·미선택 전체·토글·교차 오염 없음을 단언(SRV-017).
     node 부재 시 skip + 사유(run-001~003 선례).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _make_binary_ds(base: Path) -> Path:
    """이진(male/female) 단일 속성 데이터셋 + pred=None 케이스 1건.

    rows: (obj_id, label, pred)
      a female female  correct
      b female male     wrong  — 혼동 셀(label=female AND pred=male), 축 교차 오염 함정
      c male   male    correct
      d male   female  wrong
      e male   None    pred 미확정 → data-pred='unknown' 정규화 대상(SRV-018)
    """
    (base / "crops").mkdir(parents=True)
    rows = [
        ("a", "female", "female"),
        ("b", "female", "male"),
        ("c", "male", "male"),
        ("d", "male", "female"),
        ("e", "male", None),
    ]
    with open(base / "labels.jsonl", "w", encoding="utf-8") as f:
        for oid, lab, _p in rows:
            f.write(json.dumps({"obj_id": oid, "label": lab}) + "\n")
    with open(base / "predictions.jsonl", "w", encoding="utf-8") as f:
        for oid, _l, pred in rows:
            row: dict = {"obj_id": oid, "margin": 0.5, "quality": 0.5}
            if pred is not None:
                row["pred"] = pred
            f.write(json.dumps(row) + "\n")
    for oid, *_ in rows:
        Image.new("RGB", (60, 90), (100, 100, 100)).save(
            str(base / "crops" / f"{oid}.jpg"), format="JPEG")
    return base


def _make_numeric_ds(base: Path) -> Path:
    """숫자(정수) label/pred 단일 속성 데이터셋 (QA-ISSUE-007 회귀 재현).

    scoring.py:143 이 str(pred) 로 숫자 pred 를 201 로 수용하는 지원 형상에서,
    갤러리 렌더가 str() 강제 없이 html.escape(pred_norm) 를 부르면
    'int' object has no attribute 'replace' 로 500 크래시했다(TASK-011 회귀).

    rows: (obj_id, label, pred)  — 정수 스칼라
      a 0 0  correct
      b 1 2  wrong
      c 2 2  correct
    """
    (base / "crops").mkdir(parents=True)
    rows = [("a", 0, 0), ("b", 1, 2), ("c", 2, 2)]
    with open(base / "labels.jsonl", "w", encoding="utf-8") as f:
        for oid, lab, _p in rows:
            f.write(json.dumps({"obj_id": oid, "label": lab}) + "\n")
    with open(base / "predictions.jsonl", "w", encoding="utf-8") as f:
        for oid, _l, pred in rows:
            f.write(json.dumps({"obj_id": oid, "pred": pred,
                                "margin": 0.5, "quality": 0.5}) + "\n")
    for oid, *_ in rows:
        Image.new("RGB", (60, 90), (100, 100, 100)).save(
            str(base / "crops" / f"{oid}.jpg"), format="JPEG")
    return base


# =====================================================================
# 1. HTML 단언 — pred 버튼 존재 · label/상태 버튼 유지 · data-pred 정규화 · 축 식별자
# =====================================================================

def test_gallery_html_has_pred_and_label_axis_buttons(tmp_path: Path) -> None:
    from evalkit.gallery import build_gallery

    ds = _make_binary_ds(tmp_path / "ds")
    text = Path(build_gallery(ds)).read_text(encoding="utf-8")

    # pred 버튼이 label 버튼과 대칭으로 생성(REQ-011 / SRV-016)
    assert ">pred=male</button>" in text
    assert ">pred=female</button>" in text
    # 기존 label 버튼 유지(회귀 없음)
    assert ">label=male</button>" in text
    assert ">label=female</button>" in text
    # 기존 상태 버튼 유지
    assert ">전체</button>" in text
    assert ">오답만</button>" in text
    assert ">정답만</button>" in text

    # 각 버튼이 축 식별자로 flt 호출(축 분리 — SRV-017)
    assert "flt('status','all',this)" in text
    assert "flt('status','wrong',this)" in text
    assert "flt('status','correct',this)" in text
    assert "flt('label','male',this)" in text
    assert "flt('pred','male',this)" in text
    # data-axis 식별자 부여
    assert 'data-axis="pred"' in text
    assert 'data-axis="label"' in text
    assert 'data-axis="status"' in text


def test_gallery_html_normalizes_none_pred_to_unknown(tmp_path: Path) -> None:
    """SRV-018: pred=None 카드의 data-pred 가 'unknown' 으로 정규화되고,
    pred=unknown 버튼이 산출된다(소스 무관 동일 버킷)."""
    from evalkit.gallery import build_gallery

    ds = _make_binary_ds(tmp_path / "ds")
    text = Path(build_gallery(ds)).read_text(encoding="utf-8")

    assert 'data-pred="unknown"' in text
    assert ">pred=unknown</button>" in text
    # 빈 문자열 data-pred 잔존 금지(정규화 통일)
    assert 'data-pred=""' not in text


def test_gallery_numeric_pred_renders_without_error(tmp_path: Path) -> None:
    """QA-ISSUE-007 회귀: 숫자(정수) pred 데이터에서 build_gallery 가 예외 없이
    렌더되고, data-pred 가 str() 정규화된 문자열 값이며 pred 버튼이 생성된다.

    이전 회귀에서는 pred_norm 이 raw int 를 보존해 html.escape(pred_norm) 가
    AttributeError('int' has no 'replace') 로 500 크래시했다. str(pred) 강제로 복구.
    """
    from evalkit.gallery import build_gallery

    ds = _make_numeric_ds(tmp_path / "ds")
    # 예외 없이 렌더(회귀 시 여기서 AttributeError)
    text = Path(build_gallery(ds)).read_text(encoding="utf-8")

    # data-pred 가 문자열 값으로 정규화(raw int 아님) — scoring.py:143 규칙과 통일
    assert 'data-pred="0"' in text
    assert 'data-pred="2"' in text
    # pred 버튼도 문자열 버킷으로 생성(SRV-018 소스 무관 동일 버킷)
    assert ">pred=0</button>" in text
    assert ">pred=2</button>" in text
    # label 버튼도 문자열로 정규화(truthy 정수 라벨)
    assert ">label=1</button>" in text
    assert ">label=2</button>" in text
    # 표시 pl(pred 표시)도 동일 정규화 버킷 사용 — raw int 새지 않음
    assert "pred <b>0</b>" in text
    assert "pred <b>2</b>" in text


def test_gallery_numeric_pred_reextracted_renders(tmp_path: Path) -> None:
    """QA-ISSUE-007 서버 경로: predictions.jsonl 없이 attributes.jsonl + manifest
    pred_path 로 숫자 pred 를 재추출(_extracted_preds)하는 경로도 예외 없이 렌더된다.

    이는 render_run_gallery(server)가 타는 경로(재추출 pred 는 raw 숫자 유지,
    gallery.py:166)로, _build_single 의 str() 정규화가 없으면 500 크래시했다.
    """
    from evalkit.gallery import build_gallery

    ds = tmp_path / "ds"
    (ds / "crops").mkdir(parents=True)
    (ds / "manifest.yaml").write_text(
        "n: 3\nattributes:\n  grp:\n    labels: [0, 1, 2]\n"
        "    pred_path: attributes.grp_id\n", encoding="utf-8")
    rows = [("a", 0, 0), ("b", 1, 2), ("c", 2, 2)]  # (obj, label, numeric pred)
    with open(ds / "labels.jsonl", "w", encoding="utf-8") as f:
        for oid, lab, _p in rows:
            f.write(json.dumps({"obj_id": oid, "labels": {"grp": lab}}) + "\n")
    with open(ds / "attributes.jsonl", "w", encoding="utf-8") as f:
        for oid, _l, pred in rows:
            f.write(json.dumps({"obj_id": oid,
                                "plr_json": {"attributes": {"grp_id": pred}}}) + "\n")
    for oid, *_ in rows:
        Image.new("RGB", (60, 90), (100, 100, 100)).save(
            str(ds / "crops" / f"{oid}.jpg"), format="JPEG")

    # 재추출 경로에서 예외 없이 렌더(회귀 시 여기서 AttributeError → 서버 500)
    text = Path(build_gallery(ds)).read_text(encoding="utf-8")
    # 재추출 숫자 pred 가 문자열 data-pred 로 정규화되고 pred 버튼 생성
    assert 'data-pred="0"' in text
    assert 'data-pred="2"' in text
    assert ">pred=0</button>" in text
    assert ">pred=2</button>" in text


def test_gallery_html_escapes_button_values(tmp_path: Path) -> None:
    """SRV-016: 버튼 텍스트/값이 html.escape 로 이스케이프된다."""
    from evalkit.gallery import build_gallery

    ds = tmp_path / "ds"
    (ds / "crops").mkdir(parents=True)
    (ds / "labels.jsonl").write_text(
        json.dumps({"obj_id": "x", "label": "<b>&amp"}) + "\n", encoding="utf-8")
    (ds / "predictions.jsonl").write_text(
        json.dumps({"obj_id": "x", "pred": "<i>", "margin": 0.5}) + "\n",
        encoding="utf-8")
    Image.new("RGB", (60, 90), (100, 100, 100)).save(
        str(ds / "crops" / "x.jpg"), format="JPEG")

    text = Path(build_gallery(ds)).read_text(encoding="utf-8")
    # pred 값 '<i>' 가 pred 버튼에 이스케이프되어 실린다
    assert ">pred=&lt;i&gt;</button>" in text
    # label 값 '<b>&amp' 가 label 버튼에 이스케이프되어 실린다
    assert "&lt;b&gt;&amp;amp" in text
    # 원시(escape 안 된) 태그가 버튼 텍스트로 새지 않는다
    assert ">pred=<i></button>" not in text


def test_build_multi_untouched(tmp_path: Path) -> None:
    """SRV-020: 다속성 경로(_build_multi)는 이 변경과 무관 — 체크박스×AND/OR
    필터 UI 가 그대로 산출된다(단일선택 flt 오염 없음)."""
    from evalkit.gallery import build_gallery

    ds = tmp_path / "ds"
    (ds / "crops").mkdir(parents=True)
    rows = [("a", {"gender": "male", "type": "tank"}),
            ("b", {"gender": "female", "type": "jeep"})]
    with open(ds / "labels.jsonl", "w", encoding="utf-8") as f:
        for oid, labs in rows:
            for attr, v in labs.items():
                f.write(json.dumps({"obj_id": oid, "attribute": attr,
                                    "label": v}) + "\n")
    with open(ds / "predictions.jsonl", "w", encoding="utf-8") as f:
        for oid, labs in rows:
            for attr, v in labs.items():
                f.write(json.dumps({"obj_id": oid, "attribute": attr,
                                    "pred": v, "margin": 0.5}) + "\n")
    for oid, _ in rows:
        Image.new("RGB", (60, 90), (100, 100, 100)).save(
            str(ds / "crops" / f"{oid}.jpg"), format="JPEG")

    text = Path(build_gallery(ds, attribute="gender,type")).read_text(encoding="utf-8")
    # 다속성 필터 UI 표식 — 단일선택 flt 로 대체되지 않음
    assert 'class="aflt"' in text
    assert "setMode('and'" in text and "setMode('or'" in text
    assert "setStatus('wrong'" in text
    # 단일 flt 축 호출은 다속성 경로에 없음
    assert "flt('pred'" not in text


# =====================================================================
# 2. node flt 단언 — 축 내 OR / 축 간 AND / 미선택 전체 / 토글 / 교차 오염 없음
# =====================================================================

def _js_single_src() -> str:
    """gallery._JS_SINGLE 원문(sel/flt/render/syncButtons) 추출.
    Jinja 없이 순수 JS 라 node 에서 그대로 실행 가능."""
    from evalkit.gallery import _JS_SINGLE
    assert "function flt(" in _JS_SINGLE and "const sel" in _JS_SINGLE
    return _JS_SINGLE


# 최소 DOM 목킹 하니스. 카드 5장(a~e)과 버튼(status/label/pred 축)을 만들고
# flt() 를 순서대로 호출해 표시(display)·버튼 on 상태를 JSON 으로 출력한다.
_HARNESS = r"""
function mkCard(id,k,label,pred){
  return {id:id, dataset:{k:k,label:label,pred:pred}, style:{display:''}};
}
function mkBtn(axis,val){
  const cl=new Set();
  return {dataset:{axis:axis,val:val},
    classList:{
      toggle:(c,on)=>{ if(on===undefined){ cl.has(c)?cl.delete(c):cl.add(c);}
                       else { on?cl.add(c):cl.delete(c);} return cl.has(c);},
      contains:c=>cl.has(c), add:c=>cl.add(c), remove:c=>cl.delete(c)}};
}
const cards=[
  mkCard('a','correct','female','female'),
  mkCard('b','wrong','female','male'),   // 혼동 셀 female→male
  mkCard('c','correct','male','male'),
  mkCard('d','wrong','male','female'),
  mkCard('e','unlabeled','male','unknown'),
];
const buttons=[
  mkBtn('status','all'), mkBtn('status','wrong'), mkBtn('status','correct'),
  mkBtn('label','male'), mkBtn('label','female'),
  mkBtn('pred','male'), mkBtn('pred','female'), mkBtn('pred','unknown'),
];
globalThis.document={
  querySelectorAll:(q)=>{
    if(q==='.card') return cards;
    if(q==='.filters button') return buttons;
    return [];
  }
};
function btn(axis,val){return buttons.find(b=>b.dataset.axis===axis&&b.dataset.val===val);}
function shown(){return cards.filter(c=>c.style.display==='').map(c=>c.id);}
function onVals(){return buttons.filter(b=>b.classList.contains('on'))
  .map(b=>b.dataset.axis+':'+b.dataset.val);}

const out={};
// 초기 렌더(전체 표시, '전체' 버튼 on)
syncButtons();
out.initial={shown:shown(), on:onVals()};

// 교차 오염 없음: label=male 단독 → data-label==='male' 카드만(c,d,e),
//   data-pred==='male' 이지만 label==='female' 인 b 는 숨김
flt('label','male',btn('label','male'));
out.labelMale={shown:shown(), on:onVals()};

// 축 내 OR: label=male + label=female → 두 label 합집합(a,b,c,d,e 전부 label 있음)
flt('label','female',btn('label','female'));
out.labelMaleFemale={shown:shown()};

// 초기화(토글 해제): 두 label 다시 눌러 해제 → 전체 복귀
flt('label','male',btn('label','male'));
flt('label','female',btn('label','female'));
out.afterToggleOff={shown:shown(), on:onVals()};

// 축 간 AND(혼동 셀): label=female + pred=male → b 만
flt('label','female',btn('label','female'));
flt('pred','male',btn('pred','male'));
out.confusionCell={shown:shown(), on:onVals()};

// 상태 축 AND: 위 상태에서 status=correct 추가 → female&pred=male&correct = 없음
flt('status','correct',btn('status','correct'));
out.plusCorrect={shown:shown()};

// '전체' = 상태 축만 초기화(label/pred 유지) → 다시 b 만
flt('status','all',btn('status','all'));
out.statusAllResetsStatusOnly={shown:shown()};

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 없음 — flt 멀티선택 경로 검증 생략 (checklist: skip + 사유 표기)")
def test_flt_multiselect_axes_node(tmp_path: Path) -> None:
    harness = tmp_path / "flt_harness.js"
    harness.write_text(_js_single_src() + _HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(harness)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout.strip().splitlines()[-1])

    # 미선택 시 전체 표시 + '전체' 버튼만 on(기존 동작 보존)
    assert r["initial"]["shown"] == ["a", "b", "c", "d", "e"]
    assert r["initial"]["on"] == ["status:all"]

    # 교차 오염 없음: label=male → label==='male' 카드만(c,d,e), b(pred=male) 제외
    assert r["labelMale"]["shown"] == ["c", "d", "e"]
    assert "label:male" in r["labelMale"]["on"]
    # label 선택 시 '전체'(status:all) 버튼은 상태 축 기준이라 여전히 on
    assert "status:all" in r["labelMale"]["on"]

    # 축 내 OR: label male+female → 모든 카드 label 존재 → 전부
    assert r["labelMaleFemale"]["shown"] == ["a", "b", "c", "d", "e"]

    # 토글 해제로 전체 복귀
    assert r["afterToggleOff"]["shown"] == ["a", "b", "c", "d", "e"]
    assert r["afterToggleOff"]["on"] == ["status:all"]

    # 축 간 AND(혼동 셀): label=female AND pred=male → b 만
    assert r["confusionCell"]["shown"] == ["b"]
    assert "label:female" in r["confusionCell"]["on"]
    assert "pred:male" in r["confusionCell"]["on"]
    # '전체'(status:all)는 상태 축 기준 — 상태 축이 비어 있으면 label/pred 선택과
    # 무관하게 on 을 유지한다(상태 축 리셋 버튼이므로 일관됨).
    assert "status:all" in r["confusionCell"]["on"]

    # 상태 축도 AND — correct 추가 시 wrong 카드 b 사라짐
    assert r["plusCorrect"]["shown"] == []

    # '전체'는 상태 축만 초기화(label/pred 유지) → 다시 b
    assert r["statusAllResetsStatusOnly"]["shown"] == ["b"]
