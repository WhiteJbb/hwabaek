# WorkLog — 작업 진행 내역

> 최신 항목이 위. 오류와 수정 내역 포함.

## 2026-07-15 — M5.4 voting 제안 문맥 보장 (fix/voting-proposal-context)

### 실측 원인
- 완료 세션 `56c92390-000000`에서 v1 본문 3,826자는 정상 저장·브로드캐스트됐고
  research가 승인했지만, critic은 본문이 제공되지 않았다는 이유로 반려했다.
  critic이 연속 tool-use 안에서 outer inbox drain 전 voting 호출에 진입하면서
  단계 지시와 투표 도구만 받고 활성 제안 본문은 받지 못한 경계로 확정했다.
- v2는 critic 승인 후 research 호출이 120초 동안 반환되지 않아 기권했지만,
  participating_unanimous의 유효 승인 1표로 정상 완료됐다. 이 지연은 별도 provider
  응답 문제이며 본 수정은 불필요한 v1 반려와 문맥 없는 투표를 대상으로 한다.

### 수정
- voting 입장에서 불변 ResultProposal 스냅샷을 캡처한다. AgentLoop는 호출 직전
  inbox를 drain하고, 그래도 요청 이력에 같은 proposal marker가 없으면 본문과
  즉시 투표 지시를 직접 주입한다. 정상 inbox 경로에서는 본문이 한 번만 들어간다.
- discussion/synthesis에서 시작한 요청이 voting 전환 뒤 `vote_result`를 반환하면
  단계 불일치 도구 오류로 거부하고, 다음 voting 호출이 활성 본문을 읽고 재투표한다.

### 오류/검증
- 코어 통합 테스트에서 기존 네 경로, 전체 테스트에서 SQLite 영속화 한 경로가
  discussion에서 시작한 투표를 voting에 적용하는 오래된 경합에 의존함을 발견했다.
  fixture가 discussion 응답을 먼저 끝낸 뒤 실제 voting 호출에서 투표하도록 수정했다.
- 제안 메시지가 없는 voting 호출의 본문 fallback, 정상 inbox 경로의 비중복,
  discussion→voting 늦은 투표 거부·재시도를 신규 밀폐 테스트로 검증했다.
- 전체 unittest 510개 통과(7.928s), `git diff --check` 통과.

## 2026-07-14 — M5.3 재투표 호출 세대 격리 (fix/voting-generation-cancel)

### 실측 원인
- 실안건 `f26e2d65-000000`은 작업 48,010/60,000에서 v1 반려 사유를 반영한
  v2까지 생성했지만, research의 v1 투표 호출이 179초 동안 반환되지 않고 예약
  6k를 잡았다. v2의 critic 호출까지 예약하면 60,010이 되어 입장 게이트에서
  대기했고, v2 사용량 0인 채 120초 후 두 명 모두 기권하여 `no_quorum`이 됐다.
- 이전 버전의 호출이 `proposal_id`를 생략한 채 늦게 반환하면 활성 v2에 투표할
  수 있는 별도 세대 혼동 위험도 확인했다.

### 수정
- AgentLoop의 LLM 완료 호출을 취소 가능한 자식 태스크로 분리했다. 제안 해소 시
  해당 버전을 검토 중인 호출만 취소하고, 예약 반환 후 outer 루프는 다음 제안을
  기다린다. 세션 자체 취소는 기존처럼 outer 취소로 전파한다.
- voting 입장 시 proposal id를 호출 문맥에 캡처하고, id 생략 투표도 캡처된
  버전에 귀속시켰다. 취소를 늦게 따르는 provider의 낡은 응답은 superseded
  제안 투표로 무시되고 새 제안 id와 재시도 방법을 받는다.

### 오류/검증
- 자식 태스크 도입으로 fake LLM의 즉시 응답에도 스케줄 지점이 생겨, voting 경계
  채팅 오류가 `tool not offered` 대신 동등한 상태 거부 문구로 나올 수 있음을 기존
  경합 테스트에서 확인했다. 테스트는 두 합법 경로 모두 명시적으로 검증하게 했다.
- 3인 팀에서 느린 v1 심의자 + 다른 심의자의 반려 + v2 재투표를 재현하고, 정상
  취소 후 같은 심의자의 v2 참여 및 취소 지연 provider의 늦은 v1 반대가 v2를
  오염하지 않음을 검증했다.
- 전체 unittest 508개 통과(8.348s), compileall과 `git diff --check` 통과.

