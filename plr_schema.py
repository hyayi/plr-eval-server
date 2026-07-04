"""PLR(사람/차량 속성 추출) 도메인 어휘의 로더 + 파생물 생성기.

이 모듈 자체는 데이터를 갖지 않는다 — 어휘의 단일 원천은 schema/vocab.yaml이고,
여기서는 그것을 로드해 세 종류의 파생물을 만든다:

  1. 모듈 상수  : COLOR_ENUM, GENDER_ENUM … (tuple — 순서 보존, 불변)
  2. 조회 함수  : color_group(), lower_shape_of() … (그룹/매핑 역참조)
  3. JSON 스키마: PERSON_SCHEMA / VEHICLE_SCHEMA (모델 출력의 검증 규격)

같은 로드 결과를 4개의 소비자가 본다 — 어휘를 바꾸면 넷이 함께 움직인다:
  - plr_prompts.py      : 프롬프트에 enum 선택지 주입          (모델 인풋)
  - plr_parse.py        : 응답을 enum으로 강제 정규화           (모델 아웃풋)
  - scoring.py (core/ir): 검색 하드게이트의 그룹 매칭
  - template_caption.py / DB (core/ir): 캡션 생성·저장 계약

내부 언어는 전부 영어("internal language unified to English" 설계 결정).
한국어는 쿼리 파서 입구와 caption_ko 출구에서만 나타난다.
"""

from __future__ import annotations

from typing import Any, Final

# ---------------------------------------------------------------------------
# 선언적 어휘 로드 — schema/vocab.yaml이 도메인 enum/그룹/매핑의 단일 원천.
# import 시점에 한 번 읽어 아래의 모든 상수·함수·스키마를 파생시킨다.
# "파일 하나 = 어휘 버전 하나": vocab.yaml을 고치면 프롬프트 주입과 파서
# 정규화가 자동으로 함께 바뀐다 (한쪽만 바뀌는 반쪽 실험이 구조적으로 불가능).
# vocab.yaml 세 섹션의 발동 시점:
#   enums  → 추출 시점 (프롬프트 선택지 + 저장되는 라벨 값 자체)
#   groups → 검색 시점 (무엇과 무엇이 같은 것으로 매칭되나)
#   maps   → 읽기 시점 (저장된 fine 라벨에서 coarse 축을 파생)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path

import yaml as _yaml

_VOCAB_PATH = _Path(__file__).resolve().parent / "schema" / "vocab.yaml"
with open(_VOCAB_PATH, encoding="utf-8") as _fh:
    _VOCAB: Final[dict] = _yaml.safe_load(_fh)


def _enum(key: str) -> tuple[str, ...]:
    """vocab.yaml enums.<key> → 불변 tuple. 순서가 보존되므로 프롬프트에
    주입되는 선택지의 나열 순서도 vocab.yaml이 결정한다.

    입력/출력 예) _enum("gender") → ("male", "female")
    """
    return tuple(_VOCAB["enums"][key])


def _vgroup(key: str) -> dict[str, tuple[str, ...]]:
    """vocab.yaml groups.<key> → {그룹명: (멤버, …)}. 검색 게이트/소프트
    점수의 "같은 것으로 칠 범위" 정의.

    입력/출력 예) _vgroup("color_hard_group")["black"] → ("black", "dark_gray")
    """
    return {k: tuple(v) for k, v in _VOCAB["groups"][key].items()}


def _vmap(key: str) -> dict[str, str]:
    """vocab.yaml maps.<key> → {fine 라벨: coarse 값}. 저장된 행을 읽는
    시점에 새 축을 파생시키는 1:1 번역표 (재인덱싱 회피 장치).

    입력/출력 예) _vmap("lower_type_to_shape")["jeans"] → "long_pants"
    """
    return dict(_VOCAB["maps"][key])



# =====================================================================
# 색상 — 상의/하의/장비/차량이 공유하는 단일 색 어휘
# =====================================================================
# COLOR_ENUM은 프롬프트의 {colors} 자리에 주입되고(인풋), 파서가 모델 답을
# 이 목록으로 강제 정규화한다(아웃풋 — enum 밖 답은 gray 폴백).

COLOR_ENUM: Final[tuple[str, ...]] = _enum("color")

COLOR_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("color_group")


