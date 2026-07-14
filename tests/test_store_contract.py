"""Store Protocol 적합성(conformance) 테스트 (D-017).

src/hwabaek/store/base.py가 정의하는 Store Protocol의 계약 의미론(업서트 규칙,
append 멱등성, 정렬 순서, Last-Event-ID 재개 등)을 검증한다. 여기서 쓰는
InMemoryStore는 테스트 전용 더미 구현이며 프로덕션 코드(src/)에는 두지 않는다 —
실제 SQLite 구현(store/sqlite.py)은 M2b에서 별도로 검증한다. contracts.py와
마찬가지로 시계를 읽지 않고 고정 타임스탬프만 사용한다(결정적 테스트).
외부 의존성·네트워크 없음.
"""
from __future__ import annotations

import unittest

from hwabaek.contracts import (
    AgentSpec,
    Event,
    EventType,
    Message,
    MessageType,
    ProposalStatus,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    Vote,
    VoteDecision,
)
from hwabaek.store.base import Store

# 테스트 전역 고정 타임스탬프 (시계 의존 금지).
TS = "2026-07-14T00:00:00Z"
TS2 = "2026-07-14T01:00:00Z"


# ---------------------------------------------------------------------------
# 테스트 전용 InMemoryStore — Store Protocol 계약 의미론 검증용 (src/에 두지 않는다)
# ---------------------------------------------------------------------------

