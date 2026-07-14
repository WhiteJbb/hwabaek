"""SQLiteStore 검증 (D-017, M2b) — store/sqlite.py.

test_store_contract.py가 InMemoryStore로 확정한 Store Protocol 계약 의미론을 실제
SQLite 구현으로 전부 재검증하고, SQLite 고유의 요건(파일 재시작 후 조회, draft 초안
왕복, 특수문자 왕복)을 추가로 검증한다.

밀폐: 임시 디렉터리의 db 파일만 사용하고 네트워크·실키에 의존하지 않는다. 시계를
읽지 않고 고정 타임스탬프만 사용한다(결정적 테스트). unittest만 사용한다.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hwabaek.contracts import (
    AgentCapability,
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    Event,
    EventType,
    FailReason,
    Message,
    MessageType,
    ProposalStatus,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    Vote,
    VoteDecision,
)
from hwabaek.store.base import Store
from hwabaek.store.sqlite import SQLiteStore, _team_from_dict, _team_to_dict

# 전역 고정 타임스탬프 (시계 의존 금지).
TS = "2026-07-14T00:00:00Z"
TS2 = "2026-07-14T01:00:00Z"
TS3 = "2026-07-14T02:00:00Z"


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


class _StoreTestBase(unittest.IsolatedAsyncioTestCase):
    """임시 디렉터리에 db 파일을 만들고 SQLiteStore를 여는 공통 셋업."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="hwabaek_store_")
        self.db_path = Path(self._tmpdir) / "store.db"
        self.store = SQLiteStore(self.db_path)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        # Windows 파일 잠금 회피 — 연결을 닫은 뒤 정리한다.
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Protocol 적합성
# ---------------------------------------------------------------------------

class TestSQLiteStoreProtocol(_StoreTestBase):
    async def test_satisfies_store_protocol(self) -> None:
        self.assertIsInstance(self.store, Store)

    async def test_creates_parent_directory(self) -> None:
        nested = Path(self._tmpdir) / "a" / "b" / "c" / "nested.db"
        store = SQLiteStore(nested)
        try:
            self.assertTrue(nested.parent.is_dir())
            await store.save_session(_session())
            self.assertIsNotNone(await store.get_session("sess1"))
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# 세션
# ---------------------------------------------------------------------------

class TestSessionStorage(_StoreTestBase):
    async def test_save_and_get_roundtrip(self) -> None:
        s = _session()
        await self.store.save_session(s)
        self.assertEqual(await self.store.get_session(s.id), s)

    async def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(await self.store.get_session("ghost"))

    async def test_save_session_upserts_by_id(self) -> None:
        s = _session()
        await self.store.save_session(s)
        s2 = s.with_status(SessionStatus.VOTING)
        await self.store.save_session(s2)
        got = await self.store.get_session(s.id)
        self.assertEqual(got.status, SessionStatus.VOTING)
        all_sessions = await self.store.list_sessions()
        self.assertEqual([x.id for x in all_sessions].count(s.id), 1)

    async def test_list_sessions_recent_first_with_limit(self) -> None:
        for i in range(3):
            await self.store.save_session(_session(id=f"s{i}", created_at=TS))
        got = await self.store.list_sessions(limit=2)
        # 같은 created_at이면 나중에 저장된 s2가 먼저 (rowid DESC).
        self.assertEqual([s.id for s in got], ["s2", "s1"])

    async def test_list_sessions_orders_by_created_at_desc(self) -> None:
        # created_at이 다르면 그 값 우선으로 최근 순 정렬.
        await self.store.save_session(_session(id="old", created_at=TS))
        await self.store.save_session(_session(id="new", created_at=TS3))
        await self.store.save_session(_session(id="mid", created_at=TS2))
        got = await self.store.list_sessions()
        self.assertEqual([s.id for s in got], ["new", "mid", "old"])

    async def test_list_sessions_by_status_returns_running_and_voting_only(self) -> None:
        running = _session(id="r1")
        voting = _session(id="v1").with_status(SessionStatus.VOTING)
        completed = (
            _session(id="c1")
            .with_status(SessionStatus.VOTING)
            .with_status(SessionStatus.COMPLETED, result="final",
                         submitted_by="writer", finished_at=TS2)
        )
        for s in (running, voting, completed):
            await self.store.save_session(s)

        got_running = await self.store.list_sessions_by_status(SessionStatus.RUNNING)
        got_voting = await self.store.list_sessions_by_status(SessionStatus.VOTING)

        self.assertEqual([s.id for s in got_running], ["r1"])
        self.assertEqual([s.id for s in got_voting], ["v1"])

    async def test_by_status_reflects_upsert_transition(self) -> None:
        # running으로 저장 후 voting으로 전이하면 running 조회에서 빠진다.
        s = _session(id="t1")
        await self.store.save_session(s)
        await self.store.save_session(s.with_status(SessionStatus.VOTING))
        self.assertEqual(
            await self.store.list_sessions_by_status(SessionStatus.RUNNING), []
        )
        got_voting = await self.store.list_sessions_by_status(SessionStatus.VOTING)
        self.assertEqual([s.id for s in got_voting], ["t1"])


