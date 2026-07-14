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
  1. 어느 에이전트든 `submit_result` 호출 → 화백 합의 투표(D-011) → 승인 시 정상 종료
  2. 일반 채팅 수 상한 도달 → 토론을 닫고 제안 단계 강제(D-033)
  3. 세션 토큰 예산 초과 → 강제 종료 (failed: budget)
  4. 모든 에이전트 유휴(보낼 메시지 없음) → 종료 (failed: idle)
- **동시 세션 1개** (D-013): running/voting 세션 존재 시 새 제출 거부 + 안내.

## 코어 의미론 (M1 계약에 반영)

1. **인박스 배치 소비와 메시지 정책 (D-023)**: 에이전트는 LLM 호출 중 도착한
   메시지를 인박스에 쌓고, 호출이 끝나면 쌓인 메시지 **전부를 하나의 user 턴으로
   병합해 1회 호출**한다. 메시지 1건당 1호출 금지 — 비용·수렴 모두 불리.
   - 인박스 drain은 원자적으로 처리하고, drain 이후 도착분은 다음 배치.
   - 배치 내 순서는 **세션 단위 단조 증가 sequence**(계약 필드, 버스가 부여)로
     고정 — 동일 timestamp도 결정적.
   - 브로드캐스트는 원본 1건이 수신자별 인박스에 독립 배달(발신자 제외, 배달 id
     미분리). 버스는 동일 message id의 중복 배달을 무시. 자기송신 금지(계약 검증).
2. **대화 이력 표현과 절단 보존 우선순위**: 수신 메시지는 발신자를 태깅한 user 턴으로
   병합(예: `[from: analyst] ...`). 시스템 프롬프트는 고정(캐시 prefix), 이력은 뒤에만
   append — 프롬프트 캐싱 전략(Research §2·§6)과 정합. 이력 상한(에이전트당 턴 수
   또는 추정 토큰) 도달 시 절단하되(M5 compaction 전 최소 방어선), **보존 우선순위**:
   시스템 프롬프트(절대 제거 금지) > 사용자 원본 태스크 > 제안·투표 관련 메시지
   (결정 근거) > 최근 메시지 > 오래된 chat부터 절단. 보호 대상 자체가 상한을
   넘으면 **최신 제안 1개만 원문 보호**하고 과거 버전·투표는 한 줄 요약으로
   대체한다(D-025). 절단 발생 사실은 명시적 메시지로 이력에 삽입한다.
3. **세션 상태 기계**:
   ```
   running ──result_proposal──> voting ──정족수 승인──> completed
      ↑                           │
      └────────반려(사유 전달)─────┘
   running/voting ──> failed(fail_reason) | cancelled(사용자 취소)
   fail_reason: budget | messages | idle | agent_error | no_quorum | interrupted
   ```
   - **idle은 failed(idle)로 분류** — 결과물 없이 끝났으므로 실패.
   - **agent_error**: 재시도 소진 후에도 실패하는 에이전트는 dead 처리하고 세션은
     지속. 생존 에이전트가 1개 이하가 되면 failed(agent_error). 귀책 범주
     (`ErrorCategory` — client_error/provider_error/rate_limit/timeout/
     invalid_tool_call/runtime_error/cancelled)와 재시도 가능 여부를 분리해
     세션 기록에 남긴다 — 오류 귀책 원칙.
   - **interrupted**: 서버 재시작 시 이전 running/voting 세션 처리 (D-021).
   - **상태별 허용 명령** (계약 `ALLOWED_COMMANDS`, D-032): running =
     send_message + submit_result / voting = vote_result만 / 종료 상태 = 전부 거부.
     내부 proposal/revision 단계에서는 제출자에게 submit_result만 허용한다.
