"""aggregate — 리더보드 종합 컬럼의 단일 정의.

다속성 종합(스펙 R4): 단일 강제 순위 없음 — macro/micro 평균을 제공하고
사용자가 컬럼으로 정렬한다.
  macro_f1        = 속성별 macro_f1의 평균 (None 속성 제외)
  macro_acc       = 속성별 accuracy의 평균
  micro_acc       = 전 속성 합산 correct / 합산 n
  macro_precision = 속성별 macro precision의 평균 (파생 불가 속성 제외)
  macro_recall    = 속성별 macro recall의 평균 (파생 불가 속성 제외)
"""
from __future__ import annotations


def derive_macro_pr(metrics: dict[str, dict]) -> dict:
    """속성별 클래스 recall/precision dict에서 run 수준 macro_precision/
    macro_recall을 파생한다 (REQ-001, SRV-002).

    클래스 집합·결측 관례는 evalkit macro_f1(scoring.py gt_classes)과 동일:
    gt_classes = 정답에 등장한 클래스(= recall 키 집합), 결측 클래스 값은 0.0,
    round 4. 채점 재구현이 아니라 score()가 이미 기록한 클래스별 값의 평균
    정의만 둔다 — evalkit/scoring.py는 무수정 (contract parity 보호).

    aggregate()(신규 run 채점 시점)와 web._run_rows(aggregate 키가 없는
    구형 run의 읽기 시점 폴백)가 공유하는 단일 원천.
    """
    precs: list[float] = []
    recs: list[float] = []
    for m in metrics.values():
        recall = m.get("recall") or {}
        precision = m.get("precision") or {}
        classes = m.get("classes") or sorted(recall)
        gt_classes = [c for c in classes if c in recall]
        if not gt_classes:
            continue  # 정답 클래스 없음(파생 재료 없음) — 속성 제외
        recs.append(round(
            sum(recall.get(c, 0.0) for c in gt_classes) / len(gt_classes), 4))
        precs.append(round(
            sum(precision.get(c, 0.0) for c in gt_classes) / len(gt_classes), 4))
    return {
        "macro_precision": round(sum(precs) / len(precs), 4) if precs else None,
        "macro_recall": round(sum(recs) / len(recs), 4) if recs else None,
    }


def aggregate(metrics: dict[str, dict]) -> dict:
    """metrics = {attribute: evalkit.scoring.score() 결과}."""
    f1s = [m["macro_f1"] for m in metrics.values() if m.get("macro_f1") is not None]
    accs = [m["accuracy"] for m in metrics.values() if m.get("accuracy") is not None]
    total_n = sum(m.get("n", 0) for m in metrics.values())
    total_correct = sum(m.get("correct", 0) for m in metrics.values())
    return {
        # 기존 키는 이름·형식 불변 — 신규 키는 additive만 (INT-002 계약)
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else None,
        "macro_acc": round(sum(accs) / len(accs), 4) if accs else None,
        "micro_acc": round(total_correct / total_n, 4) if total_n else None,
        "n_total": total_n,
        **derive_macro_pr(metrics),
    }
