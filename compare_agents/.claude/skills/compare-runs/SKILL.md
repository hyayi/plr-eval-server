---
name: compare-runs
description: 두 eval-server run을 비교하고 성능 차이에 대한 이유를 여러 에이전트가 단계별로 수행하여 설명하는 파이프라인. '{A}와 {B}를 비교' 등 두 run에 대한 비교에 대해 이 스킬을 사용한다. 비교를 위해서 로컬 데이터 볼륨을 직접 읽기 때문에 서버 기동 불필요하다.
user-invocable: true
---

# compare-runs

**"두 run의 성능·프롬프트를 나란히 놓고, 왜 이 성능이 나왔는가."** 에 답하는 파이프라인. 증거는 **원인 → 결과 → 메커니즘** 순으로 흐르고, 마지막에 추론 에이전트가 "왜"를 종합한다. 각 단계는 `_workspace/<a>__<b>/`에 번호 붙은 마크다운 산출물을 남긴다.

> 이 파일은 **조율(순서·넘길 입력·게이트·산출물)만** 담는다. *어떻게* 계산하는지, 산출물 포맷이 무엇인지 등 세부는 각 `agents/<이름>.md`가 단일 원천이다 — 여기 복붙하지 말 것(드리프트 방지).

## 불변식 (subagent 프롬프트에 실어 보낼 것)
1. **파일이 진실** — 서버 기동 불필요. 로컬 데이터 볼륨(`EVAL_SERVER_DATA`)의 파일을 읽는다.
2. **업로드된 표면 `.py`는 절대 실행 금지** (RCE-safe; 저장·열람·diff 전용). 표면(`surface/`)을 읽는 에이전트(= eval-prompt-differ)를 띄울 때 이 문장을 task 프롬프트에 포함시켜라.
3. **지표 단일 원천** — 예측 추출/채점은 `evalkit`을 재사용한다(서버 채점과 byte-identical). 재구현하지 않는다.

## 에이전트 구성
| 에이전트 | 역할 | Model |
|---|---|---|
| eval-run-fetcher | run 정보 수집 및 비교 유효성 판정 | sonnet |
| eval-metric-differ | 두 run 지표 델타 + 예측 플립 | sonnet |
| eval-prompt-differ | 두 run 프롬프트·파라미터 diff | sonnet |
| eval-why-analyst | 원인 분석·종합 | opus |

> 각 에이전트의 입출력·방법은 `.claude/agents/<이름>.md` 참조. 호출은 Agent 툴로 **같은 이름의 `subagent_type`** 을 쓴다(범용 `general-purpose` 아님).

## 인자
`/compare-runs <RUN_A> <RUN_B>`
- `RUN_A`, `RUN_B` — run id(`r2026...-hex`).

## Workflow

### Phase 0 — pre-flight (오케스트레이터가 직접 수행)
하위 에이전트를 띄우기 **전에** 여기서 처리한다. 폴더 이름과 충돌은 오케스트레이터의 책임이다.
1. `DATA_ROOT`를 아래 순서로 해석한다.
   1. `$EVAL_SERVER_DATA`로 해석하고, 만약 환경 변수가 미설정인 경우 어디를 찾아야하는지 알리고 멈춘다.
   2. 위에서 나온 경로에 `runs/` 하위 디렉터리가 없으면 어디를 찾았는지 알리고 멈춘다.
2. **run id 확정**: `RUN_A`/`RUN_B`가 `runs/<id>/`에 디렉터리로 존재하면 사용, 없으면 '해당 run 없음' 에러로 멈춘다.
3. **워크스페이스 경로**: `WORKSPACE_DIR = _workspace/<확정_a>__<확정_b>` (repo 루트 기준, 구분자는 더블 언더스코어). 폴더명은 원시 인자가 아니라 **확정된 run id**로 잡는다.
4. **충돌 검사**: `WORKSPACE_DIR`가 이미 있으면 사용자에게 **반드시 알리고** AskUserQuestion으로 묻는다 — 덮어쓰기 / 기존 결과 재사용 / 취소.
   - 덮어쓰기 → 폴더를 비우고 진행.
   - 재사용 → Phase 1~3을 건너뛰고 기존 `00~03.md`를 읽어 Phase 4(제시)만 수행. 단 `00~03.md` 중 하나라도 없으면 사용자에게 알리고 덮어쓰기로 처리.
   - 취소 → 멈춘다.
5. `WORKSPACE_DIR`가 없으면 생성한다.

### Phase 1~4 — 실행
아래 표대로 에이전트를 띄운다. **넘길 입력**은 오케스트레이터가 조립해 건네는 값, **산출물**은 그 에이전트가 `WORKSPACE_DIR`에 쓰는 파일이다(내부 포맷은 agent md 소관).

| Phase | 담당 | 넘길 입력 | 제어·게이트 | 산출물 |
|---|---|---|---|---|
| 1 | eval-run-fetcher | DATA_ROOT · 확정 RUN_A/RUN_B · WORKSPACE_DIR | blocking — 반환한 `same_dataset`/`dataset` 대기 | `00_run_summary.md` |
| 2a | eval-metric-differ | + DATASET(= Phase 1 반환 `dataset`; `same_dataset=true`일 때만 유효) · same_dataset | `same_dataset=false` → 크롭별 플립 생략 | `01_metric_diff.md` |
| 2b | eval-prompt-differ | (기본 입력) + **불변식② 주입** | 2a와 **병렬·독립** | `02_prompt_diff.md` |
| 3 | eval-why-analyst | WORKSPACE_DIR (`00~02`를 읽음) | blocking — `00~02` 존재 확인 | `03_analysis_result.md` |
| 4 | 오케스트레이터 | — | `03` 핵심 제시 + 산출물 위치 안내 | (사용자) |

- **2a·2b는 한 메시지에서 병렬로** 띄운다(둘 다 Phase 1에만 의존).
- **2a·2b 완료 후 Phase 3를** 실행한다.
- **소통 매개 = 파일(`_workspace/`) + 오케스트레이터.** 에이전트끼리 직접 통신하지 않는다 — why-analyst도 전달받는 게 아니라 `00~02`를 **읽어** 추론한다(단계 분리 → 자기검토 편향 방지).

## 규칙
- **가짜 완료 방지**: 에이전트가 에러·파일 누락·빈 증거를 반환하면 숫자를 지어내지 말고 그대로 드러내라. 어떤 Phase의 산출 파일이 안 만들어졌으면 다음 Phase로 넘어가지 말고 원인을 보고하라. 작은 `n`·`hash_verified:false`는 `03`의 주의 사항으로 반드시 도달시킨다.
- **데이터셋이 다르면**(`same_dataset:false`): metric-differ가 크롭별 플립을 건너뛴다. 지표 델타·프롬프트 diff는 그대로 내되, 플립이 생략된 이유를 사용자에게 알린다.
