# WorkLog — 작업 진행 내역

> 최신 항목이 위. 오류와 수정 내역 포함.

## 2026-07-14 — M2a 착수 전 스파이크: 모델 ID 확정 + subscription 연동 검증

### 진행한 작업
- **GPT-5.6 모델 ID 확정**: 웹 문서가 403이라 최신 openai SDK(2.45.0)를 설치해
  타입 정의에서 직접 추출 — `gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna`(+별칭
  `gpt-5.6`). 기존 placeholder `gpt-5.6-terra`와 일치, 코드 변경 없이 "추정"
  마커만 확정으로 갱신.
- **subscription 연동 검증** — "작동하지만 비공식" 판정 (Research §6):
  Codex OAuth(device flow)가 구독 과금 Responses API 호출의 실재 경로
  (litellm `chatgpt/` 프로바이더 문서화), 단 OpenAI의 공식 서드파티 허용 없음 +
  Anthropic·Google의 2026년 초 동일 경로 차단 전례 + 구독 백엔드의
  max_output_tokens/metadata 거부(예산 사전 상한 불가 → 사후 집계 필요) 확인.
- **결정 (D-026, 사용자)**: 인증 하이브리드 — 어댑터 인증 모드 2종
  `api_key`(기본, M2a) | `chatgpt_oauth`(M2b 추가). LLMClient 계약 변경 없음.
  D-008 갱신, Plan 미결 2건 해소, README 고지 추가.

### 오류/이슈
- 없음.

### 다음 할 일
- M2a 착수: `feat/m2a-core` 브랜치 — bus / ConsensusEngine / SessionManager /
  agent 루프 + llm/openai_client(api_key 모드) + Fake LLM 통합 + CLI smoke
- 기본 팀 초안 사용자 확인 (Plan 미결)

## 2026-07-14 — M1 머지 (PR #1) + 저장소 이름 변경

