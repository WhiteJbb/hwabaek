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
4. 어느 에이전트가 `submit_result`를 호출하면 최종 결과와 함께 세션이 종료됩니다.

### 종료 안전장치

자율 협업 패턴의 최대 위험(무한 대화, 비용 폭증)을 시스템 차원에서 차단합니다.

- `submit_result` 도구 호출 시 정상 종료
- 세션 총 메시지 수 상한 / 토큰 예산 초과 시 강제 종료
- 모든 에이전트 유휴 시 종료
- 대시보드에서 언제든 취소 가능 (취소 후 추가 API 호출 없음)

## 주요 특징 (계획)

- **자율 협업(메시지 패싱)**: 중앙 조율자 없이 에이전트 간 직접 메시지 교환
- **팀 구성 = 설정**: 에이전트의 역할·시스템 프롬프트·모델을 YAML로 정의 — 도메인 특화 팀은 설정 추가만으로 파생
- **실시간 관측성**: 세션별 메시지 타임라인, 에이전트 상태, 토큰 사용량/예산 게이지
- **비용 통제**: 세션 토큰 예산, 프롬프트 캐싱(고정 시스템 프롬프트), 에이전트별 모델 선택

## 프로젝트 상태

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| M0 | 방향 결정, 기술 조사, 문서/계획 수립 | ✅ 완료 |
| M1 | 계약 확정 (메시지/에이전트/팀/세션 스키마) | 예정 |
| M2 | 코어 엔진 (메시지 버스, 에이전트 루프, 종료 정책) | 예정 |
| M3 | 서버 (FastAPI REST + SSE) | 예정 |
| M4 | 웹 대시보드 | 예정 |
| M5 | 견고화 (실패 경로, E2E) | 예정 |

상세 계획은 [docs/Plan.md](docs/Plan.md) 참조.

## 기술 스택

- Python 3.11+ / asyncio
- [openai SDK](https://github.com/openai/openai-python) — 기본 모델 GPT-5.6 Terra
  (에이전트별 오버라이드 가능)
- LLM 클라이언트는 프로바이더 중립 계약으로 추상화 — Anthropic 어댑터는 후순위
  ([docs/DecisionLog.md](docs/DecisionLog.md) D-008/D-009)
- FastAPI + uvicorn, SSE

## 개발 환경 (Windows)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # (M1에서 추가 예정)
.venv\Scripts\python.exe -m unittest discover -s tests
```

- `OPENAI_API_KEY` 환경변수 또는 ChatGPT subscription 연동(M2 착수 전 검증 예정).
  API 키는 로그·대시보드에 노출되지 않습니다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/ProjectContext.md](docs/ProjectContext.md) | 프로젝트 배경과 목표 |
| [docs/Plan.md](docs/Plan.md) | 구현 계획 (마일스톤) |
| [docs/DecisionLog.md](docs/DecisionLog.md) | 주요 의사결정과 근거 |
| [docs/Research.md](docs/Research.md) | 기술 조사 (API/패턴 비교) |
| [docs/IA.md](docs/IA.md) | 대시보드 화면 구조 |
| [docs/UserScenarios.md](docs/UserScenarios.md) | 사용자 시나리오 |
| [docs/Personas.md](docs/Personas.md) | 사용자 페르소나 |
| [docs/Process.md](docs/Process.md) | 작업 방식/프로세스 |
| [docs/ReviewChecklist.md](docs/ReviewChecklist.md) | 완료 전 점검 목록 |
| [docs/WorkLog.md](docs/WorkLog.md) | 작업 진행 내역 |