4. **타이머 2종과 판정 주체 (D-019)**: `idle_timeout`은 running 전용 — 모든
   에이전트가 (a) 인박스 비어 있음 (b) LLM 호출 중 아님 상태로 지속되면 idle.
   `approval.voting_timeout`은 voting 전용 — 만료 시 미투표를 기권 처리하고
   정족수 판정. **voting 중 idle 감시는 세션을 종료하지 않는다.** 두 타이머 모두
   세션의 단일 감시 태스크가 관리(에이전트 자체 판단 금지 — 레이스 방지).
5. **화백 합의 (D-011, D-016, D-018/D-020 개정)**:
   - **제안**: `submit_result`는 running에서만 허용. 호출 시 `ResultProposal`
     (id/proposer/**version**/content/**status**: pending→approved|rejected,
     rejected→superseded) 레코드가 생성되고, `result_proposal` 메시지(proposal_id
     포함)로 전원 브로드캐스트, 세션 voting 전환. 세션당 활성(pending) 제안은
     최대 1개 — **voting 중 추가 `submit_result`는 도메인 오류로 거부**, 반려로
     running 복귀 후에만 version을 올린 새 제안 제출 가능(이전 제안은 superseded).
   - **투표**: voting 중에는 현재 제안에 대한 `vote_result(approve|reject, reason)`만
     허용. 투표는 `Vote` 레코드(proposal_id 필수, reject는 사유 필수)로 기록되고
     **변경 불가**(D-020), 제출자는 자기 제안에 투표 불가. **이전 제안에 대한 늦은
     투표는 무시**한다. 도구 인자에서 proposal_id를 생략하면 활성 제안으로
     해석한다(LLM의 id 오기입에 견고 — Vote 레코드에는 항상 실제 id가 기록됨).
   - **투표 대상자 (D-018/D-027)**: voting 시작 시점의 **생존하고 vote_result
     권한을 가진** 에이전트(제출자 제외)로 **스냅샷 확정** — 심의 중 사망해도
     집합을 바꾸지 않는다. 투표 불가(사망·오류)는 voting_timeout 만료 시 기권
     처리. 대상자 0명이면 first가 아닌 한 no_quorum (팀 검증이 "first 아닌 모드 +
     2인 미만 팀"과 "제출자 외 투표 가능 에이전트 부재"를 사전 거부).
   - **정족수** (`termination.approval.mode`, 기권은 어떤 정책에서도 승인이 아님):
     `unanimous`(기본) = 스냅샷 심의자 **전원** approve — voting_timeout 만료 시
     미투표자가 있으면 failed(no_quorum) / `majority` = 스냅샷 심의자 **전체의
     과반** approve / `participating_unanimous` = 유효 투표자 전원 approve
     (`minimum_votes` 하한 지원) / `first` = 투표 생략 즉시 확정.
   - **판정과 전환의 분리 (D-021/D-032)**: ConsensusEngine은 판정만 반환하고
     세션 상태는 SessionManager가 전환한다. reject 발생 시 running으로 복귀하되
     내부 revision 단계에서 원 제출자만 수정안을 제출한다. 일반 채팅은 다시 열지
     않으며 기본 제안 버전 상한은 2개다.
   - **미승인 초안 보존 (D-025)**: 투표까지 갔지만 확정 없이 실패한 세션
     (no_quorum, voting 중 예산 초과 등)은 마지막 제안 content를
     `Session.draft_result`(+`draft_proposer`)로 보존한다 — 사용자가 최소한
     초안은 수령. no_quorum 실패 시 fail_detail에 **미투표/기권자 목록 기록 필수**
     (실패 사유가 가장 뭉개지기 쉬운 경로).
6. **종료 원자성 (D-021)**: 여러 종료 조건이 동시에 발생해도(마지막 투표 vs 예산
   초과, 취소 vs 승인 등) 종료는 **한 번만** 확정된다 — 세션 단위 lock으로 전환을
   직렬화하고 최초 유효 사유만 저장. 경합 우선순위:
   `cancelled → completed → budget/messages → agent_error → no_quorum → idle`
   (실제 경쟁 조건에서의 필요성은 M2 테스트로 재검증). 종료 후 도착한
   메시지·제안·투표는 상태를 바꾸지 못하며 감사용 rejected event로 기록할 수 있다.
7. **서버 재시작 (D-021)**: 시작 시 저장소의 이전 running/voting 세션을
   **failed(interrupted)**로 일괄 처리한다. 완료·실패·취소 세션과 의결 기록은
   조회 가능해야 하며, 실행 중 세션의 완전 복원은 M5 이후로 유보.

## 디렉터리 구조 (목표)

```
src/hwabaek/
  contracts.py        # M1: 메시지/에이전트/팀/세션 스키마 (dataclass + 검증)
  bus.py              # M2: 비동기 메시지 버스 (에이전트별 인박스)
  agent.py            # M2: 에이전트 런타임 (LLM 툴 루프)
  consensus.py        # M2: ConsensusEngine — 제안/투표/정족수 판정만 반환 (D-016/D-021)
  session.py          # M2: SessionManager — 상태 전환 직렬화 + 타이머 2종 + 예산/생존 관리
  store/
    base.py           # M1: Store Protocol (저장 계약) — D-017, 확정됨
    sqlite.py         # M2b: SQLite 구현 (ORM 금지, write-behind)
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
  team.quick.yaml     # 빠른 2인 판단 (작업 25k)
  team.default.yaml   # 표준 대등 3인 구조 (작업 60k)
  team.deep.yaml      # 고위험 3인 심층 검토 (작업 100k)
tests/                # 단위 + 통합 (Fake LLM 클라이언트로 밀폐)
```

패키지 이름은 `hwabaek`으로 확정(D-010) — 화백(和白), 신라의 만장일치 합의 회의체.

## 마일스톤

### M0 — 프로젝트 초기화 (완료: 2026-07-14)
- 필수 문서 세트 생성, 방향 결정 확정.

### M1 — 계약 확정 (계약 우선 원칙) (완료: 2026-07-14)
- `contracts.py`: `Message`(id/session_id/sender/recipients/**type: chat|result_proposal|vote**/content/created_at),
  `AgentSpec`(name/role/system_prompt/model/max_turns),
  `TeamConfig`(agents/termination: max_messages/token_budget/idle_timeout/**approval**),
  `Session`(id/task/**status: running|voting|completed|failed|cancelled**/result/**fail_reason**/usage).
  "코어 의미론" 절의 상태 기계·합의 규칙을 타입으로 강제한다.