### 진행한 작업
- M1 PR(#1)을 squash merge로 main에 병합, 작업 브랜치 삭제 (규칙 7 워크플로우).
  main 검증: 전체 테스트 274개 통과.
- GitHub 저장소 이름을 `team_ai` → **`hwabaek`**으로 변경 (사용자 수행, D-010 정합).
  로컬 origin URL 갱신: https://github.com/WhiteJbb/hwabaek.git

### 다음 할 일
- M2a 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 검증 + GPT-5.6 모델 ID 확정
- M2a: `feat/m2a-core` 브랜치 — bus / ConsensusEngine / SessionManager / agent 루프
  + Fake LLM 통합 + CLI smoke
- 기본 팀 초안(configs/team.default.yaml) 사용자 확인 (Plan 미결)

## 2026-07-14 — M1 계약 구현 마감: Store 계약 + 투표 검증 함수 (feat/m1-contracts)

### 진행한 작업
- M1 계약 구현 지시에 따라 working tree·기존 테스트 재확인 후 잔여 범위만 구현.
  지시 범위 대부분(스키마 전체·로더·명령 허용표·오류 분류·테스트 249개)은 기구현
  상태였고, 실제 잔여는 2건:
  - **Store 계약** (`store/base.py`, 직접 작성): 저장 인터페이스만 정의(D-017,
    SQLite 구현은 M2b) — 세션 upsert/조회, 재시작 시 running·voting 식별
    (interrupted 처리용), 팀 스냅샷(재현성), 메시지 타임라인(sequence 순),
    제안 버전 이력·투표, 이벤트 after_sequence 조회(Last-Event-ID 재개),
    append 중복 id 무시(D-023).
  - **`contracts.validate_vote`** (직접 작성): 제안 수준 투표 검증의 단일 지점 —
    세션 일치, 활성 proposal_id 일치(늦은 투표 거부), pending 전용, 자기 투표
    금지. 심의자 자격·중복 투표는 VoteTally.with_vote가 기존대로 강제(중복 금지
    원칙에 따라 분리).
- 테스트 위임(sonnet): test_store_contract.py(테스트 전용 InMemoryStore로 계약
  의미 적합성 검증, 19개) + TestValidateVote(6개). 전체 **274개, 3회 반복 통과**.
- Plan 갱신: M1 잔여에서 Store Protocol 완료 처리 (taxonomy만 M2 이월).

### 문서와 다르게 구현하지 않은 것 (설계 노트)
- AgentRuntime 계약: D-015가 "현재 미도입"으로 확정 → 추가하지 않음.
- DomainEvent: 기존 Event가 봉투 계약(D-022) — 별도 타입 신설·개명 없음.
- D-017 테이블 중 decisions/usage_events는 승인 제안+투표/usage 이벤트로 파생
  가능 — 별도 메서드 없이 M2b 스키마 확정 시 결정 (store/base.py 독스트링 명시).

## 2026-07-14 — 설계 자체 검토 개선 4건 반영 (feat/m1-contracts, D-025)

### 진행한 작업
- 설계 동기화 결과를 자체 검토해 발견한 개선점 4건을 사용자 승인 후 반영:
  1. `voting_timeout` 기본 30→120초 (계약·기본 팀 YAML·README) — 기본 unanimous
     조합에서 "세션 맨 끝의 timeout-기권-no_quorum 실패" 양산 방지.
  2. **미승인 초안 보존**: `Session.draft_result`/`draft_proposer` 신설(FAILED에서만,
     동반 필수) — no_quorum·voting 중 예산 초과 실패에도 사용자가 초안 수령.
     IA SC-03에 미승인 초안 카드 추가.
  3. **EventContract 결함 정정**: message payload sequence(버스 카운터)와 envelope
     sequence(이벤트 카운터)를 "동일 값"으로 서술한 오류 → 독립 카운터로 정정,
     예시도 상이한 값으로 수정.
  4. M2를 M2a(인메모리 코어)/M2b(store 접목)로 분할 + no_quorum 시 fail_detail
     의무화 + 이력 보호 상한 규칙(최신 제안 1개만 원문 보호) — Plan 반영.
- DecisionLog D-025 기록. 전체 테스트 249개(신규 5) 3회 반복 통과.

### 오류/이슈
- (자체 검토 발견) EventContract §3.2의 이중 sequence 동일성 서술 — M3 대시보드가
  문서를 믿고 구현하면 어긋날 결함이었음. 위 3번으로 수정 완료.

## 2026-07-14 — 설계 동기화: 신규 설계를 문서·계약에 반영 (feat/m1-contracts, M1 PR에 포함)

### 진행한 작업
- 사용자 설계 고도화 지시에 따른 **설계 동기화** (M2 구현 없음, 문서·계약만).
  현재 상태를 3범주(일치/충돌/미정의)로 분류 후 Gap 해소.
- **결정 기록 (D-018~D-024)**: 투표 대상 스냅샷 불변(D-016 §5 번복 —
  with_voter_removed 삭제, 0명 심의자는 no_quorum + 팀 검증 사전 거부) /
  idle·voting 타이머 분리 + approval 구조형 설정(문자열 하위호환) /
  ResultProposal.status·Vote 독립 계약·투표 변경 금지·reject 사유 필수 /
  종료 직렬화·우선순위·interrupted / 이벤트 봉투(event_id·sequence) 채택 +
  taxonomy는 후보로 M2 확정 / 메시지 sequence·자기송신 금지·중복 배달 무시 /
  voting 중 일반 메시지 허용. D-017 갱신(store/ 분리, M2 이동, 테이블 확장).
- **계약 동기화 (직접 작성)**: contracts.py — ProposalStatus·Vote·ApprovalConfig·
  ALLOWED_COMMANDS·Message.sequence/자기송신 금지·Event 봉투 개편·
  FailReason.INTERRUPTED·ErrorCategory. llm/base.py — LLMError.category +
  LLMTimeoutError. decide()는 빈 voters → no_quorum(비-first)으로 반전,
  participating_unanimous에 minimum_votes 지원.
- **병렬 위임**: 계약 테스트 동기화(opus — test_contracts 191개 + test_llm_fake
  23개), 구조형 approval 로더 + 기본 팀 YAML + 테스트 30개(sonnet),
  EventContract/ReviewChecklist(9항목 추가)/Research(조사 항목 5건) 동기화(sonnet).
- **계획 재정리 (Plan.md)**: 코어 의미론을 7개 항목으로 확장(메시지 정책, 이력 보존
  우선순위, 타이머 2종, 스냅샷 합의, 종료 원자성, 재시작 처리), 모듈 경계
  (SessionManager/ConsensusEngine 판정-전환 분리/store/base+sqlite), 마일스톤
  M1~M6 재정리(M6 확장 실험 신설) + 비목표 명시.
- 전체 테스트 **244개, 3회 반복 통과**.

### 오류/이슈
- 없음. (구 의미론 테스트들이 예상대로 실패 → 새 계약으로 동기화)

### 보류/후순위 (의도적 — M2 이후)
- Store Protocol 상세와 SQLite 구현, 도메인 이벤트 세분 taxonomy 확정(EventContract
  §8 후보), 엔진 강제 사항 전부(voting 잠금 실행, 늦은 투표 무시, 종료 lock,
  타이머 감시, 도구 호출 런타임 검증), 이력 절단 구현.

## 2026-07-14 — 합의 의미론 개정 + Hermes 미도입 확정 (feat/m1-contracts, M1 PR에 포함)

### 진행한 작업
- 사용자 설계 검토 지시 반영. PR 미오픈 상태라 M1 브랜치에서 계약을 최종본으로 개정
  (이미 폐기 결정된 의미론을 main에 올리지 않기 위해).
- **결정 기록**: D-015(Hermes 미도입 — 코어 직접 소유 유지, 후순위 실험으로만 기록),
  D-016(합의 의미론 개정 — 정족수 4종·제안 버전·voting 잠금, D-011 개정),
  D-017(SQLite EventStore, JSONL 검토안 대체).
- **계약 개정 (contracts.py, 직접 작성)**: ApprovalPolicy에 `participating_unanimous`
  추가·의미 재정의(unanimous 엄밀화 — 미투표는 승인 아님, majority는 생존 전체 과반),
  `VoteTally.decide` 재작성 + `with_voter_removed`(사망 시 정족수 재계산),
  `ResultProposal`(version) 신설, RESULT_PROPOSAL 메시지에 proposal_id 필수화.
- **테스트 개정 (opus 위임)**: decide 매트릭스 전면 재작성 + 정책 대비 테스트
  (동일 tally에 unanimous=NO_QUORUM vs participating=APPROVED) + ResultProposal +
  사망 재계산. 167 → 190개, 전부 통과(3회 반복 확인).
- **계획 갱신 (Plan.md)**: 코어 의미론 §5 재작성, M2에 consensus.py 모듈 분리·
  AgentRuntime 결합도 노트(추상화는 현재 미도입), M2 완료 기준 테스트 목록 확장
  (version 증가/늦은 투표 무시/중복 submit 거부/사망 정족수/voting 중 예산/
  idle-voting 레이스/종료 후 거부), M3에 SQLite EventStore. EventContract/README/
  configs/Research 정합화.

### 오류/이슈
- 없음. (구 의미론 기준 테스트 5건이 예상대로 실패 → 새 의미론으로 개정)

### 미구현 후순위 (의도적)
- 엔진 수준 강제(voting 잠금 실행, 늦은 투표 무시, idle/voting 레이스) — M2
- SQLite EventStore 구현 — M3 (계획만 갱신)
- HermesAgentRuntime / AgentRuntime Protocol — 미도입 (D-015)

## 2026-07-14 — M1 계약 확정 구현 (feat/m1-contracts)

### 진행한 작업
- 프로젝트 스켈레톤: pyproject.toml(setuptools, src 레이아웃) + requirements.txt +
  네이티브 Python 3.11 venv (D-014).
- **계약 직접 작성** (개발 지침 "아키텍처는 직접"): `src/hwabaek/contracts.py`
  (스키마/상태 기계/화백 투표 집계 VoteTally/SSE 이벤트 헬퍼),
  `src/hwabaek/llm/base.py`(프로바이더 중립 LLM 계약 + 귀책 구분 오류 계층) → 선 커밋.
- **병렬 위임** (opus 2건, sonnet 2건, 파일 소유권 분리): 계약 단위 테스트 115개(opus),
  FakeLLMClient + 테스트 23개(opus), YAML 로더 config.py + 기본 팀 초안 + 테스트 19개(sonnet),
  docs/EventContract.md(sonnet).
- **통합 리뷰 및 보강**: 최종 테스트 167개 전부 통과. README(팀 설정 스키마 섹션,
  개발 환경)/Plan(M1 완료)/EventContract 갱신.

### 오류/이슈 (모두 수정 완료)
- pip이 requirements.txt를 cp949로 읽어 한글 주석에서 UnicodeDecodeError →
  pip이 파싱하는 파일은 ASCII만 사용 (D-014에 기록).
- git-bash의 python은 MSYS2 빌드라 venv가 POSIX 레이아웃(bin/)으로 생성됨 →
  네이티브 `py -m venv`로 재생성.
- (리뷰 발견) config.py에서 에이전트 이름 규칙 위반 시 임시 AgentSpec 생성이 try 밖이라
  ContractError가 ConfigError로 감싸지지 않고 누출 → 수정 + 회귀 테스트 추가.
- (리뷰 발견) 이벤트 계약 공백 2건 — 에이전트별 사용량(IA SC-03 요구)과 agent_error
  귀책 기록(Plan 의미론 §3 요구)을 실을 필드 부재 → `usage.per_agent`,
  `Session.fail_detail`, `agent_state.detail` 추가 (EventContract §7 참조).
- (리뷰 발견) LLMResponse가 stop=end인데 tool_calls를 담는 모순 상태를 허용,
  bool이 int 검증을 통과(파이썬 서브클래스) → 계약 검증 보강.

### 다음 할 일
- 기본 팀 초안(configs/team.default.yaml) 사용자 확인
- M2 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 검증 + GPT-5.6 모델 ID 확정
- M2(코어 엔진): bus/agent/session + openai 어댑터 — feat/m2-core 브랜치

## 2026-07-14 — 설계 갭 검토 및 코어 의미론 확정 (M1 준비)

### 진행한 작업
- 전체 설계 문서 검토로 M1 착수 전 설계 갭 식별: 인박스 소비 정책, 대화 이력 표현,
  세션 상태 기계(idle의 성공/실패 분류 모호), submit_result 경합, idle 판정 레이스,
  대시보드 접근 범위, 동시 세션 정책.
- 사용자 정책 결정 3건: **화백 합의 모드**(D-011 — submit_result 후 투표 승인,
  기본 만장일치), **localhost 전용**(D-012 — P-02 LAN 시나리오는 확장으로 이연),
  **동시 세션 1개**(D-013).
- 기술 설계 4건을 Plan.md "코어 의미론" 절로 확정: 인박스 배치 소비(메시지 1건당
  1호출 금지), 발신자 태깅 user 턴 병합 + 이력 상한 절단, 세션 상태 기계
  (voting 상태·fail_reason enum·agent_error 시 dead 처리), 단일 감시 태스크 idle 판정.
- 문서 정합화: Plan(M1 계약에 vote/approval/fail_reason 반영, M2 실패 경로 테스트에
  합의 경로 추가), IA(SC-01 제출 비활성, SC-03 투표 표시), UserScenarios(US-01 갱신,
  US-06 반려/재제출 추가), README, ProjectContext.

### 오류/이슈
- 없음 (문서 작업만 수행).

### 다음 할 일
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치 — contracts.py + llm/base.py +
  팀 YAML 스키마 + SSE 이벤트 계약
- M2 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 검증, GPT-5.6 모델 ID 확인

## 2026-07-14 — 패키지 이름·기본 모델 결정 (M0 후속)

### 진행한 작업
- 패키지 이름 확정: `hwabaek` (D-010). 후보 약 50개를 PyPI 등록 여부로 스크리닝한 뒤
  (미등록: hwabaek/thinktank/convene/warroom/moot/dure/watercooler/jamsession 등)
  사용자 선택으로 확정. Plan/README의 작업명 `agora`(PyPI 등록됨) 교체.
- 기본 모델 변경: `claude-opus-4-8` → OpenAI **GPT-5.6 Terra** (D-008, D-007 번복).
  사용자 결정(ChatGPT subscription 연동 전제). 웹 조사로 사실 확인 — GPT-5.6은
  2026-07-09 출시 3티어(Sol/Terra/Luna), 구독과 API 과금은 분리이나
  "Sign in with ChatGPT"(BYOS OAuth) 경로 존재. Research.md §6에 기록.
- LLM 계층 멀티 프로바이더 추상화 결정 (D-009, D-001 일부 수정) — Plan의 M1/M2와
  디렉터리 구조(`llm/` 서브패키지: base 계약 + openai/anthropic 어댑터) 갱신.
- 문서 정합화: DecisionLog(D-008~D-010), ProjectContext, Plan, README, Research, Personas.

### 오류/이슈
- openai.com 공식 문서가 자동화 접근을 403으로 차단 — GPT-5.6의 정확한 API 모델 ID
  미확인(`gpt-5.6-terra` 추정). Plan 미결 사항으로 등재.

### 다음 할 일
- M2 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 실현 가능성 검증
- GPT-5.6 정확한 API 모델 ID 확인
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치 — `llm/base.py` LLM 클라이언트 계약 포함

## 2026-07-14 — 프로젝트 초기화 (M0)

### 진행한 작업
- 프로젝트 방향 확정: Claude API 직접 구현 / Python / 자율 협업(메시지 패싱) /
  범용 태스크 / 로컬 서버 + 웹 대시보드. 사용자 질의응답으로 결정 (DecisionLog D-001~D-005).
- 기술 조사 수행 (Research.md): Anthropic 스택 4가지 구축 방식 비교, Opus 4.8 기준
  API 변경사항(adaptive thinking, 샘플링 파라미터 제거, 프리필 불가), 프롬프트 캐싱 전략,
  멀티 에이전트 패턴별 위험(자율 협업의 수렴 실패 문제) 정리.
- 필수 문서 세트 생성: ProjectContext / DecisionLog / Plan / IA / UserScenarios /
  Personas / Process / ReviewChecklist / Research / WorkLog.
- 구현 계획 수립 (Plan.md): M1 계약 → M2 코어 엔진 → M3 서버 → M4 대시보드 → M5 견고화.

### 오류/이슈
- 없음 (문서 작업만 수행).

### 다음 할 일
- 미결 사항 확인: 패키지 이름(`agora` 제안), 기본 팀 구성 역할, 에이전트 도구 범위 (Plan.md 미결 사항 참조)
- git 저장소 초기화 여부 결정 (현재 git repo 아님)
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치
