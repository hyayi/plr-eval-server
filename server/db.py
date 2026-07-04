"""db — SQLite 인덱스 계층 (파일이 진실, DB는 리플레이로 재구축 가능한 파생 캐시).

동시성 계약 (.omc/plans/plr-eval-server.md):
  - WAL + busy_timeout=5000
  - 프로세스 내 단일 쓰기 락(WRITE_LOCK) — POST /runs 채점·적재와 라벨 정정
    재채점이 같은 락 아래 직렬화된다. uvicorn은 --workers 1 고정(다중 프로세스
    금지 — 락이 프로세스 내 asyncio.Lock이므로).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

WRITE_LOCK = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL DEFAULT '',
    n_crops     INTEGER NOT NULL DEFAULT 0,
    attrs_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    dataset           TEXT NOT NULL,
    version_label     TEXT NOT NULL,
    surface_hash      TEXT NOT NULL DEFAULT '',
    hash_verified     INTEGER NOT NULL DEFAULT 0,
    submitter_lab_sha TEXT NOT NULL DEFAULT '',
    git_dirty         INTEGER NOT NULL DEFAULT 0,
    model             TEXT NOT NULL DEFAULT '',
    max_tokens        INTEGER,
    temperature       REAL,
    reason_toggle     TEXT NOT NULL DEFAULT '',
    submitted_by      TEXT NOT NULL DEFAULT '',
    submitted_at      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'scored'
);
CREATE INDEX IF NOT EXISTS idx_runs_dataset ON runs(dataset);
CREATE TABLE IF NOT EXISTS metrics (
    run_id    TEXT NOT NULL,
    attribute TEXT NOT NULL,
    metric    TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (run_id, attribute, metric)
);
CREATE TABLE IF NOT EXISTS label_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset       TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    changed_by    TEXT NOT NULL DEFAULT '',
    diff_summary  TEXT NOT NULL DEFAULT '',
    rescored_runs TEXT NOT NULL DEFAULT '[]'
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """WAL·busy_timeout이 설정된 연결. row_factory=Row."""
    # check_same_thread=False: FastAPI의 동기 핸들러는 스레드풀에서 돈다.
    # 쓰기는 WRITE_LOCK으로 직렬화되고 읽기는 WAL이라 스레드 공유가 안전.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def upsert_run(conn: sqlite3.Connection, meta: dict, metrics: dict[str, dict]) -> None:
    """runs + metrics 적재 (metrics: {attribute: score()결과 dict}).

    metrics 테이블에는 정렬 가능한 스칼라만 평면화 — 구조 필드(confusion,
    margin_stats, bias)는 파일(metrics.json)이 원본이다.
    """
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, dataset, version_label, surface_hash, hash_verified,
            submitter_lab_sha, git_dirty, model, max_tokens, temperature,
            reason_toggle, submitted_by, submitted_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (meta["run_id"], meta["dataset"], meta["version_label"],
         meta.get("surface_hash", ""), int(bool(meta.get("hash_verified"))),
         meta.get("submitter_lab_sha", ""), int(bool(meta.get("git_dirty"))),
         meta.get("model", ""), meta.get("max_tokens"), meta.get("temperature"),
         str(meta.get("reason_toggle", "")), meta.get("submitted_by", ""),
         meta["submitted_at"], meta.get("status", "scored")),
    )
    conn.execute("DELETE FROM metrics WHERE run_id=?", (meta["run_id"],))
    for attribute, res in metrics.items():
        flat = {
            "accuracy": res.get("accuracy"),
            "macro_f1": res.get("macro_f1"),
            "n": res.get("n"),
            "pred_unknown_rate": (res.get("pred_unknown") or {}).get("rate"),
            "bias_rate": (res.get("bias") or {}).get("rate"),
        }
        for metric, value in flat.items():
            if value is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO metrics (run_id, attribute, metric, value) "
                    "VALUES (?,?,?,?)",
                    (meta["run_id"], attribute, metric, float(value)),
                )
    conn.commit()


def upsert_dataset(conn: sqlite3.Connection, name: str, created_at: str,
                   created_by: str, n_crops: int, attributes: list[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO datasets (name, created_at, created_by, n_crops, attrs_json) "
        "VALUES (?,?,?,?,?)",
        (name, created_at, created_by, n_crops, json.dumps(attributes, ensure_ascii=False)),
    )
    conn.commit()


def add_label_audit(conn: sqlite3.Connection, dataset: str, changed_at: str,
                    changed_by: str, diff_summary: str, rescored_runs: list[str]) -> None:
    conn.execute(
        "INSERT INTO label_audit (dataset, changed_at, changed_by, diff_summary, rescored_runs) "
        "VALUES (?,?,?,?,?)",
        (dataset, changed_at, changed_by, diff_summary,
         json.dumps(rescored_runs, ensure_ascii=False)),
    )
    conn.commit()