## 2026-07-14 — M5.2 첫 실안건 보정: 결정 슬롯 보존 (fix/m5-decision-reserve)

### 실측 원인
- M5.1 병합 후 Standard 첫 실안건(`34a4aec8-000000`)은 작업 43,324에 proposal
  단계로 전환했지만 일반 채팅 30개를 이미 소진했다. 상대등이 작업 56,688에
  정상 제안 v1을 제출하자 `result_proposal`이 31번째 메시지로 계산되어
  `failed(messages)`로 종료됐고 투표는 0건이었다. 작업 56,688/60,000,
  캐시 읽기 22,016, 전체 처리 78,704/150,000으로 토큰 상한은 넘지 않았다.
- synthesis 중 research의 응답 하나가 `send_message` 4개를 실행했고, proposal
  전환 뒤 정산된 critic의 늦은 호출은 채팅이 거부된 뒤 12번째 턴을 소진했다.
  제안 초안은 정상 보존됐으므로 결과 생성 실패가 아니라 토론 한도와 결정 절차가
  같은 카운터를 공유한 경계 결함으로 확정했다.

### 수정
- D-033에 따라 `max_messages`를 일반 채팅 상한으로 재정의했다. 상한 도달 시
  실패하지 않고 proposal로 전환하며 제안·투표 메시지는 제외한다.
- 다음 호출의 실제 작업량+진행 예약+새 예약이 25k/40k 임계값을 넘으면 진행 호출을
  먼저 정산하고 다음 단계로 선제 전환한다. 한 응답의 `send_message`는 한 번만
  실행하고 나머지는 구조화된 도구 오류로 반환한다.
- `max_turns`에 닿은 결정 가능 에이전트는 대기 상태로 보존한다. 일반 토론 호출은
  더 받지 않지만 proposal/voting/revision에서는 기존 단계별 2회 상한 안에서
  제출·투표·수정을 수행할 수 있다.

### 오류/검증
- 선제 proposal 전환 후 실제 usage 동기화가 work<proposal_by만 보고 synthesis로
  되돌리면서 `_before_agent_call`이 무한 반복하는 회귀를 신규 테스트에서 발견했다.
  proposal/revision은 usage 동기화가 후퇴시키지 않는 단조 단계로 고정했다.
- 밀폐 회귀 테스트로 채팅 상한 뒤 제안·투표 완료, 한 응답 다중 채팅 1개만 전달,
  예약 기반 선제 단계 전환, 턴 한도 뒤 제출·투표, 결정 비행동 최대 2회를 검증했다.
- 코드 정밀 렌즈에서 제출자의 토론 턴만 보존하면 임계값 전 전원이 대기해 idle로
  끝날 수 있는 경계를 발견해, 제출자 턴 소진 자체를 proposal 전환 조건으로 삼았다.
  신규 사용자 렌즈에서는 팀 화면을 일반 채팅 상한·토론 턴 의미로 고쳤고, 문서
  렌즈에서는 레거시 `messages` 실패값과 신규 강제 수렴 동작을 구분했다.
- 전체 unittest 506개를 3회 반복 통과(7.759s / 7.754s / 7.852s)했고 Python
  compileall, JavaScript 구문 검사, `git diff --check`도 통과했다.

## 2026-07-14 — M5.1 예산·수렴 제어 구현 (feat/m5-budget-control)

### 진행한 작업
- D-032에 따라 `work_tokens`(입력+출력+캐시 쓰기)와 캐시 읽기를 포함한
  `processed_tokens`를 분리했다. Standard는 작업 60k/전체 처리 150k, 종합 25k,
  제안 40k, 호출 예약 6k, 메시지 30개, 에이전트 12턴, 제안 2개로 설정했다.
- 호출 전 예약을 원자적으로 잡아 예산 말미의 동시 호출을 직렬화하고, 실제 사용량
  정산 또는 오류·취소 때 예약을 반환한다. discussion→synthesis→proposal→voting→
  revision 단계에서 도구를 줄이며, proposal/revision/voting의 비행동 응답은 교정
  호출을 한 번만 허용한다. voting 채팅 금지, 원 제출자만 수정, 두 번째 반려 종료를
  런타임 계약으로 강제했다.
- Quick(2인, 작업 25k), Standard/default(3인, 60k), Deep(3인, 100k) YAML을
  제공하고 콘솔은 프로필 이름 또는 YAML 경로를 받는다. 서버의 `default_team`을
  대시보드 선택에 반영하고 홈 선택지에 등급·작업 예산·인원수를 표시한다.