def color_group(label: str) -> str:
    """색 라벨 → 코스(coarse) 그룹. 소비자: ① scoring의 소프트 부분점수
    (통과 후보 순위에서 "비슷한 색" 가점) ② query_parser의 청바지→blue
    패밀리 확장 ③ 캡션 생성. 하드게이트에는 쓰지 않는다 — 그건 아래
    color_hard_group의 몫 (dark 밴드가 갈색·국방색까지 묶어서 너무 넓음).

    입력/출력 예) color_group("black") → "dark" · color_group("red") → "vivid"
    """
    for grp, members in COLOR_GROUP.items():
        if label in members:
            return grp
    return "unknown"


# 정확-색-의도("검은색 차")의 하드게이트 전용 정밀 그룹.
# 코스 COLOR_GROUP은 dark 밴드에 black+dark_gray+dark_brown+dark_green+
# military_olive를 전부 묶는데, 그건 밝기 쿼리("어두운 차")와 소프트 가점에는
# 맞아도 특정 색 검색에는 틀리다 — "검은색 차"를 찾는 사용자는 갈색/녹색/
# 국방색 차를 원하지 않는다 (실측: coarse 밴드로 게이트하니 "빨간 옷" 통과의
# 83%가 빨강 아님 → 2026-06 5차 재설계에서 분리). 여기서 black은 무채색
# dark_gray하고만 묶이고, 유채색 dark들은 각자 hue에 남는다.
# 소비자는 passes_hard_filter의 색 슬롯뿐 — color_group()은 별개로 유지.
COLOR_HARD_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("color_hard_group")


def color_hard_group(label: str) -> str:
    """하드게이트용 정밀 색 그룹 조회. 미등록 색은 라벨 자신을 반환 —
    넓은 밴드에 흡수되지 않고 자기 자신하고만 매칭되게(안전한 기본값).

    입력/출력 예) color_hard_group("dark_gray") → "black" (무채색 dark 등가류)
                 color_hard_group("crimson")   → "crimson" (미등록 → 자기 자신)
    """
    for grp, members in COLOR_HARD_GROUP.items():
        if label in members:
            return grp
    return label or "unknown"


# =====================================================================
# 사람(person) 속성 어휘
# =====================================================================
# GENDER/AGE는 프롬프트에 리터럴(<male|female>)로 박히고, 파서의
# _scores_dict가 이 enum으로 selected 값을 검증한다.

GENDER_ENUM: Final[tuple[str, ...]] = _enum("gender")
AGE_GROUP_ENUM: Final[tuple[str, ...]] = _enum("age_group")

OUTFIT_TYPE_ENUM: Final[tuple[str, ...]] = _enum("outfit_type")

UPPER_TYPE_ENUM: Final[tuple[str, ...]] = _enum("upper_type")

UPPER_TYPE_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("upper_type_group")


def upper_type_group(label: str) -> str:
    """상의 fine 타입 → 그룹(아우터 vs 상의). 하드게이트는 fine 타입(coat vs
    jacket — CCTV로 구분 불안정)이 아니라 이 그룹 수준에서 걸러진다.

    입력/출력 예) upper_type_group("coat") → "upper_outerwear"
                 upper_type_group("tshirt") → "upper_top"
    """
    for grp, members in UPPER_TYPE_GROUP.items():
        if label in members:
            return grp
    return "unknown"


LOWER_TYPE_ENUM: Final[tuple[str, ...]] = _enum("lower_type")

LOWER_TYPE_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("lower_type_group")


def lower_type_group(label: str) -> str:
    """하의 fine 타입 → 그룹. (하드게이트의 실제 축은 아래 lower_shape —
    옷감 구분(jeans vs slacks)은 CCTV에서 불가 판정, 2026-06.)

    입력/출력 예) lower_type_group("jeans") → "pants_like"
    """
    for grp, members in LOWER_TYPE_GROUP.items():
        if label in members:
            return grp
    return "unknown"


# ---------------------------------------------------------------------
# 모양(shape) 축 — CCTV가 실제로 구분 가능한 하드필터 수준 (2026-06 재설계).
# fine 라벨(jeans/slacks…)은 refine 전용으로 남기고, 하드게이트는 작은
# CCTV 크롭에서도 판별되는 coarse 모양(긴바지 vs 반바지 vs 치마 길이)으로
# 돌린다. 실측 근거: VQA가 청바지 크롭 24개 전부에서 박음질을 못 봄 —
# 옷감 구분 불가 확정. 전용 lower_shape 필드가 생기기 전에 인덱싱된
# 행들은 저장된 fine 라벨에서 "읽기 시점"에 모양을 파생시킨다(아래 map —
# 이 축에 재인덱싱이 필요 없었던 이유).
# ---------------------------------------------------------------------

