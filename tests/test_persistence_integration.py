"""세션 실행 → SQLite 영속화 → 재오픈 조회 통합 테스트 (M2b 완료 기준).

화백의 핵심 산출물은 "최종 답변 + 결정 과정·근거"다 (D-017) — 세션이 끝난 뒤
저장소를 새로 열어 의결 기록 전체(세션/팀/메시지/제안/투표/이벤트)를 복원할 수
있어야 한다. Fake LLM으로 밀폐 실행한다.
"""
from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path

from hwabaek.contracts import (
    AgentCapability,
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    MessageType,
    ProposalStatus,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    VoteDecision,
)
from hwabaek.llm.fake import FakeLLMClient, text_response, tool_response
from hwabaek.session import SessionManager
from hwabaek.store.sqlite import SQLiteStore

TS = "2026-07-14T00:00:00Z"


def _ids():
    counter = itertools.count()
    return lambda: f"id-{next(counter):05d}"


def _council_team() -> TeamConfig:
    """제출 1 + 심의 2 구성 — unanimous 정상 합의 경로."""
    prompt = "You are a test agent."
    return TeamConfig(
        name="persist_team",
        agents=(
            AgentSpec(
                name="proposer", role="proposes", system_prompt=prompt,
                capabilities=frozenset(
                    {AgentCapability.SEND_MESSAGE, AgentCapability.SUBMIT_RESULT}
                ),
            ),
            AgentSpec(name="voter_a", role="reviews", system_prompt=prompt),
            AgentSpec(name="voter_b", role="reviews", system_prompt=prompt),
        ),
        termination=TerminationPolicy(
            max_messages=20,
            token_budget=100_000,
            idle_timeout=5.0,
            approval=ApprovalConfig(
                mode=ApprovalPolicy.UNANIMOUS, voting_timeout=5.0
            ),
        ),
    )


def _llm_factory(spec: AgentSpec) -> FakeLLMClient:
    """proposer는 제출, 심의자들은 초안 수신 후 승인."""
    if spec.name == "proposer":
        return FakeLLMClient([
            tool_response("submit_result", {"content": "Final decision: ship it."}),
            text_response("Submitted."),
            text_response("Waiting."),
            text_response("Waiting."),
        ])
    return FakeLLMClient([
        text_response("Ready to review."),
        tool_response("vote_result", {"decision": "approve"}),
        text_response("Voted."),
        text_response("Waiting."),
        text_response("Waiting."),
    ])


class PersistenceIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_session_is_fully_queryable_after_reopen(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "hwabaek.db"

        team = _council_team()
        store = SQLiteStore(db_path)
        manager = SessionManager(
            team,
            "Persist this council decision.",
            llm_factory=_llm_factory,
            clock=lambda: TS,
            id_factory=_ids(),
            store=store,
        )
        session = await manager.run()
        await store.close()
        self.assertIs(session.status, SessionStatus.COMPLETED)

        # 재시작 시나리오 — 새 프로세스가 같은 파일을 여는 것과 동등.
        reopened = SQLiteStore(db_path)
        self.addAsyncCleanup(reopened.close)

        # 세션 스냅샷 (최종 답변).
        stored = await reopened.get_session(session.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIs(stored.status, SessionStatus.COMPLETED)
        self.assertEqual(stored.result, "Final decision: ship it.")
        self.assertEqual(stored.submitted_by, "proposer")
        self.assertEqual(stored.usage, session.usage)

        # 실행 당시 팀 구성 (재현성).
        team_snapshot = await reopened.get_team_snapshot(session.id)
        self.assertIsNotNone(team_snapshot)
        assert team_snapshot is not None
        self.assertEqual(
            {a.name for a in team_snapshot.agents},
            {"proposer", "voter_a", "voter_b"},
        )

        # 메시지 타임라인 — 제안 브로드캐스트와 투표 2건이 sequence 순으로.
        messages = await reopened.list_messages(session.id)
        self.assertEqual(
            [m.sequence for m in messages], sorted(m.sequence for m in messages)
        )
        proposal_messages = [
            m for m in messages if m.type is MessageType.RESULT_PROPOSAL
        ]
        vote_messages = [m for m in messages if m.type is MessageType.VOTE]
        self.assertEqual(len(proposal_messages), 1)
        self.assertEqual(len(vote_messages), 2)

        # 의결 기록 — 승인된 제안 v1 + approve 투표 2건.
        proposals = await reopened.list_proposals(session.id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].version, 1)
        self.assertIs(proposals[0].status, ProposalStatus.APPROVED)
        votes = await reopened.list_votes(session.id, proposals[0].id)
        self.assertEqual(
            sorted(v.voter for v in votes), ["voter_a", "voter_b"]
        )
        self.assertTrue(all(v.decision is VoteDecision.APPROVE for v in votes))

        # 이벤트 스트림 — 전량 저장 + Last-Event-ID 재개 조회.
        events = await reopened.list_events(session.id)
        self.assertGreater(len(events), 0)
        sequences = [e.sequence for e in events]
        self.assertEqual(sequences, sorted(sequences))
        mid = sequences[len(sequences) // 2]
        tail = await reopened.list_events(session.id, after_sequence=mid)
        self.assertTrue(all(e.sequence > mid for e in tail))
        # 마지막 이벤트는 result 또는 최종 session_status여야 한다.
        self.assertIn(events[-1].type.value, ("result", "session_status"))

    async def test_no_quorum_failure_persists_draft(self) -> None:
        """실패 세션도 미승인 초안과 실패 사유가 재오픈 후 조회돼야 한다 (D-025)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "hwabaek.db"

        team = _council_team()
        # voting_timeout을 짧게 재구성 — voter들이 투표하지 않는 시나리오.
        team = TeamConfig(
            name=team.name,
            agents=team.agents,
            termination=TerminationPolicy(
                max_messages=20,
                token_budget=100_000,
                idle_timeout=5.0,
                approval=ApprovalConfig(
                    mode=ApprovalPolicy.UNANIMOUS, voting_timeout=0.05
                ),
            ),
        )

        def silent_factory(spec: AgentSpec) -> FakeLLMClient:
            if spec.name == "proposer":
                return FakeLLMClient([
                    tool_response("submit_result", {"content": "Unratified draft."}),
                    text_response("Submitted."),
                    text_response("Waiting."),
                ])
            return FakeLLMClient([
                text_response("Thinking, not voting."),
                text_response("Still not voting."),
                text_response("Still not voting."),
            ])

        store = SQLiteStore(db_path)
        manager = SessionManager(
            team,
            "This will time out in voting.",
            llm_factory=silent_factory,
            clock=lambda: TS,
            id_factory=_ids(),
            store=store,
        )
        session = await manager.run()
        await store.close()
        self.assertIs(session.status, SessionStatus.FAILED)

        reopened = SQLiteStore(db_path)
        self.addAsyncCleanup(reopened.close)
        stored = await reopened.get_session(session.id)
        assert stored is not None
        self.assertEqual(stored.fail_reason.value, "no_quorum")
        self.assertEqual(stored.draft_result, "Unratified draft.")
        self.assertEqual(stored.draft_proposer, "proposer")
        # failed 세션 조회(list_sessions_by_status) — 재시작 interrupted 처리의 반대 확인.
        failed = await reopened.list_sessions_by_status(SessionStatus.FAILED)
        self.assertIn(session.id, [s.id for s in failed])


if __name__ == "__main__":
    unittest.main()
