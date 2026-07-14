# EventContract — SSE 이벤트 계약

> 대시보드(M4)가 `GET /sessions/{id}/events`로 구독하는 세션 이벤트 스트림의 계약.
> **코드 원천은 `src/hwabaek/contracts.py`의 `Event`와 6개 `make_*_event` 헬퍼**다.
> 문서와 코드가 어긋나면 코드가 진실이며, 이 문서를 코드에 맞춰 갱신한다.
> SSE 와이어 포맷(`event:`/`id:`/`data:` 줄 구성)과 REST 이력 API는 M3에서 확정 —
> 이 문서는 payload 스키마와 발행 규칙만 다룬다.

## 1. 이벤트 봉투 (envelope)

| 필드 | 타입 | 의미 |
|---|---|---|
| `event_id` | string | 전역 유일 이벤트 식별자(엔진이 부여). 세션 경계와 무관하게 유일 — 재구독 기준(§5)이 아니라 감사·중복 배달 판별용. |
| `session_id` | string | 이벤트가 속한 세션 id. |
| `type` | string | `EventType` 값 중 하나(§2). |
| `sequence` | int (≥0) | 세션 내 0부터 시작하는 단조 증가 일련번호(세션마다 독립 카운터). 재구독 복원의 기준(§5). |
| `created_at` | string | 이벤트 시각(ISO 8601). `message`는 `Message.created_at`을 그대로 쓰고, 그 외는 발행 시점에 호출자(세션 엔진)가 찍는다. |
| `payload` | object | 타입별 스키마(§3). |

## 2. 이벤트 타입

`EventType`: `session_status` / `message` / `agent_state` / `usage` / `vote_status` / `result`.

## 3. 타입별 payload 스키마

### 3.1 `session_status` (`make_session_status_event`) — 세션 상태 전이 시 발행

`result`/`submitted_by`는 담지 않는다 — 확정 결과는 `result` 이벤트로 별도 전달.
실패 세션의 미승인 초안(`Session.draft_result`, D-025)도 payload에 싣지 않는다 —
REST 세션 스냅샷 조회(M3)로 제공.

| 필드 | 타입 | 의미 |
|---|---|---|
| `status` | string | `SessionStatus`: `running`\|`voting`\|`completed`\|`failed`\|`cancelled`. |
| `fail_reason` | string\|null | `status==failed`일 때만 값 존재: `budget`\|`messages`\|`idle`\|`agent_error`\|`no_quorum`\|`interrupted`. `messages`는 D-033 이전 저장 세션 호환 값이며 신규 chat 상한은 proposal 전환에 사용한다. |
| `fail_detail` | string\|null | `status==failed`일 때만 값 가능 — 귀책(클라이언트 잘못 vs 프로바이더 혼잡) 포함 실패 상세(영어 ASCII). |

```json
{"event_id": "evt_000012", "session_id": "sess_8f3a1c", "type": "session_status", "sequence": 12,
 "created_at": "2026-07-14T09:15:22.104Z",
 "payload": {"status": "voting", "fail_reason": null, "fail_detail": null}}
```

### 3.2 `message` (`make_message_event`) — 버스에 메시지가 실릴 때 발행

payload는 `Message.to_dict()`와 동일 스키마.

| 필드 | 타입 | 의미 |
|---|---|---|
| `id` | string | 메시지 id. |
| `session_id` | string | 세션 id (envelope와 동일). |
| `sender` | string | 발신 에이전트(`*` 불가). |
| `recipients` | string[] | 수신자. 브로드캐스트는 `["*"]` 단독. |
| `type` | string | `MessageType`: `chat`\|`result_proposal`\|`vote`. |
| `content` | string | 본문(`vote`는 투표 사유). |
| `created_at` | string | 버스 시각. envelope `created_at`과 동일 값. |
| `sequence` | int | 메시지의 세션 내 단조 증가 번호(D-023) — **버스가 부여하며 메시지만 카운트**. envelope `sequence`(모든 이벤트 카운트)와는 **독립된 카운터**로, 일반적으로 값이 다르다 (D-025 정정). |
| `vote` | string\|null | `VoteDecision`: `approve`\|`reject`. `chat`/`result_proposal`은 항상 `null`. |
| `proposal_id` | string\|null | `vote`/`result_proposal` 필수(대상/자기 `ResultProposal.id` — 제안 버전 추적, D-016), `chat`은 항상 `null`. |

```json
{"event_id": "evt_000013", "session_id": "sess_8f3a1c", "type": "message", "sequence": 13,
 "created_at": "2026-07-14T09:15:23.500Z",
 "payload": {"id": "msg_0091", "session_id": "sess_8f3a1c", "sender": "analyst",
 "recipients": ["*"], "type": "result_proposal",
 "content": "Draft summary: quarterly revenue rose 8% YoY.",
 "created_at": "2026-07-14T09:15:23.500Z", "sequence": 9, "vote": null, "proposal_id": "prop_7"}}
```