- REST/SSE/SQLite 팀 스냅샷에 새 종료 정책을 하위 호환 방식으로 반영했다. usage
  이벤트와 콘솔·대시보드는 작업/캐시 재사용/전체 처리를 분리하고 단계와 두 상한을
  표시한다. 실패 화면에는 원인별 한국어 다음 행동을 제공한다.

### 오류/리뷰 발견과 수정
- `max_turns`를 소진한 제출자 큐에 단계 알림이 남아 idle 판정을 영구 차단하는 교착을
  재현했다. 소진 에이전트를 버스에서 비활성화하고 제출자 부재는 즉시 budget 실패,
  소진 심의자는 즉시 기권 처리하도록 수정했다.
- provider 계약 밖 예외가 에이전트를 THINKING에 남겨 세션이 무기한 대기하던 경로를
  `runtime_error`로 정규화했다. 원 예외 문자열은 사용자 기록에 노출하지 않는다.
- 이전 호출의 단계 권한으로 늦은 채팅이나 비원제출자 수정이 가능했던 우회를 현재
  단계 가드와 호출별 offered-tool 검증으로 차단했다.
- 예약량이 25k/40k 단계 임계값을 최대 12k 앞당기던 표류를 수정해 단계는 실제
  `work_tokens`로만 전환하고, 예약은 호출 입장 판정에만 사용한다.
- 신규 사용자 리뷰에서 `serve --team`과 홈 기본 선택 불일치, 콘솔의 캐시 포함 합계와
  작업 예산 혼용, 프로필 비용 차이 미노출, 내부 영어 실패 상세를 발견해 수정했다.
- 마지막 워크스루에서 잘못된 서버 기본 팀이 브라우저의 첫 옵션(정렬상 Deep)으로
  암묵 대체될 수 있음을 발견했다. `/teams`에서 기본 팀 존재를 검증해 400으로 막고,
  대시보드도 기본 팀 불일치 시 제출을 비활성화했다. CLI는 잘못된 프로필에
  `quick/default/deep`과 YAML 경로 사용법을 안내한다.
- 문서의 D-024 voting 채팅, 합의 모드, Deep 제안 수, 반려 후 흐름과 실제 코드를
  D-032 기준으로 정합화했다.

### 검증과 후속
- 전체 unittest 502개를 3회 반복 통과(6.600s / 6.684s / 6.684s)했고,
  JavaScript 구문 검사, Python compileall, `git diff --check`, 밀폐 콘솔
  제출→usage 단계→완료 스모크를 통과했다. 코드 정밀 / 신규 사용자 / 문서-코드
  정합 3렌즈에서 발견한 항목을 수정하고 재검토했다.
- 실 API를 쓰는 대표 기술 안건 10건은 의도적으로 실행하지 않았다. 불필요한 토큰
  지출 없이 기능 계약을 먼저 확정했으며, 8/10 완료·작업 중앙값 40k 이하·40k 이전
  첫 제안 90%라는 튜닝 기준은 후속 실사용 표본으로 측정한다.
- 이 브랜치는 아직 열린 M4 PR #5의 커밋 위에 쌓여 있다. M4가 main에 squash merge된
  뒤 기준 브랜치를 정리해 M5 PR을 병합한다.

## 2026-07-14 — M4 완료: localhost 웹 대시보드 (feat/m4-dashboard)

### 진행한 작업
- D-031에 따라 Node 빌드 체인 없이 `src/hwabaek/dashboard/`의 정적
  HTML/CSS/JavaScript를 FastAPI `/app/`에서 제공하고 `/`를 `/app/`으로 연결.
  홈/제출, 최근 최대 200개 세션, REST+SSE 상세, 읽기 전용 팀의 네 화면 구현.
- 상세 화면에 에이전트 상태·논의 타임라인·제안/투표·토큰 예산·경과 시간·결과/초안,
  취소·결과 복사·서버 health 감지와 반응형/키보드/aria-live 접근성을 반영.
  시스템 프롬프트를 제외한 실행 팀 요약을 상세 응답에 추가하고 package-data에 정적
  자산을 포함.
