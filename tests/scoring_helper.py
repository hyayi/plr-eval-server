"""테스트용 채점 헬퍼 — run_eval.main() CLI가 삭제된 뒤(client/server 재정렬)
score()를 직접 부르는 공유 어댑터.

과거 `_run_eval(golden, ledger, attribute)`가 run_eval.main을 sys.argv로 돌려
ledger 레코드를 반환하던 자리를, 순수 `evalkit.scoring.score()` 호출로 대체한다.
지표 필드(accuracy/recall/precision/f1/macro_f1/bias/confusion/pred_unknown/
n_label_unknown/margin_stats/quality_stats/n)는 동일 — ledger 전용 provenance
필드(seed_hash/gemma_repo/prompt_hash/version/model)는 서버 소관이라 여기 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LAB_ROOT = Path(__file__).parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from evalkit.scoring import score  # noqa: E402


def score_record(golden_dir, attribute: str, **kw) -> dict:
    """score()를 호출해 지표 dict를 반환 (구 _run_eval 레코드의 지표 부분과 동치)."""
    return score(str(golden_dir), attribute, **kw)
