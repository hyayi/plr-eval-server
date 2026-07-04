"""공유 계약 파리티 (AC13/P2-3): 이 레포의 공유 파일이 contract/manifest.json 과
byte-identical 인지 검증.

두 레포(lab/server)가 각자 이 테스트 + 동일 manifest.json 을 가지므로, 양쪽이 초록이면
공유 파일이 두 레포에서 동일하다는 뜻이다(전이적 파리티). 공유 파일을 고쳤는데 manifest
를 재생성 안 하면(=드리프트) 여기서 빨간불 → `python3 contract/gen_manifest.py` +
`scripts/sync_contract.sh <server-root>` 를 상기시킨다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "contract"))

from shared_files import SHARED_FILES, sha256_of  # noqa: E402

_MANIFEST = _ROOT / "contract" / "manifest.json"


def test_manifest_exists_and_covers_all_shared_files() -> None:
    assert _MANIFEST.exists(), "contract/manifest.json 없음 — gen_manifest.py 실행"
    files = json.loads(_MANIFEST.read_text(encoding="utf-8"))["files"]
    assert set(files) == set(SHARED_FILES), (
        f"manifest 파일 집합 불일치: manifest={sorted(files)} "
        f"shared={sorted(SHARED_FILES)}")


def test_shared_files_match_manifest_hashes() -> None:
    files = json.loads(_MANIFEST.read_text(encoding="utf-8"))["files"]
    drift = []
    for rel in SHARED_FILES:
        actual = sha256_of(_ROOT, rel)
        if actual != files[rel]:
            drift.append(f"{rel}: manifest={files[rel][:12]} actual={actual[:12]}")
    assert not drift, (
        "공유 계약 드리프트 — 공유 파일 변경 후 manifest 미갱신:\n  "
        + "\n  ".join(drift)
        + "\n→ `python3 contract/gen_manifest.py` 후 `scripts/sync_contract.sh`")