- 코드 정밀 / 신규 사용자 워크스루 / 문서-코드 정합 3렌즈에서 발견한 사항을 수정:
  - 잘못된 YAML의 API 오류에서 비밀값·원문·절대 경로가 노출되던 P1을 고정 공개
    메시지와 프론트의 원문 미표시로 차단하고 회귀 테스트 추가.
  - 종료 상태와 write-behind 저장 사이 경합에서 빈 REST 배열이 SSE 기록을 지우던
    P1을 서버 flush 대기 + 클라이언트 ID 병합으로 이중 보강.
  - 최신 REST에 과거 SSE backlog를 적용할 때 상태가 역행하고 완료 에이전트가
    thinking으로 남던 문제, 전체 재렌더의 포커스 유실, 화면 이동 후 늦은 응답이 현재
    화면을 덮는 문제를 종료 상태 우선·포커스 복원·route/source 소유권 검사로 수정.
  - 기본 팀 선택, 종료 정책 전체 표시, 본문/보조 열 비율, 합의 이벤트·에이전트 색상,
    모바일 연결 텍스트, progressbar 상한과 문서의 URL·nullable team 의미를 정합화.
- 검증: 전체 테스트 **474개, 3회 반복 통과**(5.890s / 5.906s / 5.913s),
  `node --check`, 실제 `--fake --db` 서버의 redirect/HTML/JS MIME/제출→완료/저장
  메시지·제안/팀 스냅샷/SSE result 스모크 통과. wheel에 정적 자산 3개 포함 확인.

### 오류/이슈 (모두 수정 완료)
- 초기 문서 패치가 실제 anchor 차이로 적용되지 않음 → 파일을 다시 읽고 작은 패치로
  분리. JavaScript 작성 전 정적 테스트의 `/app/app.js` 404는 자산 추가 후 통과.
- 완료 직후 테스트 helper가 제안만 보고 조기 반환해 메시지 write-behind 경합 노출 →
  두 레코드가 모두 저장될 때까지 기다리도록 수정하고, 서버 자체 일관성 테스트 추가.
- Windows가 `.js`를 `text/plain`으로 판정 → `text/javascript` MIME 등록. 정적 계약
  테스트가 literal hash route를 요구 → 명시적 route 상수 추가.
- venv에 setuptools가 없어 `--no-build-isolation` wheel 검사가 실패 → 정상 격리
  빌드로 검증해 프로젝트 wheel과 정적 자산 포함을 확인.
- 첫 실서버 스모크에서 PowerShell 7 전용 `SkipHttpErrorCheck`를 사용 → PowerShell
  5.1 호환 `curl.exe` 상태 검사로 교체해 통과. 모든 임시 서버·DB는 정리.

### 후속
- localhost 전용·무인증 결정(D-012)을 유지한다. LAN 공유·공개 배포는 인증 설계와
  함께 별도 마일스톤에서 검토한다.
- SQLite 이벤트 페이지 단위 replay/compaction과 브라우저 실행형 E2E는 M5에서 보강.

## 2026-07-14 — M3 완료: FastAPI REST/SSE 서버 + 재시작 복원 (feat/m3-server)

### 진행한 작업
- 서버 코어 커밋 `8374d8a`: FastAPI 조립, `POST/GET /sessions`, 취소, 팀 목록,
  세션 상세, health, SSE 실시간 스트림·backlog replay·`Last-Event-ID`, SQLite
  재시작 복원, `python -m hwabaek.serve` 진입점과 밀폐 테스트 구현.
- 완료 기준 실서버 검증: health/teams/세션 생성·조회/종료 세션 cancel 409/SSE
  sequence 0..7/`Last-Event-ID: 1` 재개 sequence 2/잘못된 헤더 400 확인.
  같은 임시 DB로 서버를 재기동해 완료 세션이 메시지 1건·제안 1건과 함께 조회됨.
- 코드 정밀 / 신규 사용자 워크스루 / 문서-코드 일치 3렌즈를 독립 실행해 발견 사항을
  일괄 수정:
  - 재시작 `failed(interrupted)` 전환에 다음 sequence의 `session_status` 이벤트를
    함께 영속화.
  - graceful shutdown을 사용자 `cancelled`와 구분해 `failed(interrupted)`로 기록.
  - LLM 팩토리 조립 예외를 `failed(agent_error)`로 종결하고 agent/writer 자원 정리.
  - 공백 task, fake 모드의 잘못된 명시 team, 충돌하는 `--db/--no-db`, 음수·비정수·
    SQLite 범위 초과 `Last-Event-ID`를 요청 경계에서 거부.
  - 느린 SSE 구독자 큐를 512개로 제한하고 초과 연결은 재접속으로 복구하도록 종료.
