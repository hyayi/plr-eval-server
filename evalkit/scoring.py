"""scoring — 순수 채점 코어 (지표 단일 원천).

lab CLI(eval/run_eval.py main)와 평가 서버(server/scoring.py)가 **같은 함수**
`score()`에서 종결된다 — 채점 구현이 하나뿐이므로 지표 드리프트가 구조적으로
불가능하다 (.omc/plans/plr-eval-server.md 원칙 1).

score()가 소유하는 것 (전부 함수 내부):
  - 이중 예측 소스 해석: predictions.jsonl의 attribute 스탬프 확인 →
    스탬프 불일치/파일 부재 시 attributes.jsonl에서 spec["pred_path"]로
    재추출하는 폴백 (기존 run_eval.py의 147-192 로직 이관)
  - 라벨 로드(load_labels), unknown 제외 정책, 전 지표 계산

score()가 만지지 않는 것 (호출자 소관):
  - prompt_hash/seed_hash/gemma_repo 등 provenance, ledger append,
    sys.argv/argparse, SEED.md/git 조회, stdout 출력
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from typing import Any

from evalkit.dataset import attribute_spec, load_labels, resolve_json_path

# Per-attribute "bias" metric preset: (true_class -> mistaken_as).
# manifest의 bias_pair 선언이 있으면 그것이 우선한다 (attribute_spec 병합).
BIAS_PAIR = {
    "gender": ("female", "male"),
}


class ScoringError(ValueError):
    """채점 불가 상태 — CLI는 SystemExit로, 서버는 4xx로 번역한다."""


def _jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def signal_stats(
    vals: dict, eval_ids: list, is_correct: dict, threshold: float,
) -> dict | None:
    """크롭별 신호(margin/quality) 구간별 accuracy — 캘리브레이션 검증.
    오답이 저신호 구간에 몰리면 신호가 유효(런타임 필터로 활용 가능),
    high/low accuracy가 같으면 노이즈.

    입력/출력 예) margin {a:0.9(정답),b:0.2(오답)}, threshold 0.7
      → {"high":{"n":1,"accuracy":1.0}, "low":{"n":1,"accuracy":0.0},
         "mean_correct":0.9, "mean_wrong":0.2, …}
    Returns None when the signal is absent from predictions."""
    ids = [i for i in eval_ids if vals.get(i) is not None]
    if not ids:
        return None

    def _acc(group):
        return round(sum(is_correct[i] for i in group) / len(group), 4) if group else None

    def _mean(group):
        return round(sum(vals[i] for i in group) / len(group), 4) if group else None

    hi = [i for i in ids if vals[i] >= threshold]
    lo = [i for i in ids if vals[i] < threshold]
    return {
        "threshold": threshold,
        "n": len(ids),
        "high": {"n": len(hi), "accuracy": _acc(hi)},
        "low": {"n": len(lo), "accuracy": _acc(lo)},
        "mean_correct": _mean([i for i in ids if is_correct[i]]),
        "mean_wrong": _mean([i for i in ids if not is_correct[i]]),
    }


def score(
    dataset_dir: str | os.PathLike[str],
    attribute: str,
    *,
    predictions_path: str | None = None,
    attributes_path: str | None = None,
    margin_threshold: float = 0.7,
    quality_threshold: float = 0.4,
) -> dict[str, Any]:
    """한 속성을 채점해 지표 dict를 반환한다 (provenance/부수효과 없음).

    predictions_path/attributes_path 기본값은 dataset_dir 안의 관례 파일.
    서버는 run 디렉터리의 attributes.jsonl 경로를 넘긴다 — 해석 코드는
    호출자와 무관하게 이 함수 하나다.

    Returns (지표 필드만):
      n, correct, accuracy, recall, precision, f1, macro_f1, bias, confusion,
      classes, pred_unknown, n_label_unknown, margin_stats, quality_stats,
      resolved_model(예측 행 스탬프에서; 없으면 None)

    Raises:
      ScoringError — 예측/라벨 부재, 조인 공집합, 전 라벨 unknown 등.
    """
    gdir = str(dataset_dir)
    spec = attribute_spec(gdir, attribute)

    preds_path = predictions_path or os.path.join(gdir, "predictions.jsonl")
    attrs_path = attributes_path or os.path.join(gdir, "attributes.jsonl")
    pred_rows = (
        {r["obj_id"]: r for r in _jsonl(preds_path)}
        if os.path.exists(preds_path) else {}
    )

    # 모델 이름: re_score가 행에 남긴 스탬프에서 (없으면 None — 호출자가 결정).
    model_stamps = {r.get("model") for r in pred_rows.values() if r.get("model")}
    resolved_model = sorted(model_stamps)[0] if model_stamps else None

    # predictions.jsonl은 "한 속성"의 추출물 — 행의 attribute 스탬프가 요청
    # 속성과 다르거나 파일이 없으면 attributes.jsonl(전체 plr_json 캐시)에서
    # 재추출. 이 폴백이 함수 안에 있어야 CLI/서버가 같은 해석을 지난다.
    stamped = {r.get("attribute") for r in pred_rows.values() if r.get("attribute")}
    if (not pred_rows) or (stamped and attribute not in stamped):
        if not os.path.exists(attrs_path):
            raise ScoringError(
                f"No predictions for attribute={attribute!r} and no "
                f"attributes.jsonl to extract from — run `lab run` first."
            )
        if not spec.get("pred_path"):
            raise ScoringError(
                f"attribute={attribute!r}: no pred_path (declare it in "
                f"manifest.yaml `attributes:` or use a preset attribute)."
            )
        extracted: dict[str, dict] = {}
        for r in _jsonl(attrs_path):
            oid = r["obj_id"]
            pj = r.get("plr_json") or {}
            row: dict = {"obj_id": oid, "attribute": attribute,
                         "pred": resolve_json_path(pj, spec["pred_path"])}
            if spec.get("margin_path"):
                m = resolve_json_path(pj, spec["margin_path"])
                if isinstance(m, (int, float)):
                    row["margin"] = float(m)
            q = (pred_rows.get(oid) or {}).get("quality")
            if q is not None:  # 품질은 크롭의 속성 — 어느 속성 평가든 재사용
                row["quality"] = q
            extracted[oid] = row
        pred_rows = extracted

    preds = {i: (str(r.get("pred")) if r.get("pred") not in (None, "") else "unknown")
             for i, r in pred_rows.items()}
    labels = load_labels(gdir, attribute)

    ids = [i for i in preds if i in labels]
    if not ids:
        raise ScoringError(
            f"No overlap between predictions and labels for "
            f"attribute={attribute!r} (multi-attribute labels.jsonl rows "
            f"must carry that key inside \"labels\")."
        )

    # 강제커밋 준수도: 라벨 필터 전, 매칭된 전체 id 기준.
    n_pred_unknown = sum(1 for i in ids if preds[i] == "unknown")
    pred_unknown = {
        "rate": round(n_pred_unknown / len(ids), 4),
        "count": f"{n_pred_unknown}/{len(ids)}",
    }

    # label=="unknown"(사람도 판별 불가)은 채점 제외 — 별도 집계.
    eval_ids = [i for i in ids if labels[i] != "unknown"]
    n_label_unknown = len(ids) - len(eval_ids)
    if not eval_ids:
        raise ScoringError(
            "All labels are 'unknown' — nothing to score against. "
            "Label at least one crop with a decided class."
        )

    classes = sorted({*(labels[i] for i in eval_ids), *(preds[i] for i in eval_ids)})
    confusion: dict[str, Counter] = defaultdict(Counter)
    correct = 0
    for i in eval_ids:
        confusion[labels[i]][preds[i]] += 1
        correct += labels[i] == preds[i]
    n = len(eval_ids)
    acc = correct / n

    recall = {}
    for c in classes:
        cid = [i for i in eval_ids if labels[i] == c]
        if cid:
            recall[c] = round(sum(preds[i] == c for i in cid) / len(cid), 4)

    precision = {}
    for c in classes:
        pid = [i for i in eval_ids if preds[i] == c]
        if pid:
            precision[c] = round(sum(labels[i] == c for i in pid) / len(pid), 4)
    f1 = {}
    for c in classes:
        r, pr = recall.get(c), precision.get(c)
        if r is None and pr is None:
            continue
        r, pr = r or 0.0, pr or 0.0
        f1[c] = round(2 * pr * r / (pr + r), 4) if (pr + r) > 0 else 0.0
    gt_classes = [c for c in classes if c in recall]
    macro_f1 = (round(sum(f1.get(c, 0.0) for c in gt_classes) / len(gt_classes), 4)
                if gt_classes else None)

    is_correct = {i: labels[i] == preds[i] for i in eval_ids}
    margins = {i: pred_rows[i].get("margin") for i in eval_ids}
    qualities = {i: pred_rows[i].get("quality") for i in eval_ids}
    margin_stats = signal_stats(margins, eval_ids, is_correct, margin_threshold)
    quality_stats = signal_stats(qualities, eval_ids, is_correct, quality_threshold)

    # 헤드라인 bias: manifest 선언(attribute_spec 병합) → 프리셋 순.
    pair = None
    bp = spec.get("bias_pair")
    if isinstance(bp, (list, tuple)) and len(bp) == 2:
        pair = (str(bp[0]), str(bp[1]))
    if pair is None:
        pair = BIAS_PAIR.get(attribute)

    bias = None
    if pair:
        t_cls, as_cls = pair
        tid = [i for i in eval_ids if labels[i] == t_cls]
        if tid:
            bias = {"pair": f"{t_cls}->{as_cls}",
                    "rate": round(sum(preds[i] == as_cls for i in tid) / len(tid), 4),
                    "count": f"{sum(preds[i] == as_cls for i in tid)}/{len(tid)}"}

    return {
        "n": n,
        "correct": correct,
        "accuracy": round(acc, 4),
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "macro_f1": macro_f1,
        "bias": bias,
        "confusion": {t: dict(confusion[t]) for t in classes},
        "classes": classes,
        "pred_unknown": pred_unknown,
        "n_label_unknown": n_label_unknown,
        "margin_stats": margin_stats,
        "quality_stats": quality_stats,
        "resolved_model": resolved_model,
    }
