"""storage — 파일 저장 계층 (파일이 진실).

레이아웃 (볼륨 루트 = env EVAL_SERVER_DATA, 기본 ./server_data):
    datasets/<name>/{crops/, labels.jsonl, manifest.yaml, label_audit.jsonl}
    runs/<run_id>/{attributes.jsonl, surface/..., run_provenance.json,
                   meta.json, metrics.json}
    quarantine/<run_id>/   ← 기동 시 발견된 불완전(크래시) run
    db.sqlite

보안 계약:
  - tar 해제는 경로 탈출/심링크/디바이스 멤버 차단 + 해제 총량 상한
  - 업로드된 surface/의 py 파일은 **절대 import/실행하지 않는다** —
    저장·열람·diff 전용 (실행하면 원격 코드 실행). 채점은 서버 자신의
    lab 코드(evalkit.scoring)로만 한다.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import tarfile
from datetime import datetime
from pathlib import Path

# 해제 총량 상한 (기본 2GB) — tarbomb/디스크 고갈 방어
MAX_EXTRACT_BYTES = int(os.environ.get("EVAL_SERVER_MAX_EXTRACT_BYTES", 2 * 1024**3))


def data_root() -> Path:
    root = Path(os.environ.get("EVAL_SERVER_DATA", "./server_data")).resolve()
    for sub in ("datasets", "runs", "quarantine"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def new_run_id() -> str:
    """정렬 가능한 run id — r<YYYYMMDD-HHMMSS>-<6hex> (AC8 최신-run 뷰의 근거)."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"r{ts}-{secrets.token_hex(3)}"


class ExtractError(ValueError):
    """tar 해제 거부 사유 (경로 탈출/상한 초과 등) — API는 4xx로 번역."""


def safe_extract_targz(tar_path: str | Path, dest: str | Path,
                       max_bytes: int = MAX_EXTRACT_BYTES) -> int:
    """tar.gz를 dest에 안전 해제하고 해제 바이트 수를 반환.

    py3.12+의 filter='data'와 동등한 검증을 수동으로도 수행(하위 버전 대비):
    절대경로/.. 탈출/심링크·하드링크·디바이스 멤버 거부, 총량 상한.
    """
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(tar_path, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            name = m.name
            if name.startswith("/") or name.startswith("\\"):
                raise ExtractError(f"absolute path in archive: {name!r}")
            parts = Path(name).parts
            if ".." in parts:
                raise ExtractError(f"path traversal in archive: {name!r}")
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise ExtractError(f"forbidden member type: {name!r}")
            if m.isreg():
                total += m.size
                if total > max_bytes:
                    raise ExtractError(
                        f"archive exceeds extract cap ({max_bytes} bytes)")
            members.append(m)
        try:
            tf.extractall(dest, members=members, filter="data")  # py>=3.12
        except TypeError:  # filter 미지원 버전 — 위 수동 검증이 방어선
            tf.extractall(dest, members=members)
    return total


def surface_hash(bundle_dir: str | Path) -> str:
    """업로드된 표면 번들의 해시 — lab의 prompt_hash와 **같은 알고리즘**을
    번들 루트에 적용한다 (번들 = surface_relpaths와 같은 파일 집합이므로
    run 시점 지문과 직접 비교 가능 — 위조/누락 검증)."""
    from evalkit.provenance import prompt_hash

    return prompt_hash(bundle_dir)


def find_dataset_root(extracted: Path) -> Path | None:
    """해제 결과에서 데이터셋 루트 탐지 — manifest.yaml이 최상위 또는
    단일 하위 디렉터리에 있는 두 관례를 모두 수용."""
    if (extracted / "manifest.yaml").exists():
        return extracted
    subdirs = [d for d in extracted.iterdir() if d.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "manifest.yaml").exists():
        return subdirs[0]
    return None


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 손상 파일은 None (호출자가 격리 판단)
        return None


def reconcile_and_rebuild(root: Path, conn: sqlite3.Connection) -> dict:
    """기동 시 정합 복구 — 요청 수락 전에 완료되어야 한다 (startup 훅).

    DB는 파생 캐시: datasets/·runs/의 파일을 리플레이해 재구축하고,
    meta.json 또는 metrics.json이 없는/깨진 run 디렉터리(채점 중 크래시 잔재)는
    quarantine/으로 격리한다.
    """
    from evalkit.dataset import declared_attributes

    from server import db as _db

    rebuilt_runs, quarantined = [], []

    # datasets 재구축
    for ds_dir in sorted((root / "datasets").iterdir()) if (root / "datasets").is_dir() else []:
        if not ds_dir.is_dir():
            continue
        manifest = ds_dir / "manifest.yaml"
        if not manifest.exists():
            continue
        n_crops = len(list((ds_dir / "crops").glob("*.jpg"))) if (ds_dir / "crops").is_dir() else 0
        reg = read_json(ds_dir / "registered.json") or {}
        _db.upsert_dataset(conn, ds_dir.name,
                           reg.get("created_at", ""), reg.get("created_by", ""),
                           n_crops, declared_attributes(ds_dir))

    # runs 재구축 + 고아 격리
    for run_dir in sorted((root / "runs").iterdir()) if (root / "runs").is_dir() else []:
        if not run_dir.is_dir():
            continue
        meta = read_json(run_dir / "meta.json")
        metrics = read_json(run_dir / "metrics.json")
        if not meta or metrics is None:
            target = root / "quarantine" / run_dir.name
            shutil.move(str(run_dir), str(target))
            quarantined.append(run_dir.name)
            continue
        # metrics.json = {"attributes": {attr: res}, "aggregate": ...} 표준형;
        # 평면형({attr: res})도 수용 (구형/수동 배치 호환).
        per_attr = metrics.get("attributes") if isinstance(metrics.get("attributes"), dict) else metrics
        _db.upsert_run(conn, meta, per_attr)
        rebuilt_runs.append(run_dir.name)

    return {"runs": rebuilt_runs, "quarantined": quarantined}