# ---------------------------------------------------------------------------
# 팀 스냅샷
# ---------------------------------------------------------------------------

class TestTeamSnapshotStorage(_StoreTestBase):
    async def test_roundtrip_default_capabilities(self) -> None:
        team = _team()
        await self.store.save_team_snapshot("sess1", team)
        self.assertEqual(await self.store.get_team_snapshot("sess1"), team)

    async def test_missing_returns_none(self) -> None:
        self.assertIsNone(await self.store.get_team_snapshot("ghost"))

    async def test_old_snapshot_without_budget_controls_uses_defaults(self) -> None:
        team = _team()
        data = _team_to_dict(team)
        for key in (
            "processed_token_limit", "synthesis_at", "proposal_by",
            "call_reserve_tokens", "max_proposals",
        ):
            data["termination"].pop(key)
        self.assertEqual(_team_from_dict(data), team)

    async def test_roundtrip_preserves_restricted_and_empty_capabilities(self) -> None:
        # 제한/빈 capabilities와 종료 정책 전체가 왕복 보존되는지 검증.
        team = TeamConfig(
            name="research_team",
            description="a team with mixed capabilities",
            default_model="gpt-5.6-terra",
            agents=(
                # 전체 권한(기본).
                AgentSpec(name="analyst", role="analysis",
                          system_prompt="You analyze."),
                # 제출 권한 없음 — send + vote만.
                AgentSpec(
                    name="writer", role="writing", system_prompt="You write.",
                    model="gpt-5.6-mini", max_turns=7,
                    capabilities=frozenset(
                        {AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT}
                    ),
                ),
                # 관찰만 — 빈 capabilities.
                AgentSpec(name="observer", role="watching",
                          system_prompt="You watch.",
                          capabilities=frozenset()),
            ),
            termination=TerminationPolicy(
                max_messages=42,
                token_budget=123_456,
                processed_token_limit=250_000,
                synthesis_at=40_000,
                proposal_by=80_000,
                call_reserve_tokens=5_000,
                max_proposals=3,
                idle_timeout=12.5,
                approval=ApprovalConfig(
                    mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                    voting_timeout=99.0,
                    minimum_votes=2,
                ),
            ),
        )
        await self.store.save_team_snapshot("sess1", team)
        got = await self.store.get_team_snapshot("sess1")
        self.assertEqual(got, team)
        # capabilities가 정확히 보존됐는지 직접 확인.
        by_name = {a.name: a for a in got.agents}
        self.assertEqual(by_name["analyst"].capabilities,
                         team.agents[0].capabilities)
        self.assertEqual(
            by_name["writer"].capabilities,
            frozenset({AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT}),
        )
        self.assertEqual(by_name["observer"].capabilities, frozenset())
        self.assertEqual(got.termination.approval.minimum_votes, 2)
        self.assertEqual(got.termination.processed_token_limit, 250_000)
        self.assertEqual(got.termination.synthesis_at, 40_000)
        self.assertEqual(got.termination.proposal_by, 80_000)
        self.assertEqual(got.termination.call_reserve_tokens, 5_000)
        self.assertEqual(got.termination.max_proposals, 3)


