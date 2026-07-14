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
   투표합니다(**화백 합의** — 기본은 실제 참여한 심의자 전원 승인 + 최소 1표,
   미투표는 승인으로 치지 않음). 승인되면 최종 결과와 함께 세션이 종료되고,
   반려되면 원 제출자가 사유를 반영해 한 번 수정합니다.

### 종료 안전장치

자율 협업 패턴의 최대 위험(무한 대화, 비용 폭증)을 시스템 차원에서 차단합니다.

- `submit_result` 제출 + 합의 승인 시 정상 종료 (투표 중에도 예산 상한 유지)
- 캐시 읽기를 제외한 작업 예산과 캐시를 포함한 전체 처리 상한을 별도로 강제
- 종합 → 제안 → 투표 → 수정 단계로 허용 호출과 도구를 좁혀 결론 없는 대화를 차단
- 호출 전 토큰 예약, 메시지·에이전트 턴·제안 버전 상한
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
  max_messages: 30       # 일반 채팅 상한 — 도달 시 제안 단계 강제
  token_budget: 60000    # 작업 예산(최소 3): input + output + cache_write
  processed_token_limit: 150000 # 전체 처리 상한: 작업 + cache_read
  synthesis_at: 25000    # 종합 유도 시작점
  proposal_by: 40000     # 일반 토론을 닫고 제안을 우선하는 지점
  call_reserve_tokens: 6000 # 동시 호출 전 예약량
  max_proposals: 2       # 최초 제안 + 최대 한 차례 수정
  idle_timeout: 45       # 전원 유휴 판정 시간(초) — running 전용
  approval:              # 화백 합의 설정 (문자열 축약형 `approval: unanimous`도 지원)
    mode: participating_unanimous # unanimous | majority | participating_unanimous | first
    timeout_seconds: 120 # 투표 대기 시간 — idle_timeout과 별개의 voting 전용 타이머
    minimum_votes: 1     # participating_unanimous 전용 유효 투표 하한
agents:                  # 1명 이상
  - name: sangdaedeung   # 필수
    role: ...            # 필수 — 대시보드 표시용
    system_prompt: ...   # 필수
    model: ...           # 선택 — 에이전트별 오버라이드
    max_turns: 12        # 선택 — 열린 토론 호출 상한(결정 단계는 별도 2회 제한)
    capabilities:        # 선택 — 생략 시 전체. 런타임이 권한 밖 호출을 거부 (D-027)
      - send_message     #   send_message | submit_result | vote_result
      - submit_result
```

기본 팀은 화백 컨셉의 **대등 3인 구조**입니다 — 조사 대등(research_daedeung),
견제 대등(critic_daedeung), 상대등(sangdaedeung, 진행·종합·제출). 상대등이 제출한
안은 나머지 두 대등 중 최소 1명이 투표하고, 실제 투표한 심의자 전원이 승인해야
의결됩니다.

내장 프로필은 `quick`(2인, 작업 25k), `default`(Standard 3인, 작업 60k),
`deep`(3인, 작업 100k)입니다. 짧고 되돌리기 쉬운 선택은 Quick, 일반 기술
의사결정은 Standard, 보안·아키텍처처럼 비가역적이거나 고위험인 안건은 Deep이
적합합니다. 프로필 이름은 대시보드나 `--team`에서 선택합니다.

허용되지 않은 키는 오타로 간주해 로드 시 즉시 거부됩니다(파일·필드 경로를 포함한
오류 메시지). 대시보드가 구독하는 이벤트 스트림 계약은
[docs/EventContract.md](docs/EventContract.md) 참조.

## 프로젝트 상태

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| M0 | 방향 결정, 기술 조사, 문서/계획 수립 | ✅ 완료 |
| M1 | 계약 확정 (메시지/에이전트/팀/세션 스키마) | ✅ 완료 |
| M2a | 코어 엔진 (버스/에이전트 루프/합의/종료 정책, 인메모리) | ✅ 완료 |
| M2b | 영속화(SQLite) + subscription OAuth 모드 + 실 API 스모크 | ✅ 완료 |
| M3 | 서버 (FastAPI REST + SSE) | ✅ 완료 |
| M4 | 웹 대시보드 | ✅ 완료 |
| M5 | 견고화 (예산·수렴 제어 구현, 실안건 튜닝/E2E 후속) | 🚧 진행 중 |
| M6 | 확장 실험 (외부 워커, 도구, 도트 월드 UI) | 후순위 |

상세 계획은 [docs/Plan.md](docs/Plan.md) 참조.

## 기술 스택

- Python 3.11+ / asyncio
- [openai SDK](https://github.com/openai/openai-python) — 기본 모델 GPT-5.6 Terra
  (에이전트별 오버라이드 가능)
- LLM 클라이언트는 프로바이더 중립 계약으로 추상화 — Anthropic 어댑터는 후순위
  ([docs/DecisionLog.md](docs/DecisionLog.md) D-008/D-009)
- FastAPI + uvicorn, SSE

## 실행 (콘솔 스모크)

```
# 밀폐 스모크 — 실키/네트워크 없이 전체 스택 확인
.venv\Scripts\python.exe -m hwabaek.run "your task" --fake