- 문서 정합: README PowerShell 5.1 실행 예시·REST 응답표, IA의 M3 데이터 연결,
  EventContract의 실제 SSE frame/replay/복원 규칙, DecisionLog의 D-022 필드명과
  중복 D-028, Plan의 M3 상태를 현재 코드와 맞춤.
- 전체 테스트 **467개, 3회 반복 통과**(5.804s / 5.776s / 5.704s).

### 오류/이슈 (모두 수정 완료)
- 초기 수동 재현에서 `Session` 인자명을 `team`으로 잘못 써 TypeError 발생 → 계약의
  `team_name`으로 바로잡아 재현, 상태 갱신만 되고 이벤트가 0건인 결함을 확정.
- 대상 unittest 클래스명을 `ServerAPITest`로 잘못 호출해 loader 오류 → 실제
  `ServerApiTest`로 재실행해 통과.
- 문서 일괄 패치가 중복 D-028의 실제 문구 차이로 검증 실패 → 어떤 파일도 부분 적용되지
  않은 것을 확인하고 정확한 범위를 읽어 작은 패치로 분리 적용.
- 테스트 시 `fastapi.testclient`의 httpx→httpx2 전환 deprecation warning이 출력되나
  기능 실패는 없으며 3회 반복 결과는 안정적. 의존성 전환은 별도 호환성 검토 대상으로 둠.

### 후속
- SQLite 과거 이벤트의 페이지 단위 replay/compaction은 M5 견고화에서 처리.
- M4 URL namespace는 D-031의 `/app/#/...`로 확정·구현.
- M3 PR #4는 `ca1b918`로 squash merge 완료.

## 2026-07-14 — M2b 완료: 실 세션 합의 성공 (feat/m2b-store)

### 진행한 작업
- D-030(participating_unanimous·max_turns 25) + 관측성/렌더링 보강 반영 후
  4차 실 세션(chatgpt_oauth, 3인 팀) **completed** — 제안 v1에 2/2 approve,
  최종 결과 정상 수령. M2b 완료 기준(실 API 스모크) 충족.
- 세션 타이밍 분석(DB 실증): 총 223초 = 토론 207초 + **투표 15.8초**.
  LLM 호출 28회, 호출 간격 중앙값 2.4초·최대 **150.2초** — 구독 백엔드가
  간헐적으로 호출 1건을 2~3분 지연시키는 것이 체감 지연의 원인(critic·research
  각 1회씩 ~150초대 호출 관측). 우리 쪽 대기 로직 아님. read 타임아웃(180s)
  직전까지 가는 수준 — 지연이 더 심해지면 타임아웃 조정 또는
  reasoning effort 하향(payload `reasoning`은 구독 화이트리스트에 포함) 검토.
- 문서 정합: README 마일스톤 표 M2b 완료 표기, Plan.md M2b 완료 처리.

### 남은 것
- M2b PR 생성 → squash merge. 다음 마일스톤 M3(FastAPI 서버).
- 관찰 항목: 구독 백엔드 간헐 지연(150s+), device flow rate limit.

## 2026-07-14 — M2b: 실 세션 3차 분석(DB 실증) — 관측성·타임아웃·투표 UX 보강 (feat/m2b-store)

### 진행한 작업
- 렌더링/넛지 반영 후 3차 실 세션도 no_quorum. 저장된 세션 DB(events)를 직접
  조회해 타임라인 실증:
  - **critic**: voting 시작(10:52:45) 후 25초 만에 max_turns 15회 소진
    (10:53:10 "max_turns exhausted") — 그 뒤로는 투표가 물리적으로 불가능한
    상태에서 voting_timeout(120s)까지 대기 후 기권 처리.
  - **research**: 첫 LLM 호출(10:52:17)이 세션 종료(10:54:47, 150초)까지
    미완료 — 스트림 무응답으로 세션 내내 THINKING에 갇힘.
  - 코드 버전 검증: 04bcbcb 커밋(19:50:59 KST) < 세션 시작(19:52:17 KST) —
    렌더링/넛지가 적용된 상태에서 발생.
- 보강 4건:
  1. **도구 오류 관측성** (agent.py): ToolError를 모델에게만 돌려주고 이벤트
     무흔적이던 것을 agent_state detail(`tool error [vote_result]: ...`)로
     노출 — 심의자의 투표 실패 여부를 다음 실 세션부터 로그·DB로 확인 가능.
  2. **투표 교정 응답** (session.py): 잘못된/지어낸 proposal_id 투표에
     "vote ignored" 대신 활성 제안 id·버전·제출자와 재시도 방법을 안내.
  3. **스트림 타임아웃** (openai_client.py): 구독 클라이언트에 명시적
     httpx.Timeout(connect 15/read 180/write 30) — 청크 간격이 read를 넘으면
     LLMTimeoutError로 정규화되어 dead 처리, 무한 THINKING 방지.
  4. **CLI 타임스탬프** (run.py): 이벤트 라인에 HH:MM:SS 표시(타이머 디버깅).
  merge_batch 제안 렌더링에 proposal_id 명시, 미투표 리마인더에 활성 제안
  id·버전 포함.