(예시에서 envelope `sequence`=13, payload `sequence`=9 — 이벤트 카운터는 상태/사용량
이벤트도 포함하므로 메시지 카운터보다 앞서 간다.)

### 3.3 `agent_state` (`make_agent_state_event`) — 에이전트 상태 변화 시 발행

| 필드 | 타입 | 의미 |
|---|---|---|
| `agent` | string | 에이전트 이름. |
| `state` | string | `AgentState`: `idle`\|`thinking`\|`voting`\|`dead`. |
| `detail` | string\|null | 상태 변화 사유 — 특히 `dead` 전이 시 귀책 포함 실패 상세(영어 ASCII). |

```json
{"event_id": "evt_000014", "session_id": "sess_8f3a1c", "type": "agent_state", "sequence": 14,
 "created_at": "2026-07-14T09:15:24.000Z",
 "payload": {"agent": "writer", "state": "thinking", "detail": null}}
```

### 3.4 `usage` (`make_usage_event`) — 사용량 갱신 시 발행

`usage`는 세션 누적치(`Session.usage`), `per_agent`는 에이전트별 누적치 **전체 맵**
(이름 오름차순) — 매 발행 시 전체를 다시 실어 대시보드가 마지막 이벤트만으로 복원 가능.

| 필드 | 타입 | 의미 |
|---|---|---|
| `usage.input_tokens` | int (≥0) | 누적 입력 토큰. |
| `usage.output_tokens` | int (≥0) | 누적 출력 토큰. |
| `usage.cache_read_tokens` | int (≥0) | 누적 캐시 읽기 토큰. |
| `usage.cache_write_tokens` | int (≥0) | 누적 캐시 쓰기 토큰. |
| `work_tokens` | int | `input + output + cache_write`. 작업 예산 판정값. |
| `processed_tokens` | int | `work_tokens + cache_read`. 전체 처리량 판정값. |
| `token_budget` | int | 작업 토큰 상한 — 작업 게이지 분모. |
| `processed_token_limit` | int 또는 null | 캐시 읽기를 포함한 전체 처리 상한. |
| `phase` | string 또는 null | 내부 예산 단계: discussion, synthesis, proposal, voting, revision. |
| `reserved_tokens` | int | 아직 정산되지 않은 진행 중 호출의 작업 토큰 예약 합계. |
| `per_agent` | object | 에이전트 이름 → `Usage.to_dict()` 스키마의 누적치 맵(빈 객체 가능). |

```json
{"event_id": "evt_000015", "session_id": "sess_8f3a1c", "type": "usage", "sequence": 15,
 "created_at": "2026-07-14T09:15:24.200Z",
 "payload": {"usage": {"input_tokens": 5230, "output_tokens": 812,
 "cache_read_tokens": 4096, "cache_write_tokens": 0},
 "work_tokens": 6042, "processed_tokens": 10138, "token_budget": 60000,
 "processed_token_limit": 150000, "phase": "discussion", "reserved_tokens": 6000,
 "per_agent": {"analyst": {"input_tokens": 2100, "output_tokens": 300,
 "cache_read_tokens": 2048, "cache_write_tokens": 0}}}}
```

### 3.5 `vote_status` (`make_vote_status_event`) — `VoteTally` 변화 시 발행

투표 1건 반영 또는 기권 일괄 처리 시. 목록은 에이전트 이름 오름차순 정렬.

| 필드 | 타입 | 의미 |
|---|---|---|
| `proposal_id` | string | 대상 초안 id. |
| `proposal_version` | int | 대상 초안의 버전(`ResultProposal.version`) — 대시보드가 "제안 N차"를 표시하는 데 사용. |
| `approvals` | string[] | 승인. |
| `rejections` | string[] | 반대. |
| `abstained` | string[] | 기권 처리(`voting_timeout` 만료까지 무응답, D-019 — `idle_timeout`과는 별개). |
| `pending` | string[] | 미응답. |

```json
{"event_id": "evt_000016", "session_id": "sess_8f3a1c", "type": "vote_status", "sequence": 16,
 "created_at": "2026-07-14T09:15:30.000Z",
 "payload": {"proposal_id": "prop_7", "proposal_version": 2, "approvals": ["reviewer"],
 "rejections": [], "abstained": [], "pending": ["researcher"]}}
```

### 3.6 `result` (`make_result_event`) — 합의 승인으로 세션이 `completed` 확정될 때만 발행

`Session.status != completed`이면 `ContractError`.

| 필드 | 타입 | 의미 |
|---|---|---|
| `result` | string | 승인된 `submit_result` 내용. |
| `submitted_by` | string | 초안 제출 에이전트. |