# 실제 실행 — OPENAI_API_KEY 필요 (기본 팀: configs/team.default.yaml)
.venv\Scripts\python.exe -m hwabaek.run "your task"

# 내장 예산 프로필 선택 (quick | default | deep, YAML 경로도 허용)
.venv\Scripts\python.exe -m hwabaek.run "your task" --team quick

# ChatGPT 구독(OAuth) 모드 — 최초 1회 로그인 후 사용 (D-026, 실험적)
.venv\Scripts\python.exe -m hwabaek.llm.chatgpt_auth login
.venv\Scripts\python.exe -m hwabaek.run "your task" --auth chatgpt_oauth
```

세션 이벤트가 콘솔에 실시간 출력되고, 종료 시 상태·결과(또는 미승인 초안)·토큰
사용량이 표시됩니다. 세션 기록은 기본적으로 SQLite(`data/hwabaek.db`)에
저장됩니다 — 경로는 `--db`, 비활성화는 `--no-db`.

## 서버와 웹 대시보드 실행 (M3/M4)

`python -m hwabaek.serve`로 REST + SSE 서버를 띄웁니다. **localhost(127.0.0.1)
전용으로 고정**되며 인증 계층은 없습니다(D-012) — 외부에 노출하지 않습니다.

```
# 밀폐 데모 — 실키/네트워크/DB 없이 기동
.venv\Scripts\python.exe -m hwabaek.serve --fake --no-db

# 실제 실행 — 기본 api_key 모드는 OPENAI_API_KEY 필요
.venv\Scripts\python.exe -m hwabaek.serve

# ChatGPT OAuth 모드 — 먼저 chatgpt_auth login 필요
.venv\Scripts\python.exe -m hwabaek.serve --auth chatgpt_oauth

# 포트/기본 팀 지정
.venv\Scripts\python.exe -m hwabaek.serve --port 9000 --team default
```

주요 인자: `--port`(기본 8000) · `--team`(요청에 team 생략 시 기본 팀, 기본
`default`, 실 LLM 모드 전용) · `--auth api_key|chatgpt_oauth`(실 LLM 인증 모드,
`--fake`에서는 무시) ·
`--fake`(스크립트 Fake LLM 밀폐 데모) · `--db <path>`(기본 `data/hwabaek.db`) ·
`--no-db`(영속화 비활성). `--db`와 `--no-db`는 함께 쓸 수 없습니다. fake 모드는
내장 `smoke` 팀을 사용하며, 요청 body에 다른 team을 명시하면 400을 반환합니다.
`--no-db`에서는 현재/가장 최근 세션 스냅샷과 SSE backlog만 메모리에 남고,
메시지·제안·투표 상세 배열은 저장하지 않으며 다음 세션이나 재시작 시 사라집니다.

서버가 뜨면 같은 PC의 브라우저에서 <http://127.0.0.1:8000/>을 엽니다. 홈에서
태스크와 팀을 골라 세션을 시작하고, 상세 화면에서 에이전트 상태·논의·표결·토큰
사용량과 최종 결과를 실시간으로 볼 수 있습니다. `세션`은 최근 200개 기록,
`팀`은 YAML 팀 구성을 읽기 전용으로 보여 줍니다. 대시보드는 인증 없는 localhost
전용이며 같은 PC에서만 사용합니다. LAN 공유·공개 호스팅은 M4 범위가 아닙니다.

PowerShell 5.1에서 태스크 제출 → 조회 최소 예시:

```powershell
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/sessions `
  -ContentType 'application/json' -Body '{"task":"summarize the notes"}'
$sessionId = $created.id
Invoke-RestMethod -Uri "http://127.0.0.1:8000/sessions/$sessionId"
Invoke-RestMethod -Uri http://127.0.0.1:8000/teams

# SSE 실시간 구독. PowerShell의 curl 별칭을 피하려고 curl.exe를 명시합니다.
curl.exe -N "http://127.0.0.1:8000/sessions/$sessionId/events"

# 마지막으로 적용한 SSE id가 1이면 sequence 2부터 재개
curl.exe -N -H 'Last-Event-ID: 1' "http://127.0.0.1:8000/sessions/$sessionId/events"
```