- `llm/base.py`: LLM 클라이언트 계약(프로바이더 중립 Protocol) 확정 — 요청/응답/사용량/
  도구 호출 표현을 프로바이더 특이사항 없이 정의 (D-009).
- 팀 설정 YAML 스키마 확정 + 로더 + 검증 오류 메시지.
- SSE 이벤트 계약(대시보드가 구독할 이벤트 타입 목록) 문서화 → `docs/EventContract.md`.
- **완료 기준**: 스키마 단위 테스트 통과. 이후 모듈은 이 계약 위에서 병렬 구현 가능.
- 완료 내역: contracts.py + llm/base.py + llm/fake.py(테스트 대역) + config.py 로더 +
  configs/team.default.yaml(기본 팀 초안) + EventContract.md.
- **설계 동기화 개정 (2026-07-14, D-018~D-024)**: ResultProposal.status(+superseded) /
  `Vote` 독립 계약 / `ApprovalConfig`(mode·voting_timeout·minimum_votes, 문자열
  하위호환) / 투표 대상 스냅샷 불변(with_voter_removed 삭제) / 상태별 허용 명령표
  (`ALLOWED_COMMANDS`) / Message.sequence·자기송신 금지 / Event 봉투(event_id·
  sequence·created_at) / FailReason.interrupted / ErrorCategory.
- **M1 잔여 처리**: Store Protocol(`store/base.py`) 확정 완료(2026-07-14 — 세션/팀
  스냅샷/메시지/제안/투표/이벤트 조회 계약 + `validate_vote` 순수 함수 추가).
  도메인 이벤트 세분 taxonomy 확정만 M2로 이월(EventContract §8 후보 — 발행 지점과 함께).

