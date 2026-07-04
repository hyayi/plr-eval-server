"""aggregate — 리더보드 종합 컬럼의 단일 정의.

다속성 종합(스펙 R4): 단일 강제 순위 없음 — macro/micro 평균을 제공하고
사용자가 컬럼으로 정렬한다.
  macro_f1   = 속성별 macro_f1의 평균 (None 속성 제외)
  macro_acc  = 속성별 accuracy의 평균
  micro_acc  = 전 속성 합산 correct / 합산 n
"""
from __future__ import annotations


def aggregate(metrics: dict[str, dict]) -> dict:
    """metrics = {attribute: evalkit.scoring.score() 결과}."""
    f1s = [m["macro_f1"] for m in metrics.values() if m.get("macro_f1") is not None]
    accs = [m["accuracy"] for m in metrics.values() if m.get("accuracy") is not None]
    total_n = sum(m.get("n", 0) for m in metrics.values())
    total_correct = sum(m.get("correct", 0) for m in metrics.values())
    return {
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else None,
        "macro_acc": round(sum(accs) / len(accs), 4) if accs else None,
        "micro_acc": round(total_correct / total_n, 4) if total_n else None,
        "n_total": total_n,
    }
