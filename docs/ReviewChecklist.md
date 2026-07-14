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
- [x] PR #4 생성 (`feat/m3-server` → `main`); squash merge 대기

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

## 합의/이벤트 계약 (D-016~D-024)

- [ ] 결과 제안에 version이 존재하고 반려 후 재제출 시 증가하는가
- [ ] 모든 투표가 proposal_id를 참조하는가 (이전 제안 늦은 투표 무시)
- [ ] 상태별 도구 호출 제한이 강제되는가 (running: send/submit, voting: send/vote, 종료: 전부 거부)
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
