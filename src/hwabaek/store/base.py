"""저장 계약 — Store Protocol (D-017).

M1에서는 인터페이스만 확정한다. SQLite 구현(store/sqlite.py)은 M2b에서 접목하며,
ORM·이벤트 소싱 프레임워크는 도입하지 않는다. 엔진(M2)은 이 Protocol에만 의존하고,
요청 경로에서는 write-behind로 호출해 블로킹을 피한다(스케줄링은 호출자 책임).

이 계약이 보장해야 하는 조회 (D-017 — "화백의 핵심 산출물은 결정 과정과 근거"):
- 완료/실패/취소 세션 조회 (서버 재시작 후 포함)
- 재시작 시 이전 running/voting 세션 식별 → failed(interrupted) 처리 (D-021)
- 메시지 타임라인 (세션 sequence 오름차순)
- 제안(버전 이력 포함)과 투표 — 의결 기록은 승인된 제안 + 투표 + 세션 결과로 복원
- 이벤트 스트림 재개 (Last-Event-ID: sequence 초과분 조회, EventContract §5)
- 실행 당시 팀 구성 스냅샷 (재현성 — agents 테이블 대응)

참고: D-017의 decisions/usage_events 테이블은 위 레코드들로부터 파생 가능하다 —
별도 레코드/메서드로 둘지는 M2b 스키마 확정 시 결정한다 (usage 이벤트는
Event(type=usage)로 이미 이 계약을 통과한다).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from hwabaek.contracts import (
    Event,
    Message,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    Vote,
)


@runtime_checkable
class Store(Protocol):
    """비동기 저장 계약. 모든 메서드는 요청 경로를 블로킹하지 않아야 한다.

    쓰기 규칙:
    - save_session/save_proposal은 id 기준 upsert — 상태 전이마다 최신본을 저장.
    - append_*는 불변 레코드의 추가 — 같은 id의 중복 append는 무시한다
      (중복 배달 방지, D-023).
    """

    # ---- 세션 ----
    async def save_session(self, session: Session) -> None:
        """세션 upsert (id 기준). 상태 전이·사용량 갱신마다 호출된다."""
        ...

    async def get_session(self, session_id: str) -> Session | None:
        ...

    async def list_sessions(self, *, limit: int = 50) -> list[Session]:
        """최근 생성 순. 대시보드 세션 목록(SC-02)용."""
        ...

    async def list_sessions_by_status(self, status: SessionStatus) -> list[Session]:
        """재시작 시 running/voting 세션을 찾아 interrupted 처리하는 데 사용 (D-021)."""
        ...

    # ---- 팀 스냅샷 (실행 당시 구성 보존 — 재현성) ----
    async def save_team_snapshot(self, session_id: str, team: TeamConfig) -> None:
        ...

    async def get_team_snapshot(self, session_id: str) -> TeamConfig | None:
        ...

    # ---- 메시지 ----
    async def append_message(self, message: Message) -> None:
        ...

    async def list_messages(self, session_id: str) -> list[Message]:
        """세션 sequence 오름차순 — 타임라인 복원 기준 (D-023)."""
        ...

    # ---- 제안 / 투표 (의결 기록) ----
    async def save_proposal(self, proposal: ResultProposal) -> None:
        """제안 upsert (id 기준) — status 전이(pending→…→superseded)를 반영."""
        ...

    async def list_proposals(self, session_id: str) -> list[ResultProposal]:
        """version 오름차순 — 반려·재제출 이력 전체."""
        ...

    async def append_vote(self, vote: Vote) -> None:
        ...

    async def list_votes(
        self, session_id: str, proposal_id: str | None = None
    ) -> list[Vote]:
        """proposal_id를 주면 해당 제안의 투표만, 없으면 세션 전체."""
        ...

    # ---- 이벤트 (session_events) ----
    async def append_event(self, event: Event) -> None:
        ...

    async def list_events(
        self, session_id: str, *, after_sequence: int = -1
    ) -> list[Event]:
        """sequence 오름차순, after_sequence 초과분만 — Last-Event-ID 재개 (EventContract §5)."""
        ...

    # ---- 수명주기 ----
    async def close(self) -> None:
        """보류 중인 write-behind 쓰기를 마저 반영하고 자원을 해제한다."""
        ...