### M2 — 코어 엔진 (서버 없이 동작)

두 단계로 나눠 진행한다 (D-025 — 통합 리스크 축소):
- **M2a (완료: 2026-07-14)**: 인메모리 코어 — bus / consensus / session / agent +
  llm/openai_client(api_key 모드) + Fake LLM 통합 테스트(실패 경로 13종) +
  CLI smoke(`python -m hwabaek.run "..." --fake`). store 없이 완결 동작.
- **M2b (완료: 2026-07-14)**: `store/sqlite.py` 접목(D-029 — sqlite3 +
  to_thread, write-behind) + chatgpt_oauth 인증 모드(D-026 — 구독 백엔드 실측
  제약은 Research §6) + 도메인 이벤트 taxonomy 확정(D-028) + **실 API 스모크
  통과**(구독 백엔드, 3인 팀 합의 completed — 과정에서 dead 상태 오분류·투표
  UX·스트림 hang 수정, 기본 팀 D-030 개정).

- `store/sqlite.py` 구현 (D-017, M2b — 계약은 `store/base.py`에 확정): 스키마는
  계약의 레코드(sessions/팀 스냅샷=agents/messages/proposals/votes/session_events)에
  대응. decisions/usage_events는 파생 가능(승인 제안+투표 / usage 이벤트) —
  별도 테이블 여부는 M2b 스키마 확정 시 결정.
- `bus.py`: asyncio 기반 인박스/브로드캐스트(발신자 제외), 세션 sequence 부여,
  중복 id 무시, 원자적 drain — 관측용 이벤트 훅.
- ~~착수 전 스파이크~~ **완료 (2026-07-14)**: 모델 ID 확정(`gpt-5.6-terra`, SDK 타입
  정의), subscription 연동은 "작동하지만 비공식" 판정 → 인증 하이브리드로 결정
  (D-026, Research §6).
- `llm/openai_client.py`: openai SDK 어댑터 — 기본 모델 `gpt-5.6-terra`, 스트리밍,
  프롬프트 캐싱(시스템 프롬프트 고정 + messages 뒤에 추가, 명시적 cache breakpoint),
  재시도, 키 마스킹. **인증 모드 2종 (D-026)**: `api_key`(기본 — M2a) |
  `chatgpt_oauth`(device flow — M2b에서 추가, 구독 백엔드가 max_output_tokens를
  거부하므로 예산은 사후 집계로 강제 + gpt-5.6 지원 실측). 프로바이더 특이사항
  (파라미터/stop 사유 처리)은 어댑터 내부에 격리. (anthropic 어댑터는 후순위 —
  adaptive thinking, `refusal` stop_reason 등도 어댑터에 격리)
- `agent.py` (Native Agent Runtime): 인박스 대기 → 쌓인 메시지 배치 병합 → LLM 호출
  → `send_message`/`submit_result`/`vote_result` tool_use 처리(런타임 검증 — 미존재
  수신자/상태 위반/중복 투표/빈 제출/과대 메시지는 구조화된 tool error로 반환,
  한 응답의 채팅은 1회, 열린 토론 반복 오류는 max_turns·예산에 포함, 결정 단계는
  단계별 2회로 별도 제한) → 반복. 에이전트별 대화 이력 유지(보존
  우선순위 절단 포함). 실행부는 세션·합의 로직과 결합하지 않게 유지 — 필요해지면
  AgentRuntime Protocol로 추출(D-015), 현재는 별도 Runtime 추상화를 만들지 않는다.
  session.py는 프로바이더 SDK 타입에 의존하지 않는다(경계: llm/base 계약만).
- `consensus.py` (ConsensusEngine): 제안 생성(version 증가, 이전 제안 superseded
  처리), 투표 대상자 스냅샷 확정(D-018), 투표 등록(proposal_id 검증 — 늦은 투표
  무시, 중복 투표 거부), `contracts.VoteTally` 기반 정족수 계산. **세션 상태를 직접
  바꾸지 않고 판정(ProposalOutcome)만 SessionManager에 반환** (D-021).
