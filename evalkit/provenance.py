"""provenance — "이 실험 결과는 정확히 이 인풋 상태에서 나왔다"의 보증 장치.

prompt_hash(): 인풋 표면 전체(prompts/**·schema/*.yaml·configs/*.yaml·
plr_prompts/plr_parse/plr_core/plr_schema/preprocess.py)의 sha256 접두 —
ledger의 모든 기록에 찍혀서, knob 하나만 바꿔도 다른 해시가 남는다.
read_seed_hash()/warn_stale_seed(): SEED.md의 core/ir 기준 해시로
"lab 측정이 어느 운영 시점 기준인지"를 추적 (다르면 stale 경고).

(원문) stable content hashes of the active prompt surface.

The "prompt surface" is the same true-surface set that ``lab port`` diffs
against core/ir: prompts/*.yaml + schema/*.yaml (declarative vocabulary) +
plr_prompts.py / plr_core.py / plr_schema.py / preprocess.py — everything
that shapes the model's input bytes and output parsing (defined once in
``surface_relpaths`` and reused by both).
``prompt_hash()`` returns a short, stable sha256 prefix of that content so every
ledger writer (``eval/run_eval.py``) and any reporter
stamps the SAME hash for a given prompt state — letting a ledger record be tied
back to the exact prompt bytes that produced it.

GPU-free and dependency-light (stdlib only): safe to import from any runner.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_LAB_ROOT = Path(__file__).resolve().parent.parent  # lab root (evalkit/ is one below)

# Files that make up the active prompt surface, relative to the lab root.
# This is the SINGLE SOURCE OF TRUTH shared by prompt_hash() and lab port
# (lab.py builds its diff set from surface_relpaths()): every prompts/*.yaml
# plus the code that shapes the model output — plr_prompts.py (prompt
# construction) and plr_core.py (target marker + response parsing). A change to
# any of these can change predictions, so all are tracked in the ledger hash and
# surfaced by lab port. Keep this list and lab port in sync via this function.
_PROMPTS_DIR = "prompts"
_SCHEMA_DIR = "schema"
_SURFACE_PY = ("plr_prompts.py", "plr_parse.py", "plr_core.py", "plr_schema.py", "preprocess.py")


def surface_relpaths(
    lab_root: str | os.PathLike[str] | None = None,
    include_exp_configs: bool = False,
    version: str | None = None,
) -> list[str]:
    """Relative paths of the existing prompt-surface files, deterministic order.

    Single source of truth for both prompt_hash() and lab port. Sorted by
    relative path so callers are independent of directory-listing order.

    ``include_exp_configs=True`` adds configs/*.yaml (experiment parameter
    configs) — used by prompt_hash so a knob change re-stamps provenance.
    ``lab port`` keeps the default False: experiment configs are lab-only
    and have no core/ir counterpart to diff against.

    ``version`` scopes the prompt surface to a single version directory
    (``prompts/<version>/**``) instead of every version. A run only uses one
    prompt version, so its surface_hash / bundle should cover only that
    version — otherwise editing an unused experimental version would perturb
    this run's fingerprint. Default None = all versions (``lab port`` keeps
    this: it diffs the whole prompt surface against core/ir).
    """
    root = Path(lab_root) if lab_root else _LAB_ROOT
    rels: list[str] = []
    prompts_dir = root / _PROMPTS_DIR
    if version is not None:
        prompts_dir = prompts_dir / version
    if prompts_dir.is_dir():
        rels.extend(sorted(str(p.relative_to(root)) for p in prompts_dir.rglob("*.yaml")))
    schema_dir = root / _SCHEMA_DIR
    if schema_dir.is_dir():
        rels.extend(sorted(str(p.relative_to(root)) for p in schema_dir.glob("*.yaml")))
    if include_exp_configs:
        configs_dir = root / "configs"
        if configs_dir.is_dir():
            rels.extend(sorted(str(p.relative_to(root)) for p in configs_dir.glob("*.yaml")))
    for py in _SURFACE_PY:
        if (root / py).exists():
            rels.append(py)
    return rels


def _surface_paths(lab_root: str | os.PathLike[str] | None = None,
                   version: str | None = None) -> list[Path]:
    """Absolute paths of the hashed surface (prompts + experiment configs)."""
    root = Path(lab_root) if lab_root else _LAB_ROOT
    return [root / rel for rel in
            surface_relpaths(root, include_exp_configs=True, version=version)]


def read_seed_hash(lab_root: str | os.PathLike[str] | None = None) -> str | None:
    """Read the core/ir HEAD recorded in SEED.md (None if absent).

    Moved here from run_search_eval.py when the search pipeline was removed —
    seed provenance is used by `lab port` (stale-seed warning) and run_eval.
    """
    root = Path(lab_root) if lab_root else _LAB_ROOT
    seed_md = root / "SEED.md"
    if not seed_md.exists():
        return None
    with open(seed_md, encoding="utf-8") as f:
        for line in f:
            # Matches: `- Source `core/ir HEAD`: `<hash>``
            if "core/ir HEAD" in line and "`" in line:
                parts = line.split("`")
                if len(parts) >= 3:
                    candidate = parts[-2].strip()
                    if len(candidate) >= 7:
                        return candidate
    return None


def warn_stale_seed(
    lab_root: str | os.PathLike[str] | None,
    seed_hash: str | None,
    core_ir_path: str | None,
) -> None:
    """Print a stderr warning if the live core/ir HEAD != SEED.md hash."""
    import subprocess
    import sys

    if seed_hash is None:
        return
    ir_path = core_ir_path or os.environ.get("CORE_IR_PATH")
    if not ir_path or not os.path.isdir(os.path.join(ir_path, ".git")):
        return
    try:
        result = subprocess.run(
            ["git", "-C", ir_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        live_head = result.stdout.strip()
        if live_head and live_head != seed_hash:
            print(
                f"WARNING: core/ir HEAD ({live_head[:12]}) != SEED.md hash "
                f"({seed_hash[:12]}) — Δ may not be comparable (stale seed).",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001
        pass


def prompt_hash(
    lab_root: str | os.PathLike[str] | None = None,
    length: int = 12,
    version: str | None = None,
) -> str:
    """Return a short stable sha256 hash of the active prompt surface content.

    Hashes ``prompts/*.yaml`` + ``plr_prompts.py`` + ``plr_core.py`` (the same
    true-surface files ``lab port`` diffs, via ``surface_relpaths``).  Each file
    contributes its relative path plus its raw bytes, so both a content edit and
    a rename change the hash.

    Args:
      lab_root: repo root to hash (default: this module's directory).
      length:   number of leading hex chars to return (default: 12).

    Returns:
      The first ``length`` hex characters of the sha256 digest, or a hash of
      the empty surface if no prompt files exist (still deterministic).
    """
    root = Path(lab_root) if lab_root else _LAB_ROOT
    h = hashlib.sha256()
    for path in _surface_paths(root, version=version):
        rel = str(path.relative_to(root))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:length]