LOWER_SHAPE_ENUM: Final[tuple[str, ...]] = _enum("lower_shape")

# Fine lower_type label -> coarse shape (read-time derivation for old rows).
LOWER_TYPE_TO_SHAPE: Final[dict[str, str]] = _vmap("lower_type_to_shape")


def lower_shape_of(lower_type_label: str | None) -> str:
    """fine 하의 라벨 → 하드게이트용 coarse 모양.
    없거나 미등록이면 'lower_unknown' — 게이트는 이를 wildcard-pass 처리
    (구버전 행 보호).

    입력/출력 예) lower_shape_of("jeans") → "long_pants"
                 lower_shape_of(None)    → "lower_unknown"
    """
    if not lower_type_label:
        return "lower_unknown"
    return LOWER_TYPE_TO_SHAPE.get(lower_type_label, "lower_unknown")


# Upper sleeve axis — HARD-FILTER level. Newly EXTRACTED field; absent on
# pre-redesign rows -> the gate must wildcard-pass (handled in scoring).
UPPER_SLEEVE_ENUM: Final[tuple[str, ...]] = _enum("upper_sleeve")


def upper_outer_of(upper_type_label: str | None) -> str:
    """fine 상의 라벨 → 아우터 여부의 읽기-시점 파생. (재설계 이전 행은
    upper_type 하나만 갖고 있어 이 파생으로 커버; 전용 추출 필드가
    재인덱싱되면 그쪽이 우선.)

    입력/출력 예) upper_outer_of("coat")   → "upper_outerwear"
                 upper_outer_of("tshirt") → "none"
    """
    if not upper_type_label:
        return "none"
    return "upper_outerwear" if upper_type_group(upper_type_label) == "upper_outerwear" else "none"


# Equipment body-location — HARD-FILTER level (presence@location).
EQUIP_LOCATION_ENUM: Final[tuple[str, ...]] = _enum("equip_location")


EQUIPMENT_TYPE_ENUM: Final[tuple[str, ...]] = _enum("equipment_type")

# Coarse-group for equipment so query_parser's hard filter can pass any bag
# when the user just says "가방", or any weapon when they say "무기".
EQUIPMENT_TYPE_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("equipment_type_group")


def equipment_type_group(label: str) -> str:
    """장비 라벨 → 그룹(bag/weapon…). 사용자가 "가방"이라고만 써도 백팩/
    숄더백/핸드백/크로스백 전부가 매칭되게 하는 확장의 근거.

    입력/출력 예) equipment_type_group("backpack") → "bag"
    """
    for grp, members in EQUIPMENT_TYPE_GROUP.items():
        if label in members:
            return grp
    return "unknown"

STATIC_ACTION_ENUM: Final[tuple[str, ...]] = _enum("static_action")


# =====================================================================
# 차량(vehicle) 속성 어휘
# =====================================================================

VEHICLE_TYPE_ENUM: Final[tuple[str, ...]] = _enum("vehicle_type")
# v0.8에는 차종 모델명 ~30개(sonata/grandeur/bmw/…)가 있었으나, 운영
# 재인덱싱 실측에서 Gemma E4B가 214대 중 2대만 "bmw"로 식별(나머지 0) —
# 프롬프트 토큰만 낭비하고 recall 개선이 없어 v0.9에서 제거. ("측정 없이
# 어휘를 늘리지 않는다"의 대표 사례.)

VEHICLE_TYPE_GROUP: Final[dict[str, tuple[str, ...]]] = _vgroup("vehicle_type_group")


def vehicle_type_group(label: str) -> str:
    """차종 라벨 → 그룹.

    입력/출력 예) vehicle_type_group("motorcycle") → "two_wheel"
    """
    for grp, members in VEHICLE_TYPE_GROUP.items():
        if label in members:
            return grp
    return "unknown"


# =====================================================================
# Military judgment (prompt-native, plr_v1.4_cot)
# =====================================================================

# Gemma가 위장무늬/전투복/군장(사람), 위장도색/군용차형/부대마크(차량)
# 단서에서 military/civilian을 직접 판정한다 — 예전처럼 국방색 하나로
# 사후 추론하지 않음(plr_v1.4_cot에서 프롬프트-네이티브화). 파서가 출력을
# 이 enum으로 고정하고, _attach_military_flags가 "military"를
# is_soldier/is_military 스칼라로 변환한다(국방색 규칙은 recall 폴백으로 병존).
MILITARY_ENUM: Final[tuple[str, ...]] = _enum("military")


