# ReviewChecklist — 완료 전 점검 목록

> 마일스톤/PR 완료 전에 점검한다. 항목별로 통과 여부를 확인.

## M3 검증 기록 (2026-07-14)

- [x] 전체 467개 테스트 3회 반복 통과
- [x] 실제 uvicorn REST/SSE 스모크와 SQLite 재기동 복원 통과
- [x] 실패 경로: 공백 task, 잘못된 team/Last-Event-ID, 동시 세션, 종료 cancel,
  팩토리 실패, graceful shutdown, 느린 SSE 소비자 검증
- [x] 요청 경로 비블로킹 I/O·API 키 비노출·localhost 바인딩 검토
- [x] 코드 정밀 / 신규 사용자 / 문서-코드 정합 3렌즈 검토와 발견 사항 수정
- [x] README·Plan·IA·EventContract·DecisionLog·WorkLog 정합
- [x] `feat/m3-server` 브랜치 사용, git 산출물 AI 흔적 없음
- [x] PR #4 squash merge 완료 (`ca1b918`), 작업 브랜치 삭제

## M4 검증 기록 (2026-07-14)

- [x] 전체 474개 테스트 3회 반복 통과 (5.890s / 5.906s / 5.913s)
- [x] 실제 `--fake --db` uvicorn에서 `/app/` 정적 자산, 제출→완료, REST 상세,
  저장된 메시지·제안, 팀 스냅샷, SSE 결과 이벤트 통과
- [x] wheel 빌드에 `dashboard/index.html`, `styles.css`, `app.js` 포함 확인
- [x] 설정 오류의 비밀값·YAML 원문·절대 경로 비노출 회귀 테스트 통과
- [x] 종료 write-behind 경합, SSE/REST 기록 병합, 상태 역행, terminal agent 상태,
  포커스 보존, 화면 전환 후 늦은 콜백 소유권 보강
- [x] 코드 정밀 / 신규 사용자 / 문서-코드 정합 3렌즈 검토와 발견 사항 수정
- [x] README·Plan·IA·DecisionLog·WorkLog 정합
- [x] `feat/m4-dashboard` 브랜치 사용, git 산출물 AI 흔적 없음

## M5.1 검증 기록 (2026-07-14)

- [x] 전체 502개 테스트 3회 반복 통과 (6.600s / 6.684s / 6.684s)
- [x] JavaScript 구문 검사, Python compileall, `git diff --check` 통과
- [x] 밀폐 콘솔 제출→단계별 usage→완료 스모크와 잘못된 프로필 안내 확인
- [x] 작업 토큰/캐시 읽기/전체 처리 분리와 두 예산 상한의 계약·저장·REST·UI 검증
- [x] 호출 예약 경합, 단계 권한, 비행동 교정, 원 제출자 수정, 제안 2개 상한,
  취소·예외·턴 소진 시 예약/에이전트 정리 회귀 테스트
- [x] 잘못된 서버 기본 팀이 첫 번째 팀으로 암묵 대체되지 않고 400으로 중단됨
- [x] 코드 정밀 / 신규 사용자 / 문서-코드 정합 3렌즈 검토와 발견 사항 수정
- [x] README·Plan·IA·UserScenarios·EventContract·DecisionLog·WorkLog 정합
- [ ] 실 API 대표 기술 안건 10건의 수렴·사용량 기준 측정 (기능 병합 후 실사용 표본으로 튜닝)
- [x] `feat/m5-budget-control` 브랜치 사용, git 산출물 AI 흔적 없음

## 기능/정확성

- [ ] 전체 테스트 통과: `.venv\Scripts\python.exe -m unittest discover -s tests`
- [ ] 통합 스모크 통과 (실 스택 E2E — Fake 통과만으로 완료 처리 금지)
- [ ] 새 기능에 실패 경로 테스트 존재 (타임아웃/예산 초과/API 오류/refusal/취소)
- [ ] 플레이키 테스트 없음 (반복 실행으로 확인 — 고정 sleep/공유 상태 의심)

## 테스트 밀폐성

- [ ] 테스트가 실 API 키·실 네트워크에 의존하지 않음 (Fake LLM 클라이언트 사용)
- [ ] 모듈 import 부작용이 테스트를 오염시키지 않음

## 보안/안전

- [ ] API 키가 로그·에러 응답·대시보드·이벤트 스트림 어디에도 노출되지 않음 (마스킹 확인)
- [ ] 세션 토큰 예산/메시지 상한이 실제로 강제됨 (무한 대화 차단)
- [ ] 취소 후 추가 API 호출이 발생하지 않음

## 코드 품질/제약

- [ ] 요청 경로에 블로킹 I/O 없음 (파일 쓰기는 write-behind / to_thread)
- [ ] 사용자 노출 문자열(로그, CLI, API 오류 메시지)이 영어 ASCII
- [ ] 오류 귀책 구분이 정확 (클라이언트 잘못 vs API 혼잡 vs 시스템 버그)

## 합의/이벤트 계약 (D-016~D-032)

- [ ] 결과 제안에 version이 존재하고 반려 후 재제출 시 증가하는가
- [ ] 모든 투표가 proposal_id를 참조하는가 (이전 제안 늦은 투표 무시)
- [ ] 상태별 도구 호출 제한이 강제되는가 (running: send/submit, proposal·revision: submit만, voting: vote만, 종료: 전부 거부)
- [ ] voting_timeout과 idle_timeout이 분리되어 있고 voting 중 idle 종료가 발생하지 않는가
- [ ] 세션 종료가 한 번만 확정되는가 (동시 종료 조건 경쟁 시 직렬화 + 우선순위)
- [ ] unanimous가 미투표를 승인으로 처리하지 않는가 (timeout → no_quorum)
- [ ] 종료 후 도착한 이벤트가 상태를 변경하지 않는가 (감사 기록만)
- [ ] 도메인 이벤트에 event_id와 세션 단위 sequence가 있는가
- [ ] 민감정보(API 키·원본 요청 전문)가 로그와 SSE에 노출되지 않는가

## 문서-코드 정합 (이 프로젝트 최다 결함원)

- [ ] 엔드포인트/CLI/설정 스키마 변경이 README·IA.md에 같은 브랜치로 반영됨
- [ ] Plan.md가 실제 구현과 일치 (어긋나면 어느 쪽이 맞는지 먼저 결정)
- [ ] DecisionLog.md에 이번 작업의 새 결정이 기록됨
- [ ] WorkLog.md에 작업/오류 내역 기록됨

## Git

- [ ] 브랜치에서 작업했고 PR 경유 (문서만 수정 제외)
- [ ] 커밋 메시지/PR에 AI 흔적 없음 (Co-Authored-By, 생성 도구 서명 금지)
- [ ] git 인자에 큰따옴표 미사용 (PS 5.1)
