# IA — 정보 구조 (화면 목록, 화면 흐름)

> 웹 대시보드(M4)의 화면 구조. UserScenarios.md / Personas.md와 독립 관리.

## 화면 목록

### SC-01 홈 / 태스크 제출
- 태스크 입력 폼 (텍스트), 팀 설정 선택 (드롭다운, 기본: default 팀)
- 제출 버튼 → 세션 생성 → SC-03으로 이동
- 실행 중(running/voting) 세션이 있으면 제출 비활성 + 안내 배너, 해당 세션 링크 (D-013)
- 최근 세션 요약 카드 (최근 5건, 상태 뱃지)

### SC-02 세션 목록
- 최근 최대 200개 세션 테이블: 태스크 요약, 팀, 상태(running/voting/completed/failed/cancelled), 시작 시각, 작업 토큰 사용량
- failed는 사유 병기 (예: `failed (budget)`, `failed (no_quorum)`)
- 태스크 링크 클릭 → SC-03

### SC-03 세션 상세 (핵심 화면)
- 상단: 태스크 내용, 상태, 경과 시간, 작업 토큰/예산 게이지, 캐시 재사용량,
  캐시를 포함한 전체 처리량·상한, 현재 예산 단계, 취소 버튼
- 중앙: **메시지 타임라인** — 에이전트별 색상 구분, 발신자→수신자 표시,
  브로드캐스트/직접 메시지 구분, 실시간 갱신(SSE)
  - 합의 이벤트는 구분 표시: 초안 제출(result_proposal), 투표(approve/reject + 사유),
    반려 후 원 제출자의 수정 단계 (D-032)
- 좌측(또는 상단 바): 에이전트 패널 — 이름/역할/상태(thinking/idle/voting/dead)/개별 토큰 사용량
- voting 상태: 상단에 투표 현황 배지 (승인 n / 반대 n / 대기 n)
- 종료 시: 최종 결과 카드 (승인된 `submit_result` 내용) 강조 표시
- 투표까지 갔지만 확정 없이 실패한 세션(no_quorum 등): 결과 카드 자리에
  **미승인 초안 카드**(`draft_result` + 제안자, "승인되지 않음" 배지) 표시 (D-025)

### SC-04 팀 설정 뷰 (읽기 전용, 초기 버전)
- Quick/Standard(default)/Deep 팀 목록과 각 팀의 에이전트 구성(이름/역할/모델),
  작업·전체 처리 상한, 종합·제안 임계값, 호출 예약량, 제안 버전 상한 표시
- 편집은 초기 버전에서 YAML 파일 직접 수정으로 대체 (추후 편집 UI 검토)

## 화면 흐름

```
SC-01 홈/제출 ──제출──> SC-03 세션 상세 (실시간 관찰)
   │                        │
   ├──최근 세션 클릭─────────┘
   │
   └──> SC-02 세션 목록 ──태스크 클릭──> SC-03
   └──> SC-04 팀 설정 뷰
```

## 내비게이션

- 상단 고정 내비: 홈(제출) / 세션 / 팀
- 세션 상세는 `/app/#/sessions/{id}`로 직접 링크한다. JSON API
  (`/sessions/{id}`)와 충돌하지 않으며, 새로고침 시 REST 스냅샷 복원 후 SSE 전체
  backlog를 재구독한다.
- 화면 URL: 홈 `/app/#/`, 세션 목록 `/app/#/sessions`, 팀 `/app/#/teams`.

## M3 서버 데이터 연결

| 화면 | REST/SSE 연결 |
|---|---|
| SC-01 홈 / 태스크 제출 | `POST /sessions`, 최근 세션은 `GET /sessions?limit=5` |
| SC-02 세션 목록 | `GET /sessions` |
| SC-03 세션 상세 | `GET /sessions/{id}` 후 `GET /sessions/{id}/events` 구독, 취소는 `POST /sessions/{id}/cancel` |
| SC-04 팀 설정 | `GET /teams` |

- `GET /sessions/{id}`는 세션과 메시지·제안·투표 이력을 복원한다. `team`은 가능하면
  실행 당시 저장한 스냅샷이며, 레거시·무저장 세션은 현재 설정으로 보완하고 둘 다
  없으면 `null`이다.
- SSE 최초 구독은 헤더 없이 전체 이벤트 backlog를 받은 뒤 라이브 이벤트로 이어진다.
  재연결할 때는 마지막으로 적용한 이벤트의 `sequence`를 `Last-Event-ID`에 넣고,
  서버가 그보다 큰 sequence부터 재전송한다.
- 종료 세션의 SSE는 저장된 backlog 전송 후 닫힌다. 없는 세션은 REST와 SSE 모두 404다.
