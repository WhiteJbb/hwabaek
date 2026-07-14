# Research — 기술/라이브러리 조사

> 조사 일자: 2026-07-14. 출처: Anthropic 공식 문서(platform.claude.com), Claude Code 문서(code.claude.com).

## 1. 멀티 에이전트 구축 방식 비교 (Anthropic 스택 기준)

| 방식 | 하네스(루프) | 호스팅 | 특징 | 판단 |
|---|---|---|---|---|
| Messages API + tool use 직접 구현 | 직접 작성 | 직접 | 통제력 최대, 모든 오케스트레이션 로직 소유 | **채택 (D-001)** |
| SDK Tool Runner (`client.beta.messages.tool_runner`) | SDK 제공 | 직접 | 단일 에이전트 툴 루프 자동화. 에이전트 내부 루프에 활용 가능 | M2에서 부분 활용 검토 |
| Claude Agent SDK (`claude-agent-sdk`) | Claude Code 하네스 | 직접 | 파일/bash 도구·서브에이전트 내장. 별도 제품 | 미채택 |
| Managed Agents (베타) | Anthropic | Anthropic | 세션 컨테이너 호스팅, `multiagent: coordinator` 내장 | 미채택 (직접 구현이 목적) |

## 2. Claude API 핵심 사실 (구현에 직접 영향)

### 모델 (2026-06 기준)
| 모델 | ID | 컨텍스트 | 가격(입력/출력, $/1M) |
|---|---|---|---|
| Claude Opus 4.8 | `claude-opus-4-8` | 1M | 5.00 / 25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | 3.00 / 15.00 (프로모션 2.00/10.00, ~2026-08-31) |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | 1.00 / 5.00 |

- ~~기본 모델은 `claude-opus-4-8` (D-007)~~ → D-008에서 GPT-5.6 Terra로 변경(§6).
  Claude 모델은 에이전트별 오버라이드 옵션으로 유지 — 이 절의 내용은 anthropic 어댑터
  구현 시(후순위) 필요.

### thinking / effort (Opus 4.8 기준 — 구버전 지식과 다름, 주의)
- `thinking: {"type": "adaptive"}` 사용. `budget_tokens`는 **400 에러** (제거됨).
- `temperature` / `top_p` / `top_k`도 Opus 4.7+에서 **400 에러** — 보내지 않는다.
- effort는 `output_config: {"effort": "low|medium|high|xhigh|max"}`.
- assistant 프리필(마지막 assistant 턴) 불가 — 구조화 출력은 `output_config.format` 사용.

### 스트리밍
- `max_tokens`가 크면(≥~16K) 논스트리밍은 SDK 타임아웃 위험 → `client.messages.stream()`
  + `get_final_message()` 기본 사용.

### 프롬프트 캐싱 (멀티 에이전트 비용의 핵심 레버)
- **접두사(prefix) 일치** 기반. tools → system → messages 순으로 렌더링.
- 에이전트별 시스템 프롬프트를 **고정(frozen)**하고, 대화는 messages 뒤에만 추가.
  타임스탬프·랜덤값을 시스템 프롬프트에 넣지 않는다.
- 마지막 system 블록에 `cache_control: {"type": "ephemeral"}` → tools+system 함께 캐싱.
- 검증: `usage.cache_read_input_tokens`가 0이면 무효화 원인 조사.

### tool use
- 병렬 tool_use 가능 — 모든 `tool_result`를 **하나의 user 메시지**로 반환해야 함.
- 도구 실패는 `is_error: true`로 반환 (누락 금지).
- `stop_reason` 처리: `end_turn` / `tool_use` / `max_tokens` / `refusal` / `pause_turn`.

### 오류 처리
- SDK가 429/5xx 자동 재시도(기본 2회). 타입별 예외 체인으로 처리
  (`RateLimitError` → `APIStatusError` → `APIConnectionError`).
- 오류 귀책 구분(클라이언트 잘못 vs API 혼잡)을 로그에 남긴다 — CLAUDE.md 검증 원칙.