class InMemoryStore:
    """Store Protocol의 최소 구현. 상태는 프로세스 메모리에만 유지한다."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._session_order: list[str] = []  # 최초 저장 순서 = 생성 순서
        self._team_snapshots: dict[str, TeamConfig] = {}
        self._messages: dict[str, list[Message]] = {}
        self._message_ids: dict[str, set[str]] = {}
        self._proposals: dict[str, dict[str, ResultProposal]] = {}
        self._votes: dict[str, list[Vote]] = {}
        self._vote_ids: dict[str, set[str]] = {}
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, set[str]] = {}
        self.closed = False

    # ---- 세션 ----
    async def save_session(self, session: Session) -> None:
        if session.id not in self._sessions:
            self._session_order.append(session.id)
        self._sessions[session.id] = session

    async def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def list_sessions(self, *, limit: int = 50) -> list[Session]:
        recent_first = list(reversed(self._session_order))
        return [self._sessions[sid] for sid in recent_first[:limit]]

    async def list_sessions_by_status(self, status: SessionStatus) -> list[Session]:
        return [s for s in self._sessions.values() if s.status == status]

    # ---- 팀 스냅샷 ----
    async def save_team_snapshot(self, session_id: str, team: TeamConfig) -> None:
        self._team_snapshots[session_id] = team

    async def get_team_snapshot(self, session_id: str) -> TeamConfig | None:
        return self._team_snapshots.get(session_id)

    # ---- 메시지 ----
    async def append_message(self, message: Message) -> None:
        ids = self._message_ids.setdefault(message.session_id, set())
        if message.id in ids:
            return  # 중복 배달 무시 (D-023)
        ids.add(message.id)
        self._messages.setdefault(message.session_id, []).append(message)

    async def list_messages(self, session_id: str) -> list[Message]:
        return sorted(self._messages.get(session_id, []), key=lambda m: m.sequence)

    # ---- 제안 / 투표 ----
    async def save_proposal(self, proposal: ResultProposal) -> None:
        self._proposals.setdefault(proposal.session_id, {})[proposal.id] = proposal

    async def list_proposals(self, session_id: str) -> list[ResultProposal]:
        proposals = self._proposals.get(session_id, {}).values()
        return sorted(proposals, key=lambda p: p.version)

    async def append_vote(self, vote: Vote) -> None:
        ids = self._vote_ids.setdefault(vote.session_id, set())
        if vote.id in ids:
            return  # 중복 배달 무시
        ids.add(vote.id)
        self._votes.setdefault(vote.session_id, []).append(vote)

    async def list_votes(
        self, session_id: str, proposal_id: str | None = None
    ) -> list[Vote]:
        votes = self._votes.get(session_id, [])
        if proposal_id is not None:
            votes = [v for v in votes if v.proposal_id == proposal_id]
        return list(votes)

    # ---- 이벤트 ----
    async def append_event(self, event: Event) -> None:
        ids = self._event_ids.setdefault(event.session_id, set())
        if event.event_id in ids:
            return  # 중복 배달 무시
        ids.add(event.event_id)
        self._events.setdefault(event.session_id, []).append(event)

    async def list_events(
        self, session_id: str, *, after_sequence: int = -1
    ) -> list[Event]:
        events = self._events.get(session_id, [])
        newer = [e for e in events if e.sequence > after_sequence]
        return sorted(newer, key=lambda e: e.sequence)

    # ---- 수명주기 ----
    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 테스트 데이터 팩토리 — contracts.py 실제 타입을 최소 구성으로 생성
# ---------------------------------------------------------------------------

def _session(**overrides) -> Session:
    base = dict(id="sess1", task="do the thing", team_name="research_team",
                created_at=TS)
    base.update(overrides)
    return Session(**base)


def _team(**overrides) -> TeamConfig:
    base = dict(
        name="research_team",
        agents=(
            AgentSpec(name="analyst", role="analysis", system_prompt="You analyze."),
            AgentSpec(name="writer", role="writing", system_prompt="You write."),
        ),
    )
    base.update(overrides)
    return TeamConfig(**base)


def _message(**overrides) -> Message:
    base = dict(
        id="m1", session_id="sess1", sender="analyst", recipients=("writer",),
        type=MessageType.CHAT, content="hello", created_at=TS, sequence=0,
    )
    base.update(overrides)
    return Message(**base)


def _proposal(**overrides) -> ResultProposal:
    base = dict(id="p1", session_id="sess1", proposer="writer", version=1,
                content="draft result", created_at=TS)
    base.update(overrides)
    return ResultProposal(**base)


def _vote(**overrides) -> Vote:
    base = dict(id="v1", session_id="sess1", proposal_id="p1", voter="analyst",
                decision=VoteDecision.APPROVE, created_at=TS)
    base.update(overrides)
    return Vote(**base)


def _event(**overrides) -> Event:
    base = dict(event_id="e1", session_id="sess1", type=EventType.USAGE,
                sequence=0, created_at=TS, payload={})
    base.update(overrides)
    return Event(**base)


# ---------------------------------------------------------------------------
# Protocol 적합성
# ---------------------------------------------------------------------------

class TestStoreProtocolConformance(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_store_satisfies_protocol(self) -> None:
        # runtime_checkable Protocol — 메서드 이름 존재 여부로 isinstance 판정.
        store = InMemoryStore()
        self.assertIsInstance(store, Store)


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

class TestSessionStorage(unittest.IsolatedAsyncioTestCase):
    async def test_save_and_get_roundtrip(self) -> None:
        store = InMemoryStore()
        s = _session()
        await store.save_session(s)
        self.assertEqual(await store.get_session(s.id), s)

    async def test_get_missing_returns_none(self) -> None:
        store = InMemoryStore()
        self.assertIsNone(await store.get_session("ghost"))

    async def test_save_session_upserts_by_id(self) -> None:
        # 같은 id 재저장 시 최신본으로 교체(상태 전이마다 호출됨).
        store = InMemoryStore()
        s = _session()
        await store.save_session(s)
        s2 = s.with_status(SessionStatus.VOTING)
        await store.save_session(s2)
        got = await store.get_session(s.id)
        self.assertEqual(got.status, SessionStatus.VOTING)
        all_sessions = await store.list_sessions()
        self.assertEqual([x.id for x in all_sessions].count(s.id), 1)

    async def test_list_sessions_recent_first_with_limit(self) -> None:
        store = InMemoryStore()
        for i in range(3):
            await store.save_session(_session(id=f"s{i}", created_at=TS))
        got = await store.list_sessions(limit=2)
        # 최근 생성 순 — 가장 나중에 저장된 s2가 먼저.
        self.assertEqual([s.id for s in got], ["s2", "s1"])

    async def test_list_sessions_by_status_returns_running_and_voting_only(self) -> None:
        # 재시작 시 이전 running/voting 세션을 찾아 interrupted 처리하는 시나리오 (D-021).
        store = InMemoryStore()
        running = _session(id="r1")
        voting = _session(id="v1").with_status(SessionStatus.VOTING)
        completed = (
            _session(id="c1")
            .with_status(SessionStatus.VOTING)
            .with_status(SessionStatus.COMPLETED, result="final",
                         submitted_by="writer", finished_at=TS2)
        )
        for s in (running, voting, completed):
            await store.save_session(s)

        got_running = await store.list_sessions_by_status(SessionStatus.RUNNING)
        got_voting = await store.list_sessions_by_status(SessionStatus.VOTING)

        self.assertEqual([s.id for s in got_running], ["r1"])
        self.assertEqual([s.id for s in got_voting], ["v1"])


# ---------------------------------------------------------------------------
# 팀 스냅샷
# ---------------------------------------------------------------------------

class TestTeamSnapshotStorage(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self) -> None:
        store = InMemoryStore()
        team = _team()
        await store.save_team_snapshot("sess1", team)
        self.assertEqual(await store.get_team_snapshot("sess1"), team)

    async def test_missing_returns_none(self) -> None:
        store = InMemoryStore()
        self.assertIsNone(await store.get_team_snapshot("ghost"))


# ---------------------------------------------------------------------------
# 메시지
# ---------------------------------------------------------------------------

class TestMessageStorage(unittest.IsolatedAsyncioTestCase):
    async def test_list_messages_ascending_by_sequence_regardless_of_append_order(
        self,
    ) -> None:
        store = InMemoryStore()
        m2 = _message(id="m2", sequence=2, sender="writer", recipients=("analyst",))
        m0 = _message(id="m0", sequence=0)
        m1 = _message(id="m1", sequence=1, sender="writer", recipients=("analyst",))
        # 일부러 역순으로 append.
        await store.append_message(m2)
        await store.append_message(m0)
        await store.append_message(m1)

        got = await store.list_messages("sess1")
        self.assertEqual([m.sequence for m in got], [0, 1, 2])

    async def test_duplicate_append_ignored(self) -> None:
        # D-023: 같은 id 중복 append는 무시.
        store = InMemoryStore()
        m = _message()
        await store.append_message(m)
        await store.append_message(_message(sequence=99))  # 같은 id, 다른 sequence
        got = await store.list_messages("sess1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].sequence, 0)


# ---------------------------------------------------------------------------
# 제안
# ---------------------------------------------------------------------------

class TestProposalStorage(unittest.IsolatedAsyncioTestCase):
    async def test_save_proposal_upserts_status_transition(self) -> None:
        # pending 저장 후 rejected로 재저장 — 같은 id는 최신 상태로 교체.
        store = InMemoryStore()
        p = _proposal()
        await store.save_proposal(p)
        p2 = p.with_status(ProposalStatus.REJECTED)
        await store.save_proposal(p2)

        got = await store.list_proposals("sess1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].status, ProposalStatus.REJECTED)

    async def test_list_proposals_ascending_by_version(self) -> None:
        store = InMemoryStore()
        p1 = _proposal(id="p1", version=1)
        p2 = _proposal(id="p2", version=2)
        p3 = _proposal(id="p3", version=3)
        # 저장 순서를 뒤섞어도 조회는 version 오름차순.
        await store.save_proposal(p3)
        await store.save_proposal(p1)
        await store.save_proposal(p2)

        got = await store.list_proposals("sess1")
        self.assertEqual([p.version for p in got], [1, 2, 3])


# ---------------------------------------------------------------------------
# 투표
# ---------------------------------------------------------------------------

class TestVoteStorage(unittest.IsolatedAsyncioTestCase):
    async def test_append_and_list_all_votes_for_session(self) -> None:
        store = InMemoryStore()
        v1 = _vote(id="v1", proposal_id="p1", voter="analyst")
        v2 = _vote(id="v2", proposal_id="p2", voter="writer",
                   decision=VoteDecision.REJECT, reason="needs more detail")
        await store.append_vote(v1)
        await store.append_vote(v2)

        got = await store.list_votes("sess1")
        self.assertEqual({v.id for v in got}, {"v1", "v2"})

    async def test_list_votes_filtered_by_proposal_id(self) -> None:
        store = InMemoryStore()
        v1 = _vote(id="v1", proposal_id="p1", voter="analyst")
        v2 = _vote(id="v2", proposal_id="p2", voter="writer",
                   decision=VoteDecision.REJECT, reason="needs more detail")
        await store.append_vote(v1)
        await store.append_vote(v2)

        got = await store.list_votes("sess1", proposal_id="p1")
        self.assertEqual([v.id for v in got], ["v1"])

    async def test_duplicate_append_ignored(self) -> None:
        store = InMemoryStore()
        v = _vote()
        await store.append_vote(v)
        await store.append_vote(v)
        got = await store.list_votes("sess1")
        self.assertEqual(len(got), 1)


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------

class TestEventStorage(unittest.IsolatedAsyncioTestCase):
    async def test_list_events_default_returns_all_ascending(self) -> None:
        store = InMemoryStore()
        e2 = _event(event_id="e2", sequence=2)
        e0 = _event(event_id="e0", sequence=0)
        e1 = _event(event_id="e1", sequence=1)
        await store.append_event(e2)
        await store.append_event(e0)
        await store.append_event(e1)

        got = await store.list_events("sess1")
        self.assertEqual([e.sequence for e in got], [0, 1, 2])

    async def test_list_events_after_sequence_returns_only_newer(self) -> None:
        # Last-Event-ID 재개 시나리오 (EventContract §5).
        store = InMemoryStore()
        for seq in range(4):
            await store.append_event(_event(event_id=f"e{seq}", sequence=seq))

        got = await store.list_events("sess1", after_sequence=1)
        self.assertEqual([e.sequence for e in got], [2, 3])

    async def test_duplicate_append_ignored(self) -> None:
        store = InMemoryStore()
        e = _event()
        await store.append_event(e)
        await store.append_event(e)
        got = await store.list_events("sess1")
        self.assertEqual(len(got), 1)


# ---------------------------------------------------------------------------
# 수명주기
# ---------------------------------------------------------------------------

class TestStoreClose(unittest.IsolatedAsyncioTestCase):
    async def test_close_is_callable(self) -> None:
        store = InMemoryStore()
        await store.close()  # 예외 없이 호출 가능해야 한다.
        self.assertTrue(store.closed)


if __name__ == "__main__":
    unittest.main()
