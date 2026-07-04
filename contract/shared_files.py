"""공유 계약(shared contract) 파일 목록 — 단일 원천.

eval-realign 2차(AC13)에서 server 를 별도 레포로 분리했다. 완전한 "공유 코드 0"은
거짓이다: lab 클라이언트의 run/submit 과 서버의 채점/렌더가 아래 파일들을 **둘 다**
쓴다. 따라서 이 파일들은 두 레포에 **byte-identical 복본**으로 vendored 되고,
`contract/manifest.json`(양쪽 동일)과 `tests/test_contract_parity.py`(양쪽 존재)가
드리프트를 감지한다. 원천은 lab repo; 서버로의 복사는 `scripts/sync_contract.sh`.

server 전용(공유 아님): evalkit/{scoring,report,gallery}.py + server/.
lab 전용(공유 아님): plr_core/plr_prompts/plr_parse/preprocess + prompts/ + runners/
                     + lab.py + registry + gemma_model (추론 표면 — 서버는 안 씀).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 공유 계약 파일 (repo 루트 기준 상대경로). 순서 고정 — manifest 재현성.
#
# 서버는 채점(scoring→dataset)과 해시 대조(provenance)에만 이 둘을 쓴다.
# evalkit/validate.py + plr_schema.py + schema/vocab.yaml 은 라벨 어휘(enum)
# 검증용인데, 그 검증은 클라이언트 `lab validate-dataset` 소관이고 서버는 그것을
# 신뢰하므로(SPEC:41) 더 이상 공유가 아니라 **lab 전용**이다 — 서버는 push 시
# 구조 가드만 돌린다(plr_schema/vocab 를 vendoring 하지 않음).
SHARED_FILES: tuple[str, ...] = (
    "evalkit/dataset.py",       # attribute_spec/resolve_json_path/load_* — 채점·클라이언트 공용
    "evalkit/provenance.py",    # prompt_hash/surface_relpaths — 서버 해시 대조에 필요
)


def sha256_of(root: Path, rel: str) -> str:
    """root/rel 파일의 sha256 hex (전체, 64자)."""
    return hashlib.sha256((root / rel).read_bytes()).hexdigest()


def compute_manifest(root: Path) -> dict[str, str]:
    """SHARED_FILES -> {relpath: sha256} (manifest.json 의 files 블록)."""
    return {rel: sha256_of(root, rel) for rel in SHARED_FILES}