| 엔드포인트 | 성공 응답 | 주요 오류 |
|---|---|---|
| `GET /health` | 200 `{"status":"ok"}` | — |
| `POST /sessions` | 201, 평면 Session | 잘못된 팀 400, 실행 중 세션 409, 잘못된 body 422 |
| `GET /sessions?limit=50` | 200 `{"sessions": [...]}` (`limit` 1~200 보정) | — |
| `GET /sessions/{id}` | 200 `{"session": ..., "team": ..., "messages": [...], "proposals": [...], "votes": [...]}` | 없음 404 |
| `POST /sessions/{id}/cancel` | 200, 평면 Session | 없음 404, 종료 세션 409 |
| `GET /teams` | 200 `{"default_team": "...", "teams": [...]}` | 설정 오류 400 |
| `GET /sessions/{id}/events` | 200 SSE; 종료 세션은 backlog 후 연결 종료 | 잘못된 `Last-Event-ID` 400, 없음 404 |

SSE의 정확한 와이어 형식과 재구독 규칙은
[docs/EventContract.md](docs/EventContract.md)를 참조하세요.
상세 응답의 `team`은 가능하면 실행 당시 저장한 스냅샷이며, 레거시·무저장
세션은 현재 설정으로 보완합니다. 둘 다 없으면 `null`입니다. 시스템 프롬프트는
응답에 포함하지 않습니다.

## 개발 환경 (Windows)

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests
```

- venv는 네이티브 Windows Python(3.11+, `py` 런처)으로 만듭니다 — git-bash의
  MSYS2 python은 venv 레이아웃이 달라 프로젝트 규칙과 어긋납니다 (DecisionLog D-014).

- 인증(D-026 하이브리드): `OPENAI_API_KEY` 환경변수(기본·공식) 또는 ChatGPT
  subscription OAuth(`--auth chatgpt_oauth`, 선택·**비공식**). API 키와 OAuth 토큰은
  로그·오류 메시지·대시보드 어디에도 노출되지 않습니다.
- chatgpt_oauth 모드 고지 (실험적):
  - OpenAI가 공식 보장하지 않는 경로입니다 — 약관 변경 시 예고 없이 제거될 수
    있습니다 (Anthropic·Google이 동일 경로를 차단한 전례 있음).
  - 구독 백엔드는 `max_output_tokens`를 거부합니다. 런타임은 호출 전에 예산을
    예약하고 응답 후 실제 사용량을 정산하지만, 응답 1건이 예약량보다 크면 상한을
    초과한 뒤 `failed (budget)`로 끝날 수 있습니다.
  - 실계정 검증 완료 (2026-07-14): gpt-5.6-terra 구독 백엔드 동작 확인.
    백엔드가 `store=false`·`stream=true`를 강제하고 명시적 프롬프트 캐시
    breakpoint를 거부하므로, 어댑터가 내부적으로 스트리밍을 집계하며 이 모드에서는
    명시적 캐싱을 비활성화합니다 (api_key 모드는 영향 없음).
  - 로그인 시 "Codex용 장치 코드 인증을 활성화" 안내가 나오면: ChatGPT 계정은
    장치 코드 인증이 기본 비활성입니다. chatgpt.com → 설정 → 보안에서 장치 코드
    인증을 켠 뒤 `python -m hwabaek.llm.chatgpt_auth login`을 다시 실행하세요
    (Team/Enterprise는 워크스페이스 관리자 허용이 필요할 수 있음).
  - 토큰 파일(`~/.hwabaek/chatgpt_token.json`)은 Windows에서 사용자 프로필 ACL에
    의존합니다. 경로는 `HWABAEK_CHATGPT_AUTH_FILE`로 재정의할 수 있습니다.
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