- `session.py` (SessionManager): 에이전트 기동/종료, 상태 전환의 단일 직렬화 지점
  (세션 lock — 종료 1회 확정 + 우선순위, D-021), voting 잠금(running에서만
  submit_result), 타이머 2종(idle_timeout/voting_timeout — D-019) 감시, 취소,
  예산·생존 에이전트 관리, 동시 세션 1개 강제(D-013 — 서버 API와 별개로 이중 강제,
  전역 singleton은 피함).
- 최소 실행 스크립트(`python -m hwabaek.run "태스크"`)로 콘솔에서 스모크 확인.
- **완료 기준**: Fake LLM 테스트(밀폐) + 실 API 스모크 1회. 실패 경로 테스트
  (토큰 예산 초과 / 채팅 상한의 제안 강제 / API 오류 / 합의 반려 후 재제출 시 version 증가 /
  이전 proposal에 대한 늦은 투표 무시 / voting 중 중복 submit_result 거부 /
  심의자 사망 시 정족수 재계산 / no_quorum / 투표 중 기권 / voting 중에도 예산 강제 /
  idle 판정과 voting timeout의 레이스 없음 / 세션 종료 후 추가 메시지·투표 거부)
  포함 — "실패 경로가 제품이다".

### M3 — 서버 (완료: 2026-07-14)
- REST: `POST /sessions`(태스크 제출 — 동시 세션 1개 검사와 생성을 원자적으로),
  `GET /sessions/{id}`, `POST /sessions/{id}/cancel`, `GET /teams`(팀 설정 목록),
  저장된 세션 조회(완료/실패/취소 세션·메시지 타임라인·제안/투표·의결문·사용량 —
  store 기반).
- SSE: `GET /sessions/{id}/events` — 이벤트 실시간 스트림, `Last-Event-ID`(sequence)
  호환 (EventContract §5).
- 서버 시작 시 이전 running/voting 세션을 failed(interrupted)로 처리 (D-021).
- **완료 기준**: curl 기반 통합 스모크 + 재시작 후 완료 세션 조회 확인.
  요청 경로에 블로킹 I/O 없음 확인.
- **구현 분할 (위임 계획)**: 파일 소유권 겹침 없이 순차/병렬 위임 —
  ① 서버 코어(server/app.py·api.py·events.py + 밀폐 테스트 + `python -m
  hwabaek.serve` 진입점 + fastapi/uvicorn 의존성) → ② 문서 정합(README·IA —
  엔드포인트 반영) + 실서버 curl 스모크 → ③ 3렌즈 병렬 검토(코드 정밀 /
  신규 사용자 워크스루 / 문서-코드 일치) 후 일괄 수정. 조율·검토는 직접 수행.
- **마감 결과 (2026-07-14)**: 서버 코어 커밋(`8374d8a`)과 완료 기준 스모크 이후
  남은 작업을 마감한다. 재시작 `failed(interrupted)` 전환 시 EventContract §4의
  `session_status` 이벤트도 영속화하고 회귀 테스트를 추가한다. 코드 정밀 / 신규
  사용자 워크스루 / 문서-코드 일치 3렌즈를 독립 검토한 뒤 발견 사항을 일괄 수정하고,
  README·EventContract·IA·WorkLog를 실제 REST/SSE/CLI 동작과 맞춘다. 전체 테스트와
  밀폐 서버 스모크를 다시 통과해 M3 완료 기준을 충족했다.
  리뷰에서 확인된 공백 태스크, fake 모드의 명시적 잘못된 팀, 충돌하는 DB 옵션,
  잘못된 `Last-Event-ID`는 요청 경계에서 명시적으로 거부한다. graceful shutdown은
  사용자 취소가 아니라 `failed(interrupted)`로 기록하며, 에이전트/LLM 팩토리 조립
  실패도 세션을 `failed(agent_error)`로 종결하고 라이터·태스크를 정리한다. 느린
  SSE 구독자의 라이브 큐는 상한을 두고 초과 시 재접속시키며, SQLite backlog의
  페이지 단위 전송·compaction은 M5 견고화에서 처리한다.

