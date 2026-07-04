# 공유 계약 (Shared Contract) — lab ↔ eval-server 경계

eval-realign 2차(AC13)에서 평가 서버를 **별도 git 레포**(`~/plr-eval-server`)로
분리했다. 이 문서는 무엇이 **공유**이고 무엇이 각 레포 **전용**인지 파일 단위로 고정한다.

## 왜 "공유 코드 0"이 아닌가

lab 클라이언트의 `run`/`submit` 과 서버의 채점/렌더가 아래 파일들을 **둘 다** 쓴다:

- 서버는 `evalkit.provenance.prompt_hash` 로 업로드된 표면 번들 해시를 대조한다.
- 서버의 채점(`evalkit.scoring`)·갤러리(`evalkit.gallery`)가 `evalkit.dataset`
  (attribute_spec/resolve_json_path/load_labels)에 의존한다.
- 서버는 데이터셋 push 시 `evalkit.validate.validate_dataset` 로 형식을 검증하고,
  이는 `plr_schema` → `schema/vocab.yaml` 을 import 한다.
- lab 클라이언트도 같은 `dataset`/`provenance`/`validate` 를 run/submit 에서 쓴다.

따라서 이 파일들은 두 레포에 **byte-identical 복본**으로 vendored 된다.

## 공유 계약 파일 (두 레포에서 동일해야 함)

| 파일 | 왜 공유 |
|---|---|
| `evalkit/dataset.py` | 속성 스펙/예측 경로 추출/라벨 로드 — 채점·클라이언트 공용 |
| `evalkit/provenance.py` | `prompt_hash`/`surface_relpaths` — 서버 해시 대조 + 클라 표면 번들 |
| `evalkit/validate.py` | `validate_dataset` — 데이터셋 형식 검증(서버 push·클라 검증) |
| `plr_schema.py` | `validate.py` 가 import; vocab 로더 |
| `schema/vocab.yaml` | `plr_schema` 어휘 단일 원천 |

목록의 기계 판독 원천: [`contract/shared_files.py`](shared_files.py) 의 `SHARED_FILES`.

## 서버 전용 (server repo 에만)

- `evalkit/scoring.py` — 채점 코어 (`score`, `signal_stats`)
- `evalkit/report.py` — 트렌드 리포트 렌더
- `evalkit/gallery.py` — 오답 갤러리 렌더 (crops base64)
- `server/` — FastAPI 앱·DB·스토리지·렌더 어댑터·라우트

## lab 전용 (lab repo 에만 — 추론 표면, 서버는 안 씀)

- `plr_core.py` / `plr_prompts.py` / `plr_parse.py` / `preprocess.py`
- `prompts/**` / `configs/**`
- `runners/` (re_score·demo·client) / `lab.py` / `registry.py` / `gemma_model.py`

## 드리프트 방지

- `contract/manifest.json` — 공유 파일 각 sha256. **두 레포에서 동일**.
- `tests/test_contract_parity.py` — 로컬 공유 파일이 manifest 와 일치하는지 검증.
  두 레포가 각자 초록이고 manifest 가 같으면 → 공유 파일이 두 레포에서 동일(전이적).
- 공유 파일을 고칠 땐 **lab repo 가 원천**:
  1. `python3 contract/gen_manifest.py` — manifest 재생성
  2. `scripts/sync_contract.sh /path/to/plr-eval-server` — 파일+manifest 를 서버로 복사
  3. 두 레포에서 `pytest tests/test_contract_parity.py` 초록 확인
