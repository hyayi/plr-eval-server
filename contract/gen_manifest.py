#!/usr/bin/env python3
"""contract/manifest.json 을 SHARED_FILES 의 현재 해시로 재생성한다.

원천 레포(lab)에서 공유 파일을 고친 뒤 이 스크립트로 manifest 를 갱신하고,
`scripts/sync_contract.sh <server-root>` 로 파일+manifest 를 서버 레포에 복사한다.

Usage: python3 contract/gen_manifest.py            # 이 레포 루트 기준
       python3 contract/gen_manifest.py <root>     # 명시 루트
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared_files import SHARED_FILES, compute_manifest  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
        else Path(__file__).resolve().parent.parent
    manifest = {
        "note": "공유 계약 파일의 sha256 — 두 레포(lab/server)에서 byte-identical 이어야 함. "
                "gen_manifest.py 로 재생성, sync_contract.sh 로 서버에 전파.",
        "files": compute_manifest(root),
    }
    out = Path(__file__).resolve().parent / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"[gen_manifest] {out} — {len(manifest['files'])} shared files")
    for rel, h in manifest["files"].items():
        print(f"  {h[:12]}  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