- 테스트 3개 추가(총 **447개 통과**, 핵심 모듈 3회 반복 안정).

### 남은 것 / 사용자 결정 필요
- critic이 채팅으로 턴을 소진해 투표 불능이 되는 문제의 구조적 대응:
  max_turns 상향(15→20+) 여부, voting 중 심의자 채팅 정책(D-024) 재검토 여부.
- unanimous 모드에서 심의자 1명이 hang/dead이면 no_quorum이 보장되는 문제:
  기본 팀을 participating_unanimous(+minimum_votes)로 바꿀지 여부.
- 다음 실 세션에서 tool error detail 관측으로 critic의 투표 실패 원인 확정.

## 2026-07-14 — M2b: 실 세션 no_quorum 대응 — 제안/투표 렌더링과 미투표 넛지 (feat/m2b-store)

### 진행한 작업
- 어댑터 수정 후 사용자 실 세션(3인 팀) 재실행: 인증·스트리밍·협업·제안·초안
  보존(D-025)까지 전부 정상 동작. 단 **failed(no_quorum)** — 심의자 2명이
  제안에 "동의합니다" 채팅만 보내고 vote_result를 호출하지 않아 voting_timeout
  만료 시 전원 기권 처리.
- 원인: 런타임이 result_proposal을 일반 채팅과 동일하게 렌더링(`[from: x]`
  태그뿐) — 시스템 프롬프트의 투표 규칙만으로는 모델이 "지금이 투표
  시점"임을 행동으로 연결하지 못함. 런타임 계층에서 2중 대응:
  1. **merge_batch 타입별 렌더링** (agent.py): result_proposal은
     `[result proposal from x]` 마커 + `[action required]` 투표 지시(채팅은
     투표가 아님·미투표는 기권 명시), vote는 `[vote from x: approve|reject]`
     + 사유로 렌더링.
  2. **미투표 넛지** (session.py send_message): voting 중 스냅샷 심의자 중
     미투표자(tally.pending)가 채팅을 보내면 tool result에 "you have NOT
     voted ..." 리마인더를 부착.
- 테스트 6개 추가(총 **444개 통과**, 통합 3회 반복 안정): merge_batch 렌더링
  4건(신규 tests/test_agent.py) + 통합 2건(voting 중 채팅에 리마인더 부착 /
  running 중에는 미부착).

### 남은 것
- 사용자 재실행으로 합의 도달 확인 → M2b 완료(PR).

## 2026-07-14 — M2b: 실 API 스모크 → 구독 백엔드 실측 대응 + dead 상태 버그 (feat/m2b-store)

### 진행한 작업
- 사용자 실계정 스모크(3인 팀, chatgpt_oauth)에서 전 에이전트 client_error 사망 →
  원인 진단을 위해 스크래치 스크립트로 구독 백엔드에 변형 요청을 직접 보내 400
  본문을 실측. **구독 백엔드 강제 사항 3건 확정** (Research §6 실측 결과에 기록):
  1. `store=false` 필수, 2. `stream=true` 필수(비스트리밍 400),
  3. `prompt_cache_breakpoint` 거부("not supported on this model").
- 어댑터 대응 (openai_client.py):
  - chatgpt_oauth payload에 `store=False`/`stream=True` 강제, 명시적 캐시
    breakpoint 미배치(이 모드에서 캐싱 opt-in 오프).
  - `_stream_final_response` 신설 — SSE 이벤트를 집계해 완성 응답으로 복원.
    실측상 종결 스냅샷(response.completed)의 output이 **비어 있어**,
    `response.output_item.done`의 완성 아이템(message/function_call)을 수집해
    `_ResponseView`로 보강(스냅샷 비변형, usage는 스냅샷 것 사용).
  - response.failed는 LLMServerError로 정규화(error.message 미포함 — 마스킹).
- 실계정 재검증: 텍스트 응답("안녕하세요!") + tool call(submit_result 인자 복원)
  모두 어댑터 경로로 성공. **gpt-5.6-terra 구독 백엔드 지원 실측 확정.**