# =====================================================================
# JSON 스키마 — 모델 출력(파싱 후)의 최종 검증 규격
# =====================================================================
# 파서(plr_parse)가 만든 plr_json이 이 스키마를 통과해야 저장된다.
# 실패 시 스키마-재시도 1회 → 그래도 실패면 DLQ (indexing 쪽 오류 처리).
# enum 필드들이 위의 상수에서 파생되므로 vocab.yaml 변경이 검증 규격까지
# 자동으로 따라온다.

PROMPT_VERSION: Final[str] = "plr_v0.4"


def _topk_array_schema(label_enum: tuple[str, ...]) -> dict[str, Any]:
    """{label: enum값, score: 0~1} 항목 배열의 스키마 생성 헬퍼.
    색/타입처럼 상위 후보 여러 개(top-k)를 받는 필드에 공용."""
    return {
        "type": "array",
        # minItems=0 — production data has frequent "color unknown" cases (low-res
        # crops, motion blur, occlusion). normalize_plr_json() fills in a single
        # placeholder when an array is missing/empty; if the model legitimately
        # returns nothing, indexing should not block.
        "minItems": 0,
        "maxItems": 5,
        "items": {
            "type": "object",
            "required": ["label", "score"],
            "properties": {
                "label": {"type": "string", "enum": list(label_enum)},
                "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
        },
    }


PERSON_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["object_type", "attributes"],
    "properties": {
        "object_type": {"const": "person"},
        "track_id": {"type": "string"},
        "visibility": {
            "type": "object",
            "properties": {
                "body_visibility": {
                    "type": "string",
                    "enum": ["full_body", "upper_only", "lower_only", "partial"],
                },
                "occlusion": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                },
                "image_quality": {
                    "type": "string",
                    "enum": ["good", "fair", "poor"],
                },
                "quality_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "usable_for_attribute": {"type": "boolean"},
                "quality_warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "attributes": {
            "type": "object",
            "required": [
                "gender_scores", "age_group_scores", "outfit_type_scores",
                "upper_clothing", "lower_clothing",
                "static_action_state_scores",
            ],
            "properties": {
                "gender_scores": {
                    "type": "object",
                    "required": ["male", "female", "selected"],
                    "properties": {
                        "male": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "female": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "selected": {"type": "string", "enum": list(GENDER_ENUM)},
                        "decision_margin": {"type": "number"},
                        # reason: 현행 파서가 채우는 근거 한 줄 (gender_reason emit).
                        # evidence/caution은 v0.4 근거-목록 시절의 선택 필드 (구행 호환).
                        "reason": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "caution": {"type": "string"},
                    },
                },
                "age_group_scores": {
                    "type": "object",
                    "required": ["adult", "child", "selected"],
                    "properties": {
                        "adult": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "child": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "selected": {"type": "string", "enum": list(AGE_GROUP_ENUM)},
                        "decision_margin": {"type": "number"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "outfit_type_scores": {
                    "type": "object",
                    "required": ["selected"],
                    "properties": {
                        "two_piece": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "one_piece": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "layered": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "obscured": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "selected": {"type": "string", "enum": list(OUTFIT_TYPE_ENUM)},
                        "decision_margin": {"type": "number"},
                    },
                },
                "upper_clothing": {
                    "type": "object",
                    "required": ["color_topk", "type_topk"],
                    "properties": {
                        "color_topk": _topk_array_schema(COLOR_ENUM),
                        "type_topk": _topk_array_schema(UPPER_TYPE_ENUM),
                        # sleeve: plr_v1.5_cot부터 추출 프롬프트가 emit (upper.sleeve).
                        "sleeve": {"type": "string", "enum": list(UPPER_SLEEVE_ENUM)},
                        "evidence": {"type": "string"},
                        # Scalar derived field for JSONB-containment queries (set at index time).
                        "primary_color": {"type": "string", "enum": list(COLOR_ENUM)},
                    },
                },
                "lower_clothing": {
                    "type": "object",
                    "required": ["color_topk", "type_topk"],
                    "properties": {
                        "color_topk": _topk_array_schema(COLOR_ENUM),
                        "type_topk": _topk_array_schema(LOWER_TYPE_ENUM),
                        "evidence": {"type": "string"},
                        # Scalar derived field for JSONB-containment queries (set at index time).
                        "primary_color": {"type": "string", "enum": list(COLOR_ENUM)},
                    },
                },
                "equipment": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "score"],
                        "properties": {
                            "type": {"type": "string", "enum": list(EQUIPMENT_TYPE_ENUM)},
                            "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "color_topk": _topk_array_schema(COLOR_ENUM),
                            "evidence": {"type": "string"},
                        },
                    },
                },
                "static_action_state_scores": {
                    "type": "object",
                    "required": ["selected"],
                    "properties": {
                        # 행동별 개별 점수 키(standing, sitting, …)는 enum에서 파생
                        # — vocab.yaml 확장 시 선언도 자동 동행.
                        **{
                            action: {"type": "number", "minimum": 0.0, "maximum": 1.0}
                            for action in STATIC_ACTION_ENUM
                        },
                        "selected": {"type": "string", "enum": list(STATIC_ACTION_ENUM)},
                        "decision_margin": {"type": "number"},
                        "evidence": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                # Scalar derived field for JSONB-containment queries (B4 populates).
                "is_soldier": {"type": "boolean"},
                # Prompt-native military judgment (plr_v1.4_cot). Gemma emits this
                # directly from camouflage / field-uniform / load-bearing cues.
                "military": {"type": "string", "enum": list(MILITARY_ENUM)},
                # Optional: details about the vehicle the person is riding.
                # Populated only when action is riding_*. Lets queries like
                # "빨간 오토바이 탄 남자" match on rider_vehicle_color/type
                # in addition to action=riding_motorcycle and gender=male.
                "rider_vehicle": {
                    "type": "object",
                    "required": [],
                    "properties": {
                        "color_topk": _topk_array_schema(COLOR_ENUM),
                        "type": {
                            "type": "string",
                            "enum": ["motorcycle", "bicycle", "scooter", "kickboard", "unknown"],
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
        "prompt_version": {"type": "string"},
    },
}


VEHICLE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["object_type", "attributes"],
    "properties": {
        "object_type": {"const": "vehicle"},
        "track_id": {"type": "string"},
        "visibility": {
            "type": "object",
            "properties": {
                "vehicle_visibility": {"type": "string", "enum": ["full", "partial"]},
                "occlusion": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                },
                "image_quality": {
                    "type": "string",
                    "enum": ["good", "fair", "poor"],
                },
                "quality_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "quality_warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "attributes": {
            "type": "object",
            "required": ["color_topk", "type_topk"],
            "properties": {
                "color_topk": _topk_array_schema(COLOR_ENUM),
                "type_topk": _topk_array_schema(VEHICLE_TYPE_ENUM),
                "evidence": {"type": "string"},
                # Scalar derived fields for JSONB-containment queries (set at index time).
                "primary_color": {"type": "string", "enum": list(COLOR_ENUM)},
                "is_military": {"type": "boolean"},
                # Prompt-native military judgment (plr_v1.4_cot). Gemma emits this
                # directly from camo paint / military body type / insignia cues.
                "military": {"type": "string", "enum": list(MILITARY_ENUM)},
            },
        },
        "prompt_version": {"type": "string"},
    },
}


# =====================================================================
# Validation
# =====================================================================


class SchemaValidationError(ValueError):
    """Raised when a PLR JSON does not match the expected schema."""


def validate_plr(data: dict[str, Any]) -> None:
    """파싱된 PLR dict를 스키마 검증. 실패 시 SchemaValidationError.

    jsonschema 패키지가 있으면 정식 검증, 없으면 최소 구조 확인으로 완화.
    호출부(indexing의 _plr_with_schema_retry)가 이 예외를 잡아 재시도 1회를
    돌린다 — 여기서 raise하는 것이 곧 "스키마-재시도 트리거"다.
    """
    obj_type = data.get("object_type")
    if obj_type == "person":
        schema = PERSON_SCHEMA
    elif obj_type == "vehicle":
        schema = VEHICLE_SCHEMA
    else:
        raise SchemaValidationError(
            f"object_type must be 'person' or 'vehicle', got {obj_type!r}"
        )

    try:
        import jsonschema  # type: ignore
    except ImportError:
        # Fallback minimal check
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            raise SchemaValidationError("attributes must be a dict")
        return

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise SchemaValidationError(f"PLR JSON invalid: {e.message}") from e


def is_valid_plr(data: dict[str, Any]) -> bool:
    """validate_plr의 불리언 래퍼 (예외 대신 True/False)."""
    try:
        validate_plr(data)
        return True
    except SchemaValidationError:
        return False