## 3. 멀티 에이전트 패턴 조사

- **오케스트레이터-워커**: 수렴 제어 쉬움, 병렬 fan-out에 최적. Anthropic 권장 기본형.
- **파이프라인**: 결정적 단계 흐름. 유연성 낮음.
- **자율 협업(메시지 패싱)** — 채택(D-003):
  - 장점: 역할 간 상호 비판·보완이 자연 발생, 팀 구성만 바꿔 다양한 협업 구조 실험 가능.
  - 위험: **수렴 실패(무한 대화)**, 비용 폭증, 관측 어려움.
  - 대응: (1) `submit_result` 도구로 명시적 종료, (2) 메시지 수/토큰 예산 상한,
    (3) 유휴 감지, (4) 대시보드 타임라인 관측성. → Plan M2 종료 정책.
  - 참고 구현: Claude Code Agent Teams(인박스/메일박스 + 공유 태스크 리스트),
    Managed Agents multiagent(코디네이터 + 스레드) — 개념 차용 가능.

## 4. 서버/대시보드 스택

- **FastAPI + uvicorn**: asyncio 네이티브 (요청 경로 블로킹 금지 제약과 부합). (D-006)
- **SSE vs WebSocket**: 대시보드는 서버→클라이언트 단방향 스트림이면 충분 → SSE.
  사용자 입력(제출/취소)은 REST.
- 세션 영속화: ~~초기에는 JSONL 파일 검토~~ → **SQLite EventStore로 확정 (D-017)** —
  의결 기록(제안/투표/결정)의 구조적 조회가 필요. write-behind로 요청 경로 블로킹 회피.

## 5. 미조사 / 추후 조사

- 에이전트에 부여할 서버 도구(web_search 등) 통합 방식 — 도구 범위 결정(Plan 미결) 후.
- 세션 이력이 길어질 때의 컨텍스트 관리(compaction 베타) — M5.
- ~~ChatGPT subscription OAuth 연동의 기술 상세~~ → 스파이크 완료(§6),
  **구독 백엔드 실측 완료 (2026-07-14 — §6 실측 결과)**. 잔여: device flow
  rate limit(미계측 — 실사용 중 관찰).
- Python asyncio Queue의 원자적 drain 구현 방식 (`get_nowait` 루프 vs 단일 소비자 보장) — M2 bus.
- OpenAI tool call의 중단·재시도 처리 (부분 tool call 응답, 타임아웃 후 재호출 시 중복 방지) — M2 어댑터.
- SSE reconnect와 Last-Event-ID의 실무 동작 (브라우저 EventSource 재전송 규약) — M3.
- SQLite의 async 접근 방식 비교 (`aiosqlite` vs `asyncio.to_thread` + 표준 `sqlite3`) — M2 store.
- 프로바이더별 usage 집계 차이 (OpenAI/Anthropic의 캐시 토큰 보고 방식) — M2 어댑터.

## 6. OpenAI GPT-5.6 / subscription 연동 (2026-07-14 추가 조사)

### GPT-5.6 패밀리 (2026-07-09 출시)

| 티어 | 포지션 | 가격(입력/출력, $/1M) | 비고 |
|---|---|---|---|
| Sol | 플래그십 | 5.00 / 30.00 | |
| Terra | 중간 — GPT-5.5급 성능 | 2.50 / 15.00 | **기본 모델 (D-008)** |
| Luna | 최속·최저가 | 1.00 / 6.00 | 경량 역할 후보 |

- ChatGPT / Codex / OpenAI API 모두에서 제공.
- Responses API: Programmatic Tool Calling(모델이 인메모리 프로그램으로 도구들을 조합
  호출), Multi-agent(베타 — 단일 요청 내 동시 서브에이전트) 추가.
- 프롬프트 캐싱: 명시적 cache breakpoint 지원, 최소 캐시 수명 30분.
  GPT-5.6+는 캐시 쓰기 1.25x 과금 / 캐시 읽기 90% 할인 — §2의 Claude 캐싱 전략과
  마찬가지로 "시스템 프롬프트 고정 + 뒤에만 추가" 원칙 적용 가능.