```json
{"event_id": "evt_000020", "session_id": "sess_8f3a1c", "type": "result", "sequence": 20,
 "created_at": "2026-07-14T09:16:05.000Z",
 "payload": {"result": "Final report: revenue analysis complete.", "submitted_by": "analyst"}}
```

## 4. 발행 규칙 (Plan "코어 의미론" §3~5 대응)

| 상황 | 발행 이벤트 | 비고 |
|---|---|---|
| 세션 생성(초기 `running`) | `session_status` | 세션의 첫 이벤트로 sequence `0`을 부여한다. |
| `send_message` chat 발신 | `message` | |
| `submit_result` 호출 (running에서만) | `message`(`result_proposal`) + `session_status`(`voting`) + `vote_status`(빈 집계 초기 스냅샷) | `running→voting`. 반려 후 재제출은 version이 오른 새 제안(D-016). voting 중 중복 submit은 거부되어 이벤트 없음. |
| `vote_result` 호출 | `message`(`vote`) + `vote_status` | 투표는 브로드캐스트 메시지로도 남음(화백 원칙). 이전 제안에 대한 늦은 투표는 무시되어 `vote_status` 미발행. |
| 무응답 기권 처리 (교정 호출 소진 또는 `voting_timeout` 만료) | `vote_status` | 메시지 이벤트 없음. voting 일반 채팅은 거부되며 미투표 응답에는 교정 호출을 한 번만 허용한다(D-032). |
| 합의 승인 | `vote_status`(최종) + `session_status`(`completed`) + `result` | `voting→completed`. |
| 합의 반려 | `vote_status`(최종) + `session_status`(`running`) | 사유는 이미 `vote` 메시지 content로 전달됨. |
| 합의 무효(`no_quorum`) | `vote_status`(최종) + `session_status`(`failed`) | |
| 에이전트 상태 전이 | `agent_state` | idle 판정은 세션의 단일 감시 태스크. |
| LLM 호출 종료 후 사용량 갱신 또는 voting/revision 단계 전환 | `usage` | 단계 전환 이벤트는 사용량이 같아도 새 `phase` 스냅샷을 발행한다. |
| 일반 채팅 상한 도달 | `usage`(`phase=proposal`) | 제안·투표 메시지는 채팅 상한에서 제외하며 이후 일반 채팅은 거부한다(D-033). |
| 토큰/유휴 상한 초과 | `session_status`(`failed`, 해당 `fail_reason`) | |
| 생존 에이전트 1개 이하 | `agent_state`(`dead`) + `session_status`(`failed`, `agent_error`) | |
| 사용자 취소 | `session_status`(`cancelled`) | |
| 서버 재시작 시 이전 running/voting 세션 | `session_status`(`failed`, `interrupted`) | 재시작 복구 처리(D-021) — 중단 시점 이전에 확정되지 않은 진행 중 세션을 일괄 종료. |

## 5. 재구독 복원 규칙

- 새로고침 시 먼저 REST로 세션 스냅샷/이력을 복원한 뒤 SSE를 재구독한다(IA.md 내비게이션).
- 연결이 끊겼다가 재개될 때 마지막으로 받은 SSE `id`(`sequence`)를
  `Last-Event-ID` 헤더로 보낸다. 서버는 그 sequence보다 큰 이벤트부터 재개한다.
  `event_id`는 감사·중복 배달 판별용 전역 유일 식별자이며 재구독 기준이 아니다.
- 헤더 없는 최초 구독은 sequence `-1` 이후, 즉 저장된 전체 이벤트를 오래된 순서로
  재전송한 뒤 라이브 스트림으로 이어진다. 현재 runner의 메모리 이벤트 로그 또는
  SQLite 이벤트 이력에서 재전송한다.
- SSE 와이어 프레임은 아래와 같다. `id:`에는 `sequence`, `event:`에는 이벤트 타입,
  `data:`에는 §1의 전체 이벤트 봉투 JSON을 한 줄로 싣고 빈 줄로 프레임을 끝낸다.

  ```text
  id: 12
  event: session_status
  data: {"event_id":"...","session_id":"...","type":"session_status","sequence":12,...}

  ```

- 종료 세션은 backlog를 모두 보낸 뒤 스트림을 닫는다. 활성 세션은 backlog 뒤에 새
  이벤트를 이어 보내며, 재전송 도중 발생한 이벤트도 누락·중복 없이 sequence 순서로 전달한다.
- 같은 연결의 자동 재접속은 SSE `id`로 받은 마지막 sequence를 기준으로 재개한다.
  새 페이지 로드·새로고침은 REST 스냅샷을 복원한 뒤 `Last-Event-ID` 없이 SSE 전체
  backlog를 받고, 이미 복원한 레코드는 `event_id`나 도메인 레코드 id로 멱등 적용한다.
