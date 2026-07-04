"""server.scoring — run 채점 어댑터.

채점 구현은 lab과 **동일한 함수**(evalkit.scoring.score)이고, 속성 선택도
**동일한 헬퍼**(evalkit.dataset.eval_attributes)다 — 서버에 채점 로직 사본은
한 줄도 없다 (지표 드리프트 구조적 차단, 계획 원칙 1).
"""
from __future__ import annotations

import json
from pathlib import Path

from evalkit.dataset import eval_attributes
from evalkit.scoring import ScoringError, score

from server.aggregate import aggregate


def score_run(dataset_dir: str | Path, attributes_jsonl: str | Path,
              *, margin_threshold: float = 0.7,
              quality_threshold: float = 0.4) -> dict:
    """등록 데이터셋의 라벨로 run(attributes.jsonl)을 전 속성 채점.

    Returns {"attributes": {attr: score()결과}, "aggregate": {...},
             "skipped": [...], "undeclared": [...]}

    Raises ScoringError — 평가 가능한 속성이 하나도 없을 때.
    """
    attrs, skipped, undeclared = eval_attributes(dataset_dir)
    if not attrs:
        raise ScoringError(
            "no evaluable attributes: manifest attributes:/labels.jsonl을 확인하세요")

    per_attr: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for attribute in attrs:
        try:
            per_attr[attribute] = score(
                dataset_dir, attribute,
                # 예측 소스는 run 디렉터리의 파일들 — predictions.jsonl은 (제출
                # 시 없다면) 빈 소스가 되고, score() 내부 폴백이 attributes.jsonl
                # 에서 pred_path 재추출한다. 해석 코드는 lab CLI와 동일 경로.
                predictions_path=str(Path(attributes_jsonl).with_name("predictions.jsonl")),
                attributes_path=str(attributes_jsonl),
                margin_threshold=margin_threshold,
                quality_threshold=quality_threshold,
            )
        except ScoringError as exc:
            errors[attribute] = str(exc)
    if not per_attr:
        raise ScoringError("all attributes failed: " + json.dumps(errors, ensure_ascii=False))

    return {
        "attributes": per_attr,
        "aggregate": aggregate(per_attr),
        "skipped": skipped,
        "undeclared": undeclared,
        "errors": errors,
    }
