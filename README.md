# hwabaek — 멀티 에이전트 오케스트레이션 시스템

여러 LLM 에이전트가 메시지 버스를 통해 **서로 직접 대화하며(자율 협업)** 사용자가 제출한
범용 태스크를 분담·협업 처리하는 시스템입니다. LLM API(tool use) 위에
오케스트레이션 계층을 직접 구현합니다.

> 이름의 유래: **화백(和白)** — 중앙 지시자 없이 구성원 간 논의로 결정하던
> 신라의 합의 회의체. 이 시스템의 자율 협업 패턴과 같은 구조입니다.

## 어떻게 동작하나

```
사용자 ──(브라우저)──> 웹 대시보드 ──REST/SSE──> 로컬 서버 (FastAPI)
                                                    │
                                              세션 매니저
                                                    │
                                       ┌──── 메시지 버스 (asyncio) ────┐
                                       │         │         │          │
                                    Agent A   Agent B   Agent C   (팀 설정으로 정의)
                                       └── 각자 LLM 툴 루프 실행 ────┘
```

1. 사용자가 대시보드에서 태스크를 제출하면 **세션**이 생성됩니다.
2. YAML 팀 설정에 정의된 에이전트들(예: 조사자/분석가/작성자)이 기동되어,
   `send_message` 도구로 서로 메시지를 주고받으며 협업합니다.
3. 협업 과정은 대시보드의 메시지 타임라인에 실시간(SSE)으로 표시됩니다.
4. 어느 에이전트가 `submit_result`로 초안을 제출하면 다른 에이전트들이 승인/반대를
   투표합니다(**화백 합의** — 기본은 생존 심의자 전원 승인의 엄밀한 만장일치, 미투표는
   승인으로 치지 않음). 승인되면 최종 결과와 함께 세션이 종료되고, 반려되면 사유와
   함께 논의가 재개됩니다(재제출은 버전이 오른 새 제안).

### 종료 안전장치

자율 협업 패턴의 최대 위험(무한 대화, 비용 폭증)을 시스템 차원에서 차단합니다.

- `submit_result` 제출 + 합의 승인 시 정상 종료 (투표 중에도 예산 상한 유지)
- 세션 총 메시지 수 상한 / 토큰 예산 초과 시 강제 종료
- 모든 에이전트 유휴 시 종료
- 대시보드에서 언제든 취소 가능 (취소 후 추가 API 호출 없음)

## 주요 특징 (계획)

- **자율 협업(메시지 패싱)**: 중앙 조율자 없이 에이전트 간 직접 메시지 교환
- **팀 구성 = 설정**: 에이전트의 역할·시스템 프롬프트·모델을 YAML로 정의 — 도메인 특화 팀은 설정 추가만으로 파생
- **실시간 관측성**: 세션별 메시지 타임라인, 에이전트 상태, 토큰 사용량/예산 게이지
- **비용 통제**: 세션 토큰 예산, 프롬프트 캐싱(고정 시스템 프롬프트), 에이전트별 모델 선택

## 팀 설정 (configs/*.yaml)

팀은 YAML 파일 하나로 정의합니다. 전체 스키마와 기본 팀(조사자/분석가/작성자)은
[configs/team.default.yaml](configs/team.default.yaml) 참조.

```yaml
name: default            # 팀 식별자 (소문자/숫자/_/-)
description: ...         # 선택
default_model: ...       # 선택 — 생략 시 계약 기본값(GPT-5.6 Terra)
termination:             # 종료 정책 (전부 선택)
  max_messages: 100      # 세션 메시지 상한
  token_budget: 200000   # 세션 토큰 예산
  idle_timeout: 30       # 전원 유휴 판정 시간(초) — running 전용
  approval:              # 화백 합의 설정 (문자열 축약형 `approval: unanimous`도 지원)
    mode: unanimous      # unanimous | majority | participating_unanimous | first
    timeout_seconds: 120 # 투표 대기 시간 — idle_timeout과 별개의 voting 전용 타이머
    minimum_votes: null  # participating_unanimous 전용 유효 투표 하한
agents:                  # 1명 이상
  - name: sangdaedeung   # 필수
    role: ...            # 필수 — 대시보드 표시용
    system_prompt: ...   # 필수
    model: ...           # 선택 — 에이전트별 오버라이드
    max_turns: 50        # 선택 — 에이전트당 LLM 호출 상한
    capabilities:        # 선택 — 생략 시 전체. 런타임이 권한 밖 호출을 거부 (D-027)
      - send_message     #   send_message | submit_result | vote_result
      - submit_result
```