- 테스트 12개 추가(총 **438개 통과**): payload store/stream/breakpoint 단언,
  스트리밍 집계(완성/보강/스냅샷 우선/incomplete/failed/무종결), api_key 모드
  비스트리밍 유지, 전원 사망 회귀(아래).

### 오류/이슈 (수정 완료)
- (agent/session) **dead 상태 덮어쓰기로 실패 사유 오분류**: 실 스모크에서 전원
  사망인데 failed(agent_error)가 아닌 failed(idle)로 종료. 원인 — AgentLoop가
  fatal 후에도 루프를 계속 돌며 IDLE을 보고해 `_agent_states`의 DEAD가 IDLE로
  덮어써지고 생존자 수가 부풀어 agent_error 판정이 누락. 수정 2중:
  ① AgentLoop `_dead` 플래그로 fatal 후 루프 완전 종료(인박스 소비도 중단),
  ② SessionManager `_on_agent_state`에서 DEAD를 종결 상태로 보호(덮어쓰기 무시).
  회귀 테스트를 수정 전 코드에 돌려 동일 오분류(IDLE≠AGENT_ERROR) 재현 확인.
- (어댑터) 스트림 집계 1차 구현이 종결 스냅샷만 신뢰해 **text가 빈 문자열** —
  구독 백엔드는 스냅샷에 output을 싣지 않는 것을 실측으로 확인, done 아이템
  보강으로 해결.

### 남은 것
- 사용자 재실행으로 3인 팀 전체 세션 E2E 확인 → 통과 시 M2b 완료(PR).
- device flow rate limit 미계측(실사용 중 관찰), 구독 백엔드 암묵 캐싱 여부 미확인.

## 2026-07-14 — M2b: chatgpt_oauth CLI 연결 마무리 (feat/m2b-store)

### 진행한 작업
- 직전 WIP 커밋(a54030d)의 TODO 소화: 전체 테스트 스위트 실행(.venv 재구성 —
  Python 3.14, editable install) → **426개 통과**. test_chatgpt_auth.py 16개
  (device flow 왕복/refresh/실패 경로/토큰 마스킹/payload 화이트리스트/클라이언트
  구성) 포함 확인.
- CLI 실동작 스모크 3종:
  - `--fake --db <임시경로>`: 전체 스택 관통 + SQLite 저장 확인 (exit 0).
  - `--auth chatgpt_oauth` (토큰 없음): 로그인 안내 + exit 2 — 아래 버그 수정 후.
  - `--auth api_key` (키 없음): 기존 안내 유지 확인 (exit 2).
- README 정합화: 실행 절에 chatgpt_oauth 로그인·사용 커맨드와 `--db`/`--no-db`
  추가, D-026 고지를 실제 구현 상태로 갱신(비공식 경로 제거 리스크, 사후 집계
  예산, **미실측 항목 명시** — gpt-5.6 구독 백엔드 지원/stream·accept 헤더 강제),
  M2b 상태를 "진행 중 (실 API 스모크 남음)"으로 표기.

### 오류/이슈 (수정 완료)
- (CLI) `--auth chatgpt_oauth`로 토큰 없이 실행하면 로그인 안내 대신 **원시
  traceback**이 그대로 노출 — run.py `_real_llm_factory`가 OpenAIClient 구성
  시점의 LLMAuthError를 잡지 않았다. api_key 분기와 동일하게 catch → 한 줄
  안내(`error: chatgpt login required: ...`) + exit 2로 수정 (메시지는 토큰
  미포함이라 그대로 출력해도 안전).

### 남은 것 (M2b 완료 조건)
- **실 API 스모크** — 사용자 자원 필요: OPENAI_API_KEY(api_key 모드) 및/또는
  실계정 `chatgpt_auth login`(chatgpt_oauth 모드 — stream/accept 헤더 강제,
  gpt-5.6 구독 백엔드 지원 실측이 여기서만 가능). Fake 통과만으로 M2 완료 처리
  금지(체크리스트 원칙).

## 2026-07-14 — M2a 머지 (PR #2)