# ---------------------------------------------------------------------------
# 메시지
# ---------------------------------------------------------------------------

class TestMessageStorage(_StoreTestBase):
    async def test_list_messages_ascending_by_sequence_regardless_of_append_order(
        self,
    ) -> None:
        m2 = _message(id="m2", sequence=2, sender="writer", recipients=("analyst",))
        m0 = _message(id="m0", sequence=0)
        m1 = _message(id="m1", sequence=1, sender="writer", recipients=("analyst",))
        await self.store.append_message(m2)
        await self.store.append_message(m0)
        await self.store.append_message(m1)

        got = await self.store.list_messages("sess1")
        self.assertEqual([m.sequence for m in got], [0, 1, 2])

    async def test_duplicate_append_ignored(self) -> None:
        m = _message()
        await self.store.append_message(m)
        await self.store.append_message(_message(sequence=99))  # 같은 id, 다른 sequence
        got = await self.store.list_messages("sess1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].sequence, 0)

    async def test_message_full_roundtrip_including_vote_fields(self) -> None:
        # VOTE 메시지는 vote/proposal_id를 싣는다 — 왕복에서 보존되는지 검증.
        vote_msg = _message(
            id="mv", sequence=3, sender="analyst", recipients=("*",),
            type=MessageType.VOTE, content="looks good",
            vote=VoteDecision.APPROVE, proposal_id="p1",
        )
        await self.store.append_message(vote_msg)
        got = await self.store.list_messages("sess1")
        self.assertEqual(got, [vote_msg])


# ---------------------------------------------------------------------------
# 제안
# ---------------------------------------------------------------------------

class TestProposalStorage(_StoreTestBase):
    async def test_save_proposal_upserts_status_transition(self) -> None:
        p = _proposal()
        await self.store.save_proposal(p)
        p2 = p.with_status(ProposalStatus.REJECTED)
        await self.store.save_proposal(p2)

        got = await self.store.list_proposals("sess1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].status, ProposalStatus.REJECTED)

    async def test_list_proposals_ascending_by_version(self) -> None:
        p1 = _proposal(id="p1", version=1)
        p2 = _proposal(id="p2", version=2)
        p3 = _proposal(id="p3", version=3)
        await self.store.save_proposal(p3)
        await self.store.save_proposal(p1)
        await self.store.save_proposal(p2)

        got = await self.store.list_proposals("sess1")
        self.assertEqual([p.version for p in got], [1, 2, 3])


# ---------------------------------------------------------------------------
# 투표
# ---------------------------------------------------------------------------

class TestVoteStorage(_StoreTestBase):
    async def test_append_and_list_all_votes_for_session(self) -> None:
        v1 = _vote(id="v1", proposal_id="p1", voter="analyst")
        v2 = _vote(id="v2", proposal_id="p2", voter="writer",
                   decision=VoteDecision.REJECT, reason="needs more detail")
        await self.store.append_vote(v1)
        await self.store.append_vote(v2)

        got = await self.store.list_votes("sess1")
        self.assertEqual({v.id for v in got}, {"v1", "v2"})

    async def test_list_votes_filtered_by_proposal_id(self) -> None:
        v1 = _vote(id="v1", proposal_id="p1", voter="analyst")
        v2 = _vote(id="v2", proposal_id="p2", voter="writer",
                   decision=VoteDecision.REJECT, reason="needs more detail")
        await self.store.append_vote(v1)
        await self.store.append_vote(v2)

        got = await self.store.list_votes("sess1", proposal_id="p1")
        self.assertEqual([v.id for v in got], ["v1"])

    async def test_list_votes_preserves_insertion_order(self) -> None:
        # 조회는 삽입 순서(rowid ASC).
        for i in range(3):
            await self.store.append_vote(
                _vote(id=f"v{i}", proposal_id="p1", voter=f"a{i}")
            )
        got = await self.store.list_votes("sess1")
        self.assertEqual([v.id for v in got], ["v0", "v1", "v2"])

    async def test_duplicate_append_ignored(self) -> None:
        v = _vote()
        await self.store.append_vote(v)
        await self.store.append_vote(v)
        got = await self.store.list_votes("sess1")
        self.assertEqual(len(got), 1)

    async def test_reject_vote_reason_roundtrip(self) -> None:
        v = _vote(id="vr", decision=VoteDecision.REJECT, reason="insufficient")
        await self.store.append_vote(v)
        got = await self.store.list_votes("sess1")
        self.assertEqual(got, [v])


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------

class TestEventStorage(_StoreTestBase):
    async def test_list_events_default_returns_all_ascending(self) -> None:
        e2 = _event(event_id="e2", sequence=2)
        e0 = _event(event_id="e0", sequence=0)
        e1 = _event(event_id="e1", sequence=1)
        await self.store.append_event(e2)
        await self.store.append_event(e0)
        await self.store.append_event(e1)

        got = await self.store.list_events("sess1")
        self.assertEqual([e.sequence for e in got], [0, 1, 2])

    async def test_list_events_after_sequence_returns_only_newer(self) -> None:
        for seq in range(4):
            await self.store.append_event(_event(event_id=f"e{seq}", sequence=seq))

        got = await self.store.list_events("sess1", after_sequence=1)
        self.assertEqual([e.sequence for e in got], [2, 3])

    async def test_duplicate_append_ignored(self) -> None:
        e = _event()
        await self.store.append_event(e)
        await self.store.append_event(e)
        got = await self.store.list_events("sess1")
        self.assertEqual(len(got), 1)

    async def test_event_payload_roundtrip(self) -> None:
        # Event는 from_dict가 없어 스토어가 자체 역직렬화한다 — payload 보존 확인.
        e = _event(
            event_id="ep", sequence=5, type=EventType.SESSION_STATUS,
            payload={"status": "failed", "fail_reason": "no_quorum",
                     "nested": {"votes": [1, 2, 3]}},
        )
        await self.store.append_event(e)
        got = await self.store.list_events("sess1")
        self.assertEqual(got, [e])


# ---------------------------------------------------------------------------
# 수명주기
# ---------------------------------------------------------------------------

class TestStoreClose(_StoreTestBase):
    async def test_close_is_callable_and_idempotent(self) -> None:
        await self.store.close()
        await self.store.close()  # 반복 호출도 안전해야 한다.

    async def test_use_after_close_raises(self) -> None:
        await self.store.close()
        with self.assertRaises(RuntimeError):
            await self.store.get_session("sess1")


# ---------------------------------------------------------------------------
# SQLite 고유 — 파일 재시작 후 조회 (M2b 완료 기준)
# ---------------------------------------------------------------------------

class TestSQLitePersistence(_StoreTestBase):
    async def test_restart_reopen_recovers_full_decision_record(self) -> None:
        # 완료 세션 + 팀 스냅샷 + 메시지 + 제안 + 투표 + 이벤트를 저장하고,
        # 스토어를 닫은 뒤 새 SQLiteStore로 같은 파일을 다시 열어 전부 조회되는지 검증.
        completed = (
            _session(id="done")
            .with_status(SessionStatus.VOTING)
            .with_status(SessionStatus.COMPLETED, result="final answer",
                         submitted_by="writer", finished_at=TS2)
        )
        team = _team()
        chat = _message(id="mc", session_id="done", sequence=0,
                        content="let us begin")
        proposal_msg = _message(
            id="mp", session_id="done", sequence=1, sender="writer",
            recipients=("*",), type=MessageType.RESULT_PROPOSAL,
            content="final answer", proposal_id="p1",
        )
        vote_msg = _message(
            id="mv", session_id="done", sequence=2, sender="analyst",
            recipients=("*",), type=MessageType.VOTE, content="approve",
            vote=VoteDecision.APPROVE, proposal_id="p1",
        )
        proposal = _proposal(id="p1", session_id="done", proposer="writer",
                             version=1).with_status(ProposalStatus.APPROVED)
        vote = _vote(id="v1", session_id="done", proposal_id="p1", voter="analyst")
        event = _event(event_id="e1", session_id="done", sequence=0,
                       type=EventType.RESULT,
                       payload={"result": "final answer", "submitted_by": "writer"})

        await self.store.save_session(completed)
        await self.store.save_team_snapshot("done", team)
        for m in (chat, proposal_msg, vote_msg):
            await self.store.append_message(m)
        await self.store.save_proposal(proposal)
        await self.store.append_vote(vote)
        await self.store.append_event(event)
        await self.store.close()

        # 재시작 시뮬레이션 — 새 인스턴스로 같은 파일을 연다.
        reopened = SQLiteStore(self.db_path)
        try:
            self.assertEqual(await reopened.get_session("done"), completed)
            self.assertEqual(await reopened.get_team_snapshot("done"), team)
            self.assertEqual(
                await reopened.list_messages("done"),
                [chat, proposal_msg, vote_msg],
            )
            self.assertEqual(await reopened.list_proposals("done"), [proposal])
            self.assertEqual(await reopened.list_votes("done"), [vote])
            self.assertEqual(await reopened.list_events("done"), [event])
            # 완료 세션이 목록·상태 조회에도 남아 있어야 한다.
            listed = await reopened.list_sessions()
            self.assertEqual([s.id for s in listed], ["done"])
            self.assertEqual(
                await reopened.list_sessions_by_status(SessionStatus.COMPLETED),
                [completed],
            )
        finally:
            await reopened.close()

    async def test_failed_session_with_draft_roundtrip(self) -> None:
        # draft_result가 있는 failed(no_quorum) 세션의 재시작 왕복.
        failed = (
            _session(id="nq")
            .with_status(SessionStatus.VOTING)
            .with_status(
                SessionStatus.FAILED,
                fail_reason=FailReason.NO_QUORUM,
                fail_detail="quorum not reached: 1 abstained",
                draft_result="an unapproved draft",
                draft_proposer="writer",
                finished_at=TS2,
            )
        )
        await self.store.save_session(failed)
        await self.store.close()

        reopened = SQLiteStore(self.db_path)
        try:
            got = await reopened.get_session("nq")
            self.assertEqual(got, failed)
            self.assertEqual(got.fail_reason, FailReason.NO_QUORUM)
            self.assertEqual(got.draft_result, "an unapproved draft")
            self.assertEqual(got.draft_proposer, "writer")
            self.assertEqual(got.fail_detail, "quorum not reached: 1 abstained")
            by_status = await reopened.list_sessions_by_status(SessionStatus.FAILED)
            self.assertEqual(by_status, [failed])
        finally:
            await reopened.close()

    async def test_special_characters_roundtrip(self) -> None:
        # 한국어·따옴표·개행이 섞인 내용이 왕복에서 손상되지 않아야 한다.
        weird = 'multi\nline "quoted" \'single\' 한국어 결과\t탭 \\백슬래시'
        session = _session(id="sc", task=weird)
        message = _message(id="msc", session_id="sc", sequence=0, content=weird)
        proposal = _proposal(id="psc", session_id="sc", content=weird)
        vote = _vote(id="vsc", session_id="sc", proposal_id="psc",
                     decision=VoteDecision.REJECT, reason=weird)
        event = _event(event_id="esc", session_id="sc", sequence=0,
                       payload={"note": weird})

        await self.store.save_session(session)
        await self.store.append_message(message)
        await self.store.save_proposal(proposal)
        await self.store.append_vote(vote)
        await self.store.append_event(event)
        await self.store.close()

        reopened = SQLiteStore(self.db_path)
        try:
            self.assertEqual((await reopened.get_session("sc")).task, weird)
            self.assertEqual((await reopened.list_messages("sc"))[0].content, weird)
            self.assertEqual((await reopened.list_proposals("sc"))[0].content, weird)
            self.assertEqual((await reopened.list_votes("sc"))[0].reason, weird)
            self.assertEqual(
                (await reopened.list_events("sc"))[0].payload["note"], weird
            )
        finally:
            await reopened.close()


if __name__ == "__main__":
    unittest.main()
