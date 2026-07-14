"""ConsensusEngine 단위 테스트 (Plan 코어 의미론 §5, D-016/D-018/D-020/D-021).

엔진은 판정만 반환하고 세션 상태를 모른다 — 여기서는 제안 생성/투표 집계/정족수
판정/제안 상태 마감/버전·supersede 전이만 검증한다. 시계·id는 결정적으로 주입해
밀폐한다(고정 clock + 순번 id_factory). 외부 의존성·네트워크 없음.
"""
import unittest

from hwabaek.consensus import ConsensusEngine, ConsensusError, ConsensusState
from hwabaek.contracts import (
    ApprovalConfig,
    ApprovalPolicy,
    ContractError,
    ProposalOutcome,
    ProposalStatus,
    Vote,
    VoteDecision,
)

TS = "2026-07-14T00:00:00Z"


class _SeqIds:
    """순번 id 발급기 — 결정적 유일 id (proposal/vote 공용)."""

    def __init__(self, prefix: str = "id-") -> None:
        self._prefix = prefix
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"{self._prefix}{self._n}"


def _engine(
    mode: ApprovalPolicy = ApprovalPolicy.UNANIMOUS,
    *,
    minimum_votes: int | None = None,
    session_id: str = "s1",
) -> ConsensusEngine:
    approval = ApprovalConfig(mode=mode, minimum_votes=minimum_votes)
    return ConsensusEngine(
        session_id, approval, clock=lambda: TS, id_factory=_SeqIds()
    )


# 편의: alive 집합 상수 (제출자 p, 심의자 a/b/c).
ALIVE3 = frozenset({"p", "a", "b"})
ALIVE4 = frozenset({"p", "a", "b", "c"})


# ---------------------------------------------------------------------------
# 정상 승인 흐름
# ---------------------------------------------------------------------------