- **모델 ID 확정 (2026-07-14 스파이크)**: `gpt-5.6-sol` / `gpt-5.6-terra` /
  `gpt-5.6-luna` + 별칭 `gpt-5.6` — openai SDK 2.45.0의 모델 타입 정의에서 직접
  확인 (웹 문서 403 우회). 기본 모델은 `gpt-5.6-terra` (D-008, contracts.DEFAULT_MODEL).

### ChatGPT subscription 연동 (D-008의 전제)

- ChatGPT 구독과 API 키 과금은 **별개 시스템** — 구독으로 일반 API 키 호출은 불가.
- 단 **"Sign in with ChatGPT"**(OAuth 2.0, BYOS: bring-your-own-subscription) 경로로는
  사용자가 자기 ChatGPT 계정으로 로그인해 구독 quota로 모델을 사용 가능
  (Free/Go/Plus/Pro 지원, 개발자 측 무과금)이라는 조사 결과.
- **스파이크 결과 (2026-07-14) — "작동하지만 비공식"**:
  - 경로 실재: **Codex OAuth(device code flow)** 토큰이 구독 과금으로 Responses API를
    호출하는 유일한 경로. litellm이 `chatgpt/` 프로바이더로 문서화(토큰 로컬 저장,
    Chat Completions는 Responses로 브릿지), openclaw 등 실사용 사례 존재.
  - 공식 보장 없음: OpenAI 공식 문서(learn.chatgpt.com/docs/auth)는 Codex 제품 대상만
    서술 — 서드파티 허용도 금지도 명시하지 않음. **Anthropic(2026-02 약관 금지 →
    2026-04 과금 강제)·Google(2026-02)이 동일 경로를 차단한 전례** → 소급 차단 리스크.
  - 기술 제약: 구독 백엔드는 `max_tokens`/`max_output_tokens`/`metadata`를 **거부**
    (litellm은 strip) → 토큰 예산의 사전 상한 불가, **사후 집계로만 강제** 가능.
    litellm 문서는 gpt-5.4까지 기재 — 5.6의 구독 백엔드 지원은 실측 필요.
  - **결정 (D-026)**: 하이브리드 — 어댑터 인증 모드 2종(api_key | chatgpt_oauth).
    M2a는 api_key(공식·안정), M2b에서 chatgpt_oauth 모드 추가.
- **구독 백엔드 실측 결과 (2026-07-14, 실계정 — 어댑터 접목 시 전부 반영)**:
  - `gpt-5.6-terra` 구독 백엔드 **지원 확인** (텍스트·tool call 모두 정상, usage 보고).
  - 사전 로그인 필요: ChatGPT 보안 설정에서 **장치 코드 인증 활성화**가 선행돼야
    device flow가 진행된다(기본 비활성 — 서버가 활성화 안내를 반환).
  - `store=false` **강제** (400 "Store must be set to false").
  - `stream=true` **강제** (400 "Stream must be set to true") → 어댑터가 SSE 이벤트를
    집계해 완성 응답으로 되돌린다. accept 헤더는 별도 강제 없음(httpx 기본으로 동작).
  - `prompt_cache_breakpoint` **거부** (400 "not supported on this model") →
    chatgpt_oauth 모드에서는 명시적 캐싱 opt-in을 끈다(암묵 캐싱 여부는 미확인).
  - 종결 이벤트(response.completed)의 Response 스냅샷에 **output이 비어 있음**
    (usage는 정상) → `response.output_item.done` 이벤트의 완성 아이템
    (message/function_call)을 수집해 보강해야 한다. 표준 API와 다른 지점.

출처: openai.com/index/gpt-5-6 (TechCrunch·MarkTechPost 2026-07-09 보도),
OpenAI Help Center(구독 vs API 과금 분리), openai/codex#10974 (Sign in with ChatGPT),
learn.chatgpt.com/docs/auth (Codex 인증), docs.litellm.ai/docs/providers/chatgpt,
openai SDK 2.45.0 타입 정의.