### M4 — 기본 대시보드 (완료: 2026-07-14)
- **구조/라우팅 (D-031)**: 별도 프론트엔드 빌드 체인 없이 패키지의 정적
  HTML/CSS/JavaScript를 FastAPI가 `/app/`에서 제공한다. JSON API
  `/sessions/{id}`와 충돌하지 않도록 화면은 `/app/#/`, `/app/#/sessions`,
  `/app/#/sessions/{id}`, `/app/#/teams` 해시 라우팅을 사용한다. `/`는
  `/app/`으로 redirect한다.
- **홈/제출 (SC-01)**: 태스크 입력, 팀 선택, 실행 중 세션 안내, 최근 세션 5건.
  공백 입력·서버 검증 오류를 평이한 메시지로 표시하고 생성 후 상세로 이동.
- **세션 목록 (SC-02)**: 상태·태스크·팀·시각·토큰 사용량을 표시하고 상세로 연결.
- **세션 상세 (SC-03)**: REST 스냅샷 이후 SSE 전체 backlog를 구독한다. event_id와
  도메인 레코드 id로 중복 적용을 막고, 상태·에이전트·메시지·제안/투표·사용량·
  승인 결과 또는 미승인 초안을 실시간 표시한다. running/voting에서만 취소 허용.
- **팀 (SC-04)**: 팀 설명, 모델, 종료 정책, 에이전트 역할·모델·capabilities를
  읽기 전용으로 표시.
- **표현/접근성**: 화백 회의체를 연상시키는 종이색·먹색·주색 계열, 모바일까지
  반응형, 키보드 focus·상태 텍스트·aria-live를 제공한다. 도트 월드 UI는 M6 범위다.
- **파일/검증**: `src/hwabaek/dashboard/` 정적 자산 + `server/app.py` mount,
  package-data 설정, `tests/test_dashboard.py` 통합 테스트. 전체 테스트와 `--fake`
  실서버 제출→SSE→결과 스모크 후 코드 정밀 / 신규 사용자 / 문서 정합 3렌즈 리뷰.
- **완료 기준**: P-01이 브라우저에서 서버 기동 후 제출·관찰·결과 복사·과거 세션
  재접근을 수행할 수 있고, P-02가 도움 없이 제출→결과 핵심 경로를 찾을 수 있음.

### M5 — 견고화
- **M5.1 예산·수렴 제어 (D-032, 기능 구현 완료·실안건 튜닝 진행 중)**:
  - `Usage.work_tokens`(캐시 읽기 제외)와 `processed_tokens`(캐시 포함)를 분리하고
    Standard 상한을 작업 60k / 전체 처리 150k로 설정한다.
  - 작업 25k에서 종합을 유도하고 40k에서 심의자의 일반 토론을 닫아 상대등의
    제안을 우선한다. 20k는 투표와 최대 한 차례 수정에 보존한다.
  - 호출 전 6k 예약으로 동시 호출 초과를 제한하고, voting에서는 채팅 금지·투표
    1회 교정 재시도·제안 최대 2개를 강제한다.
  - 기본 3인 팀 `max_turns: 12`, `max_messages: 30`; Quick(25k 작업)과
    Deep(100k 작업) 프로필을 별도 YAML로 제공한다.
  - 대시보드는 작업/캐시/전체 처리를 분리하고 현재 예산 phase와 두 상한을 표시한다.
  - 밀폐 검증: 캐시 읽기 제외/전체 처리 상한, 단계 전환·도구 제한, 동시 호출 예약,
    투표 교정 1회, 제안 2개 상한, 프로필 로드와 대시보드 표시를 회귀 테스트한다.
  - 실안건 튜닝 완료 기준: 대표 기술 안건 10개 중 8개 이상 완료, 작업 토큰 중앙값 40k 이하,
    90%가 40k 이전 첫 제안, 제안 이후 budget 실패 0건, 호출 예약 초과 6k 이내.