기본 팀은 화백 컨셉의 **대등 3인 구조**입니다 — 조사 대등(research_daedeung),
견제 대등(critic_daedeung), 상대등(sangdaedeung, 진행·종합·제출). 상대등이 제출한
안을 나머지 두 대등이 모두 승인해야 의결됩니다.

허용되지 않은 키는 오타로 간주해 로드 시 즉시 거부됩니다(파일·필드 경로를 포함한
오류 메시지). 대시보드가 구독하는 이벤트 스트림 계약은
[docs/EventContract.md](docs/EventContract.md) 참조.

## 프로젝트 상태

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| M0 | 방향 결정, 기술 조사, 문서/계획 수립 | ✅ 완료 |
| M1 | 계약 확정 (메시지/에이전트/팀/세션 스키마) | ✅ 완료 |
| M2a | 코어 엔진 (버스/에이전트 루프/합의/종료 정책, 인메모리) | ✅ 완료 |
| M2b | 영속화(SQLite) + subscription OAuth 모드 + 실 API 스모크 | 예정 |
| M3 | 서버 (FastAPI REST + SSE) | 예정 |
| M4 | 웹 대시보드 | 예정 |
| M5 | 견고화 (실패 경로, E2E) | 예정 |
| M6 | 확장 실험 (외부 워커, 도구, 도트 월드 UI) | 후순위 |

상세 계획은 [docs/Plan.md](docs/Plan.md) 참조.

## 기술 스택

- Python 3.11+ / asyncio
- [openai SDK](https://github.com/openai/openai-python) — 기본 모델 GPT-5.6 Terra
  (에이전트별 오버라이드 가능)
- LLM 클라이언트는 프로바이더 중립 계약으로 추상화 — Anthropic 어댑터는 후순위
  ([docs/DecisionLog.md](docs/DecisionLog.md) D-008/D-009)
- FastAPI + uvicorn, SSE

## 실행 (콘솔 스모크, M2a)

```
# 밀폐 스모크 — 실키/네트워크 없이 전체 스택 확인
.venv\Scripts\python.exe -m hwabaek.run "your task" --fake

# 실제 실행 — OPENAI_API_KEY 필요 (기본 팀: configs/team.default.yaml)
.venv\Scripts\python.exe -m hwabaek.run "your task"
```

세션 이벤트가 콘솔에 실시간 출력되고, 종료 시 상태·결과(또는 미승인 초안)·토큰
사용량이 표시됩니다. 웹 대시보드는 M4에서 제공됩니다.

## 개발 환경 (Windows)

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests
```

- venv는 네이티브 Windows Python(3.11+, `py` 런처)으로 만듭니다 — git-bash의
  MSYS2 python은 venv 레이아웃이 달라 프로젝트 규칙과 어긋납니다 (DecisionLog D-014).

- 인증: `OPENAI_API_KEY` 환경변수(기본) 또는 ChatGPT subscription OAuth(선택 모드,
  M2b 예정 — 비공식 경로라 OpenAI 약관 변경 시 제거될 수 있습니다, D-026).
  API 키는 로그·대시보드에 노출되지 않습니다.
- 서버는 localhost(127.0.0.1) 전용이며, 세션은 동시에 1개만 실행됩니다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/ProjectContext.md](docs/ProjectContext.md) | 프로젝트 배경과 목표 |
| [docs/Plan.md](docs/Plan.md) | 구현 계획 (마일스톤) |
| [docs/DecisionLog.md](docs/DecisionLog.md) | 주요 의사결정과 근거 |
| [docs/Research.md](docs/Research.md) | 기술 조사 (API/패턴 비교) |
| [docs/IA.md](docs/IA.md) | 대시보드 화면 구조 |
| [docs/EventContract.md](docs/EventContract.md) | SSE 이벤트 계약 (payload 스키마) |
| [docs/UserScenarios.md](docs/UserScenarios.md) | 사용자 시나리오 |
| [docs/Personas.md](docs/Personas.md) | 사용자 페르소나 |
| [docs/Process.md](docs/Process.md) | 작업 방식/프로세스 |
| [docs/ReviewChecklist.md](docs/ReviewChecklist.md) | 완료 전 점검 목록 |
| [docs/WorkLog.md](docs/WorkLog.md) | 작업 진행 내역 |