### 진행한 작업
- M2a PR(#2) squash merge → main, 작업 브랜치 삭제. main에서 전체 테스트
  377개 통과 확인.
- main 히스토리: PR 단위 유지 (#1 M1 계약, #2 M2a 코어+기본 팀).

### 다음 할 일
- **M2b** (`feat/m2b-store` 브랜치): store/sqlite.py 접목, chatgpt_oauth 인증
  모드, 도메인 이벤트 taxonomy 확정(EventContract §8), **실 API 스모크**
- 실 API 스모크 전 확인: OPENAI_API_KEY 준비 (대등 3인 세션 1회 실행 비용 발생)

## 2026-07-14 — 기본 팀 확정(대등 3인) + capabilities 도구 권한 (feat/m2a-core, D-027)

### 진행한 작업
- 사용자 최종안 채택: 기본 팀을 **research_daedeung / critic_daedeung /
  sangdaedeung** 3인 구조로 교체 (첫 턴 행동 강제, 반대를 위한 반대 방지,
  투표·메시지 구분 프롬프트 포함). 제한: 60msg/100k tokens/idle 45s/voting 120s.
- **capabilities 계약 신설** (직접 작성): `AgentCapability` 3종 + `AgentSpec.
  capabilities`(기본 전체 권한) + TeamConfig 검증 2건(제출 가능 에이전트 필수,
  비-first 모드에서 각 제출자마다 다른 투표 가능 에이전트 필요). SessionManager
  `_guard`에 권한 축 추가(상태 축과 이중 검증), **심의자 스냅샷 자격 = 생존 ∧
  vote_result 권한**으로 갱신 — 검토에서 발견한 스냅샷-권한 상호작용 버그
  (투표 불가 심의자 → unanimous 상시 no_quorum) 사전 차단.
- 사용자 제안에서 3건 조정(D-027에 근거 기록): 기본값 전체 권한(하위 호환),
  ToolError 재사용, (str, Enum) 관례 유지.
- 병렬 위임: 로더 capabilities 파싱 + 기본 팀 검증(sonnet, 테스트 39개),
  계약·통합 capability 테스트(opus, +13개 — 권한 밖 submit 거부, 스냅샷 제외).
- 전체 테스트 **377개, 3회 반복 통과** + --fake 스모크 + 기본 팀 로드 확인.

## 2026-07-14 — M2a 코어 엔진 구현 (feat/m2a-core)

### 진행한 작업
- **인터페이스 우선**: bus.py/consensus.py의 시그니처·독스트링(모듈 계약)을 직접
  확정해 선 커밋 → 병렬 구현의 드리프트 방지.
- **병렬 위임 (opus ×4)**: MessageBus(테스트 19 — 실패 post의 시퀀스 미소비,
  원자 drain, wake 동기화), ConsensusEngine(26 — supersede 관측용 last_superseded
  프로퍼티 추가), OpenAI 어댑터(23 — SDK 타입에서 명시적 캐시 breakpoint 확인·적용,
  usage 비중첩 분해, 오류 정규화 시 원문 미포함으로 키 유출 차단, 절단된 tool call
  파싱 크래시 발견·수정), 세션 통합 테스트(13 시나리오 — 실패 경로 전체 + 타이머
  레이스 + 취소 후 호출 금지 + 종료 후 명령 감사 기록).
- **조립 계층 직접 구현**: agent.py(도구 3종 스키마, 배치 병합, 이력 절단, 구조화
  tool error), session.py(SessionManager — 단일 코디네이터 종료 직렬화, 타이머 2종
  단일 감시, 판정-전환 분리, no_quorum fail_detail 의무, 미승인 초안 보존),
  run.py(CLI — --fake 밀폐 스모크 / 실 API는 OPENAI_API_KEY).
- 설계 조정 2건: vote_result 도구의 proposal_id를 생략 가능(활성 제안 해석 —
  Vote 레코드에는 항상 실제 id)으로 완화, 제안 시점 즉시 판정(first APPROVED /
  심의자 0명 NO_QUORUM)을 _apply_outcome으로 일원화(리뷰에서 발견한 엣지).
- CLI --fake 전체 스택 관통 스모크 성공. 전체 테스트 **355개, 3회 반복 통과**.

### 오류/이슈 (수정 완료)
- (어댑터) 절단된 function_call의 인자 JSON을 즉시 파싱해 크래시 — TOOL_USE 확정
  후로 파싱을 미뤄 해결 (테스트가 발견).
- (세션) 심의자 0명 제안이 voting_timeout까지 불필요 대기 — 즉시 no_quorum 처리.

### 남은 것 (M2b)
- store/sqlite.py 접목, chatgpt_oauth 인증 모드, 도메인 이벤트 taxonomy 확정,
  **실 API 스모크** (Fake만으로 M2 완료 처리 금지 — 체크리스트 원칙).

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