- **M5.2 실안건 1차 보정 (D-033)**:
  - `max_messages`는 일반 채팅만 세고, 상한 도달 시 실패 대신 proposal 단계로
    전환한다. 제안·투표 메시지는 결정 절차이므로 채팅 상한에서 제외한다.
  - 호출 입장 시 실제 작업량+진행 예약+다음 예약으로 단계 임계값을 선제 보호해
    경계 부근의 새 토론 호출을 직렬화한다.
  - 한 응답의 `send_message`는 1회로 제한하고, `max_turns` 도달 에이전트도
    proposal/voting/revision의 단계별 2회 한도 안에서는 결정 호출을 수행한다.
    제출자의 토론 턴 소진은 proposal 강제 전환 조건으로 사용한다.
  - 첫 실안건 실패 패턴(채팅 30개, 늦은 proposal, 심의자 턴 소진)을 밀폐 재현해
    제안·투표가 완료되고 작업 예산과 각 결정 호출 상한이 유지되는지 검증한다.
- 후속 견고화: 취소 레이스/타임아웃 경합(종료 우선순위 재검증), 서버 재시작 처리
  고도화, 이력 compaction(요약 기반), E2E(실 스택) 테스트, 문서-코드 정합 점검.
- 마일스톤 완료 시 3렌즈 병렬 검토(코드 정밀 / 신규 사용자 워크스루 / 문서-코드 일치성).

### M6 — 확장 실험 (후순위)
- Hermes worker adapter 등 외부 Agent Runtime/Worker 실험 (D-015 — 코어 계약에
  전용 타입 노출 금지 전제).
- 에이전트 외부 작업 도구(웹 조사, 코드 작업 등).
- "도트 월드" UI (픽셀 스타일 팀 관측 화면).

## 비목표 (현재 범위에서 구현하지 않음)

Hermes/OpenClaw 연동, 멀티호스트 분산 실행, Redis·Kafka 등 외부 메시지 큐,
에이전트의 자유로운 동적 생성, 장기 기억, 웹 검색, 코드 실행, GitHub 자동 수정,
완성형 도트 대시보드, 복잡한 이벤트 소싱, 실행 중 세션 완전 복구.

## 작업 방식

- 브랜치: `feat/m1-contracts`, `feat/m2-core`, `feat/m3-server`, `feat/m4-dashboard` — PR + squash merge.
- 문서만 수정하는 변경은 main 직접 커밋 허용.
- 각 마일스톤 완료 시 WorkLog.md 갱신 + ReviewChecklist.md 점검.

## 미결 사항 (사용자 확인 필요 시 질문)

- ~~GPT-5.6 모델 ID 확인 / subscription 연동 실현 가능성~~ — **스파이크로 해소
  (2026-07-14)**: ID 확정(`gpt-5.6-terra`), 인증은 하이브리드(D-026)
- ~~기본 팀 구성~~ — **사용자 확정 (D-027)**: 대등 3인 구조
  (research_daedeung/critic_daedeung/sangdaedeung) + capabilities 도구 권한.
  파생 팀(code-review/knowledge/career)은 후속 설정 추가로.
- Hermes(외부 Agent Runtime/Worker 프레임워크) — 미도입 확정(D-015). 장기 실행
  작업자(웹 조사/코드 수정 등)가 필요해질 때 외부 Runtime/Worker Adapter로만
  후순위 실험 검토. Hermes 전용 타입을 코어 계약에 노출하지 않는다.
- 에이전트에게 부여할 작업 도구 범위 (초기: 텍스트 협업만 / 추후: 웹 검색, 코드 실행 등 서버 도구)
- ~~M4 대시보드 URL namespace~~ — **D-031로 해소**: `/app/#/...` 해시 라우팅.
