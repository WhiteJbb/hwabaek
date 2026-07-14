# Plan — 구현 계획

> 규칙: 구현 전 이 문서를 갱신하고, 이 계획에 따라 구현한다.
> 방향 결정 근거는 [DecisionLog.md](DecisionLog.md), 기술 조사는 [Research.md](Research.md) 참조.

## 시스템 개요

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

- **세션**: 사용자가 태스크 1건을 제출하면 세션이 생성되고, 팀 설정에 정의된 에이전트들이
  기동되어 메시지 버스로 협업한다. 결과 제출 또는 종료 조건 도달 시 세션이 끝난다.
- **에이전트**: 이름/역할(시스템 프롬프트)/모델을 가진 독립 LLM 툴 루프.
  기본 모델은 GPT-5.6 Terra(D-008), LLM 클라이언트는 프로바이더 중립 계약(D-009).
  `send_message`(다른 에이전트 또는 브로드캐스트), `submit_result`(최종 결과 제출) 도구를 가진다.
- **종료 제어** (자율 협업 패턴의 핵심 안전장치):
  1. 어느 에이전트든 `submit_result` 호출 → 세션 정상 종료
  2. 세션 총 메시지 수 상한 초과 → 강제 종료 (failed: budget)
  3. 세션 토큰 예산 초과 → 강제 종료 (failed: budget)
  4. 모든 에이전트 유휴(보낼 메시지 없음) → 종료 (idle)

## 디렉터리 구조 (목표)

```
src/hwabaek/
  contracts.py        # M1: 메시지/에이전트/팀/세션 스키마 (dataclass + 검증)
  bus.py              # M2: 비동기 메시지 버스 (에이전트별 인박스)
  agent.py            # M2: 에이전트 런타임 (LLM 툴 루프)
  session.py          # M2: 세션 수명주기 + 종료 정책 + 사용량 집계
  llm/
    base.py           # M1: LLM 클라이언트 계약 (프로바이더 중립 Protocol) — D-009
    openai_client.py  # M2: openai SDK 어댑터 (재시도/스트리밍/캐싱/키 마스킹)
    anthropic_client.py  # 후순위: anthropic SDK 어댑터
  server/
    app.py            # M3: FastAPI 조립
    api.py            # M3: REST (세션 생성/조회/취소, 팀 설정 조회)
    events.py         # M3: SSE 이벤트 스트림
  dashboard/          # M4: 정적 웹 UI (HTML/JS, FastAPI가 서빙)
configs/
  team.default.yaml   # 기본 팀 구성 (범용: 조사자/분석가/작성자 등)
tests/                # 단위 + 통합 (Fake LLM 클라이언트로 밀폐)
```

패키지 이름은 `hwabaek`으로 확정(D-010) — 화백(和白), 신라의 만장일치 합의 회의체.

## 마일스톤

### M0 — 프로젝트 초기화 (완료: 2026-07-14)
- 필수 문서 세트 생성, 방향 결정 확정.

### M1 — 계약 확정 (계약 우선 원칙)
- `contracts.py`: `Message`(id/session_id/sender/recipients/type/content/created_at),
  `AgentSpec`(name/role/system_prompt/model/max_turns),
  `TeamConfig`(agents/termination: max_messages/token_budget/idle_timeout),
  `Session`(id/task/status/result/usage).
- `llm/base.py`: LLM 클라이언트 계약(프로바이더 중립 Protocol) 확정 — 요청/응답/사용량/
  도구 호출 표현을 프로바이더 특이사항 없이 정의 (D-009).
- 팀 설정 YAML 스키마 확정 + 로더 + 검증 오류 메시지.
- SSE 이벤트 계약(대시보드가 구독할 이벤트 타입 목록) 문서화.
- **완료 기준**: 스키마 단위 테스트 통과. 이후 모듈은 이 계약 위에서 병렬 구현 가능.

### M2 — 코어 엔진 (서버 없이 동작)
- `bus.py`: asyncio 기반 인박스/브로드캐스트, 관측용 이벤트 훅.
- **착수 전 스파이크**: ChatGPT subscription 연동(Sign in with ChatGPT OAuth, BYOS)으로
  자체 앱에서 GPT-5.6 호출이 가능한지 검증 (D-008 전제). 불가 판명 시
  `OPENAI_API_KEY` 과금으로 폴백하고 DecisionLog D-008 갱신.
- `llm/openai_client.py`: openai SDK 어댑터 — 기본 모델 GPT-5.6 Terra, 스트리밍,
  프롬프트 캐싱(시스템 프롬프트 고정 + messages 뒤에 추가, 명시적 cache breakpoint),
  재시도, 키 마스킹. 프로바이더 특이사항(파라미터/stop 사유 처리)은 어댑터 내부에 격리.
  (anthropic 어댑터는 후순위 — adaptive thinking, `refusal` stop_reason 등도 어댑터에 격리)
- `agent.py`: 인박스 대기 → 새 메시지 수신 시 LLM 호출 → `send_message`/`submit_result`
  tool_use 처리 → 반복. 에이전트별 대화 이력 유지.
- `session.py`: 에이전트 기동/종료, 종료 정책 4종 강제, 토큰 사용량 집계.
- 최소 실행 스크립트(`python -m hwabaek.run "태스크"`)로 콘솔에서 스모크 확인.
- **완료 기준**: Fake LLM 테스트(밀폐) + 실 API 스모크 1회. 실패 경로 테스트
  (토큰 예산 초과 / 메시지 상한 / API 오류 / refusal) 포함 — "실패 경로가 제품이다".

### M3 — 서버
- REST: `POST /sessions`(태스크 제출), `GET /sessions/{id}`, `POST /sessions/{id}/cancel`,
  `GET /teams`(팀 설정 목록).
- SSE: `GET /sessions/{id}/events` — 메시지/상태/사용량 이벤트 실시간 스트림.
- **완료 기준**: curl 기반 통합 스모크. 요청 경로에 블로킹 I/O 없음 확인.

### M4 — 웹 대시보드
- 화면: 태스크 제출, 세션 목록, 세션 상세(에이전트별 레인 + 메시지 타임라인, 상태/사용량).
- IA.md의 화면 구조를 따른다.
- **완료 기준**: 신규 사용자 워크스루 렌즈로 리뷰.

### M5 — 견고화
- 취소/타임아웃/서버 재시작 시 세션 처리, E2E(실 스택) 테스트, 문서-코드 정합 점검.
- 마일스톤 완료 시 3렌즈 병렬 검토(코드 정밀 / 신규 사용자 워크스루 / 문서-코드 일치성).

## 작업 방식

- 브랜치: `feat/m1-contracts`, `feat/m2-core`, `feat/m3-server`, `feat/m4-dashboard` — PR + squash merge.
- 문서만 수정하는 변경은 main 직접 커밋 허용.
- 각 마일스톤 완료 시 WorkLog.md 갱신 + ReviewChecklist.md 점검.

## 미결 사항 (사용자 확인 필요 시 질문)

- GPT-5.6 정확한 API 모델 ID 확인 (`gpt-5.6-terra` 추정, 확실하지 않음 —
  공식 문서가 자동화 접근 403이라 구현 착수 시 확인)
- ChatGPT subscription 연동(Sign in with ChatGPT OAuth) 실현 가능성 — M2 착수 전 스파이크
- 기본 팀 구성(역할 3~4개)의 구체 정의 — M1에서 초안 제시 후 확인
- 에이전트에게 부여할 작업 도구 범위 (초기: 텍스트 협업만 / 추후: 웹 검색, 코드 실행 등 서버 도구)