- `Last-Event-ID`가 음수·비정수이거나 SQLite 정수 범위를 넘으면 스트림을 열기 전에
  HTTP 400으로 거부한다.
- 라이브 이벤트를 충분히 빨리 소비하지 못해 서버 큐 상한을 넘긴 연결은 종료될 수 있다.
  클라이언트는 마지막으로 적용한 SSE `id`를 기준으로 재접속해 누락분을 복원한다.

## 6. 대시보드 소비 매핑 (IA.md SC-03)

| UI 요소 | 소비 이벤트 |
|---|---|
| 상태 배지 / 경과 시간 | `session_status` |
| 작업/캐시/전체 처리량, 예산 게이지와 단계 | `usage` |
| 메시지 타임라인 | `message` |
| 에이전트 패널 — 상태 | `agent_state` (dead 사유는 `payload.detail`) |
| 에이전트 패널 — 개별 토큰 사용량 | `usage` (`payload.per_agent`) |
| 투표 현황 배지 | `vote_status` (`payload.proposal_version`으로 "제안 N차" 표시) |
| 최종 결과 카드 | `result` (트리거: `session_status.status==completed`) |
| 취소 버튼 반응 | `session_status`(`status==cancelled`) |

## 7. 해소된 모호점 (초안 작성 시 발견 → M1 계약에 반영)

- ~~`usage` 이벤트에 에이전트별 사용량 없음~~ → `payload.per_agent`(전체 맵) 추가.
- ~~`agent_error` 귀책을 실을 필드 없음~~ → `Session.fail_detail`(FAILED에서만 허용,
  `session_status.payload.fail_detail`로 노출) + `agent_state.payload.detail`(dead 사유) 추가.

## 8. 내부 도메인 이벤트 taxonomy (확정: 6개 집계 타입 유지 — D-028)

**결정 (D-028)**: 세분 enum을 도입하지 않는다. 아래 논리적 이벤트들은 §2의 6개
집계 `EventType`에 payload로 매핑된다 — 소비자는 payload 필드로 세분 의미를
식별한다. 세분이 실제로 필요한 소비자가 M4에서 등장하면 payload 필드 추가 또는
신규 타입으로 확장하며, 봉투는 이미 호환된다. `limit.warning`(예산 임박)은 현재
별도 이벤트가 없다 — usage 이벤트의 phase와 작업/전체 처리량 대비 상한으로 판단한다.

아래는 코드의 6개 집계 타입을 설명하기 위한 논리 이벤트 참고 목록이다. 별도 enum이나
와이어 타입이 아니며, 소비자는 §3 payload와 §4 발행 규칙을 계약으로 사용한다.

### 8.1 논리 이벤트 예시

- `session.created` / `session.started` / `session.status_changed` / `session.completed` /
  `session.failed` / `session.cancelled`
- `agent.started` / `agent.thinking` / `agent.idle` / `agent.dead` / `agent.error`
- `message.sent` / `message.delivered` / `message.batch_consumed`
- `proposal.created` / `proposal.rejected` / `proposal.superseded` / `proposal.approved`
- `vote.cast` / `vote.rejected`
- `usage.updated`
- `limit.warning` / `limit.exceeded`

### 8.2 SSE 집계 타입과의 확정 매핑

| 논리 이벤트 | SSE `EventType` |
|---|---|
| `session.status_changed` / `session.completed` / `session.failed` / `session.cancelled`<br>(`session.created`/`session.started`는 초기 발행에 대응) | `session_status` |
| `message.sent` | `message` |
| `agent.*`(`started`/`thinking`/`idle`/`dead`/`error`) | `agent_state` |
| `vote.cast` | `vote_status` |
| `usage.updated` | `usage` |
| `proposal.approved` | `result` |

`proposal.created`/`proposal.rejected`/`proposal.superseded`, `vote.rejected`,
`message.delivered`/`message.batch_consumed`는 별도 SSE 타입이 아니라 현재 집계 이벤트의
payload와 저장 레코드로 표현한다. `limit.warning`은 `usage`, 토큰·유휴 등 종료
한도 초과는 `session_status`의 `fail_reason`으로 표현한다. chat 상한은
`usage.phase=proposal` 전환으로 표현한다(D-033).

### 8.3 호환 원칙

봉투(§1)는 동일하게 유지하고, 타입 세분화만 확장한다 — 내부 taxonomy가 확정되어도
`event_id`/`session_id`/`type`/`sequence`/`created_at`/`payload` 구조는 변하지 않으며,
필요 시 `EventType`에 값이 추가되거나 `payload`에 필드가 추가되는 방식으로만 진화한다.
기존 6개 타입만 구독하는 클라이언트가 깨지지 않는 것이 원칙이다.
