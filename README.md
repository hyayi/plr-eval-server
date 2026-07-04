# plr-eval-server

PLR 평가 서버 — 채점·리더보드·report/gallery 렌더·버전관리.

lab 레포(`../plr-prompt-lab`)의 `lab submit` 클라이언트가 `attributes.jsonl` +
표면 번들(surface .tgz)을 업로드하면, 서버가 채점·지표 저장·HTML 렌더까지 수행한다.
업로드된 surface `.py` 파일은 **절대 import/실행하지 않는다** (RCE-safe; 저장·열람·diff 전용).

---

## 실행

```bash
pip install -r requirements.txt
uvicorn server.app:app --host 0.0.0.0 --port 8890 --workers 1
```

### 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `EVAL_SERVER_DATA` | `./server_data` | 데이터셋·run 파일이 저장될 볼륨 경로 |
| `EVAL_SERVER_TOKEN` | (없음 → 인증 없음) | 변이 API(POST/PATCH)의 `X-Auth-Token` 값 |

### Docker

```bash
# server/Dockerfile 사용
docker build -t plr-eval-server -f server/Dockerfile .
docker run -p 8890:8890 \
  -e EVAL_SERVER_DATA=/data \
  -e EVAL_SERVER_TOKEN=your-token \
  -v /your/data:/data \
  plr-eval-server
```

`server/docker-compose.example.yml` 을 참고해 볼륨·환경 변수를 설정하세요.

---

## 테스트

```bash
python3 -m pytest tests/ -q
```

PYTHONPATH 설정 불필요 — 이 레포가 self-contained 이므로 `tests/` 가 자동으로 루트를 경로에 추가합니다.

---

## 공유 계약 (shared contract)

`contract/CONTRACT.md` 참조.

`evalkit/dataset.py`, `evalkit/provenance.py` 두 파일만 lab 레포와
**byte-identical 복본**으로 관리됩니다 (채점·해시 대조에 필요).

라벨 어휘(enum) 검증(`validate.py`/`plr_schema.py`/`vocab.yaml`)은 **vendoring 하지
않습니다** — 그 검증은 클라이언트 `lab validate-dataset` 소관이고 서버는 그것을
신뢰합니다(SPEC:41). 서버는 데이터셋 push 시 구조 가드(manifest 파싱·labels.jsonl·
crops 존재)만 돌립니다.

- 원천: `../plr-prompt-lab` (lab 레포)
- 드리프트 감지: `tests/test_contract_parity.py` (양쪽 레포 동일 존재)
- 갱신 절차: lab 레포에서 공유 파일 수정 →
  `python3 contract/gen_manifest.py` →
  `scripts/sync_contract.sh <server-root>` 로 이 레포에 전파

공유 파일을 이 레포에서 직접 수정하지 마세요 — 다음 sync 때 덮어씌워집니다.