class TestApproveFlow(unittest.TestCase):
    def test_unanimous_all_approve_then_resolve_approved(self) -> None:
        eng = _engine()
        state = eng.open_proposal("p", "draft v1", ALIVE3)
        self.assertIsInstance(state, ConsensusState)
        self.assertEqual(state.proposal.version, 1)
        self.assertEqual(state.proposal.status, ProposalStatus.PENDING)
        self.assertEqual(state.tally.voters, frozenset({"a", "b"}))
        self.assertEqual(state.outcome, ProposalOutcome.PENDING)

        # 첫 승인 — 아직 b 미투표라 PENDING.
        res = eng.register_vote("a", VoteDecision.APPROVE, "")
        self.assertIsNotNone(res)
        vote, state = res
        self.assertIsInstance(vote, Vote)
        self.assertEqual(vote.voter, "a")
        self.assertEqual(vote.proposal_id, state.proposal.id)
        self.assertEqual(state.outcome, ProposalOutcome.PENDING)

        # 전원 승인 — APPROVED.
        _, state = eng.register_vote("b", VoteDecision.APPROVE, "")
        self.assertEqual(state.outcome, ProposalOutcome.APPROVED)

        # resolve(APPROVED) → 제안 APPROVED 확정, 활성 내려감.
        resolved = eng.resolve(ProposalOutcome.APPROVED)
        self.assertEqual(resolved.status, ProposalStatus.APPROVED)
        self.assertEqual(resolved.version, 1)
        self.assertIsNone(eng.active)

    def test_vote_carries_reject_reason(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        vote, _ = eng.register_vote("a", VoteDecision.REJECT, "needs sources")
        self.assertEqual(vote.decision, VoteDecision.REJECT)
        self.assertEqual(vote.reason, "needs sources")


# ---------------------------------------------------------------------------
# 반려 → 재제출(version+1) → supersede
# ---------------------------------------------------------------------------

class TestRejectAndResubmit(unittest.TestCase):
    def test_reject_then_resubmit_bumps_version_and_supersedes(self) -> None:
        eng = _engine()
        s1 = eng.open_proposal("p", "draft v1", ALIVE3)
        v1_id = s1.proposal.id

        # 반대 1표 — unanimous는 즉시 REJECTED.
        _, state = eng.register_vote("a", VoteDecision.REJECT, "too vague")
        self.assertEqual(state.outcome, ProposalOutcome.REJECTED)

        rejected = eng.resolve(ProposalOutcome.REJECTED)
        self.assertEqual(rejected.status, ProposalStatus.REJECTED)
        self.assertEqual(rejected.version, 1)
        self.assertIsNone(eng.active)
        # 아직 새 제안이 없으므로 supersede는 발생하지 않았다.
        self.assertIsNone(eng.last_superseded)

        # 재제출 — version 2, 이전 제안은 SUPERSEDED로 전환.
        s2 = eng.open_proposal("p", "draft v2", ALIVE3)
        self.assertEqual(s2.proposal.version, 2)
        self.assertNotEqual(s2.proposal.id, v1_id)
        self.assertIsNotNone(eng.last_superseded)
        self.assertEqual(eng.last_superseded.id, v1_id)
        self.assertEqual(eng.last_superseded.status, ProposalStatus.SUPERSEDED)

    def test_fresh_open_has_no_superseded(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft v1", ALIVE3)
        self.assertIsNone(eng.last_superseded)


# ---------------------------------------------------------------------------
# voting 중 중복 open
# ---------------------------------------------------------------------------

class TestDuplicateOpen(unittest.TestCase):
    def test_open_while_active_raises_consensus_error(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        with self.assertRaises(ConsensusError):
            eng.open_proposal("p", "draft again", ALIVE3)

    def test_consensus_error_is_contract_error(self) -> None:
        # ConsensusError는 ContractError 하위 — 도구 오류로 반환 가능.
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        with self.assertRaises(ContractError):
            eng.open_proposal("p", "again", ALIVE3)


# ---------------------------------------------------------------------------
# 늦은 투표 / 활성 없음 → None 무시
# ---------------------------------------------------------------------------

class TestLateAndInactiveVotes(unittest.TestCase):
    def test_register_vote_without_active_returns_none(self) -> None:
        eng = _engine()
        self.assertIsNone(eng.register_vote("a", VoteDecision.APPROVE, ""))

    def test_register_vote_for_without_active_returns_none(self) -> None:
        eng = _engine()
        self.assertIsNone(
            eng.register_vote_for("old-id", "a", VoteDecision.APPROVE, "")
        )

    def test_register_vote_for_old_proposal_id_is_ignored(self) -> None:
        eng = _engine()
        s1 = eng.open_proposal("p", "draft v1", ALIVE3)
        old_id = s1.proposal.id
        _, _ = eng.register_vote("a", VoteDecision.REJECT, "no")
        eng.resolve(ProposalOutcome.REJECTED)
        s2 = eng.open_proposal("p", "draft v2", ALIVE3)
        self.assertNotEqual(s2.proposal.id, old_id)

        # 옛 proposal_id로 투표 — 늦은 투표라 None(무시), 활성 tally 불변.
        self.assertIsNone(
            eng.register_vote_for(old_id, "a", VoteDecision.APPROVE, "")
        )
        self.assertEqual(eng.active.tally.approvals, frozenset())

    def test_register_vote_for_matching_id_applies(self) -> None:
        eng = _engine()
        s = eng.open_proposal("p", "draft", ALIVE3)
        res = eng.register_vote_for(s.proposal.id, "a", VoteDecision.APPROVE, "")
        self.assertIsNotNone(res)
        _, state = res
        self.assertEqual(state.tally.approvals, frozenset({"a"}))


# ---------------------------------------------------------------------------
# 계약 오류 전파 (자기 투표 / 중복 / 비심의자)
# ---------------------------------------------------------------------------

class TestVoteContractErrors(unittest.TestCase):
    def test_proposer_self_vote_raises(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        with self.assertRaises(ContractError):
            eng.register_vote("p", VoteDecision.APPROVE, "")

    def test_duplicate_vote_raises(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        eng.register_vote("a", VoteDecision.APPROVE, "")
        with self.assertRaises(ContractError):
            eng.register_vote("a", VoteDecision.APPROVE, "")

    def test_non_voter_raises(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)  # 심의자 {a, b}
        with self.assertRaises(ContractError):
            eng.register_vote("stranger", VoteDecision.APPROVE, "")

    def test_reject_without_reason_raises(self) -> None:
        # Vote 계약이 reject 사유 필수를 강제 — ContractError 전파.
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        with self.assertRaises(ContractError):
            eng.register_vote("a", VoteDecision.REJECT, "")


# ---------------------------------------------------------------------------
# expire_pending — 정책별 기권 판정
# ---------------------------------------------------------------------------

class TestExpirePending(unittest.TestCase):
    def test_expire_without_active_returns_none(self) -> None:
        eng = _engine()
        self.assertIsNone(eng.expire_pending())

    def test_unanimous_partial_approve_then_expire_is_no_quorum(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)  # 심의자 {a, b}
        eng.register_vote("a", VoteDecision.APPROVE, "")
        state = eng.expire_pending()
        # b 기권 — unanimous는 전원 승인 실패 → NO_QUORUM.
        self.assertEqual(state.outcome, ProposalOutcome.NO_QUORUM)
        self.assertIn("b", state.tally.abstained)

    def test_participating_unanimous_same_scenario_is_approved(self) -> None:
        # 같은 시나리오(a approve, b 기권)라도 PU는 유효 투표 전원 승인 → APPROVED.
        eng = _engine(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS)
        eng.open_proposal("p", "draft", ALIVE3)
        eng.register_vote("a", VoteDecision.APPROVE, "")
        state = eng.expire_pending()
        self.assertEqual(state.outcome, ProposalOutcome.APPROVED)

    def test_expire_is_idempotent_no_pending_left(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        state = eng.expire_pending()  # 아무도 투표 안 함 → 전원 기권
        self.assertEqual(state.tally.pending, frozenset())
        self.assertEqual(state.outcome, ProposalOutcome.NO_QUORUM)


# ---------------------------------------------------------------------------
# first 모드 / 심의자 0명 — 즉시 판정
# ---------------------------------------------------------------------------

class TestImmediateOutcomes(unittest.TestCase):
    def test_first_mode_approves_immediately_on_open(self) -> None:
        eng = _engine(mode=ApprovalPolicy.FIRST)
        state = eng.open_proposal("p", "draft", ALIVE3)
        self.assertEqual(state.outcome, ProposalOutcome.APPROVED)
        resolved = eng.resolve(ProposalOutcome.APPROVED)
        self.assertEqual(resolved.status, ProposalStatus.APPROVED)

    def test_zero_voters_unanimous_is_no_quorum_immediately(self) -> None:
        # alive에 제출자만 존재 → 심의자 0명 스냅샷 → 즉시 NO_QUORUM.
        eng = _engine()
        state = eng.open_proposal("p", "draft", frozenset({"p"}))
        self.assertEqual(state.tally.voters, frozenset())
        self.assertEqual(state.outcome, ProposalOutcome.NO_QUORUM)

    def test_proposer_not_in_alive_still_works(self) -> None:
        # 제출자가 alive에 없어도 차집합 스냅샷은 안전하게 동작.
        eng = _engine()
        state = eng.open_proposal("p", "draft", frozenset({"a", "b"}))
        self.assertEqual(state.tally.voters, frozenset({"a", "b"}))


# ---------------------------------------------------------------------------
# minimum_votes (participating_unanimous 전용)
# ---------------------------------------------------------------------------

class TestMinimumVotes(unittest.TestCase):
    def test_below_minimum_votes_is_no_quorum(self) -> None:
        eng = _engine(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS, minimum_votes=2)
        eng.open_proposal("p", "draft", ALIVE4)  # 심의자 {a, b, c}
        eng.register_vote("a", VoteDecision.APPROVE, "")
        state = eng.expire_pending()  # b, c 기권 → 유효 투표 1 < 2
        self.assertEqual(state.outcome, ProposalOutcome.NO_QUORUM)

    def test_meeting_minimum_votes_is_approved(self) -> None:
        eng = _engine(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS, minimum_votes=2)
        eng.open_proposal("p", "draft", ALIVE4)  # 심의자 {a, b, c}
        eng.register_vote("a", VoteDecision.APPROVE, "")
        eng.register_vote("b", VoteDecision.APPROVE, "")
        state = eng.expire_pending()  # c 기권 → 유효 투표 2 >= 2
        self.assertEqual(state.outcome, ProposalOutcome.APPROVED)


# ---------------------------------------------------------------------------
# resolve 규칙
# ---------------------------------------------------------------------------

class TestResolve(unittest.TestCase):
    def test_resolve_pending_is_rejected(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", ALIVE3)
        eng.register_vote("a", VoteDecision.APPROVE, "")  # 아직 PENDING
        with self.assertRaises(ContractError):
            eng.resolve(ProposalOutcome.PENDING)
        # 활성 제안은 여전히 살아있다.
        self.assertIsNotNone(eng.active)

    def test_resolve_no_quorum_closes_proposal_as_rejected(self) -> None:
        eng = _engine()
        eng.open_proposal("p", "draft", frozenset({"p"}))  # 즉시 NO_QUORUM
        resolved = eng.resolve(ProposalOutcome.NO_QUORUM)
        self.assertEqual(resolved.status, ProposalStatus.REJECTED)
        self.assertIsNone(eng.active)

    def test_resolve_without_active_raises(self) -> None:
        eng = _engine()
        with self.assertRaises(ContractError):
            eng.resolve(ProposalOutcome.APPROVED)


if __name__ == "__main__":
    unittest.main()
