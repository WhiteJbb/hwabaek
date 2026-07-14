"""ConsensusEngine — 제안·투표·정족수 판정 (Plan 코어 의미론 §5, D-016/018/020/021).

이 파일은 M2a 인터페이스 확정본이다. 구현 규칙:

- **판정만 반환한다** — 세션 상태 전환은 SessionManager의 책임 (D-021).
  이 모듈은 세션·버스·LLM을 알지 못하고 contracts 타입만 다룬다.
- **세션당 활성(pending) 제안 최대 1개** — 활성 제안이 있는 동안 open_proposal은
  도메인 오류. version은 1부터 단조 증가하며, 반려된 제안은 다음 제안 생성 시
  SUPERSEDED로 전환된다.
- **투표 검증의 단일 지점** — contracts.validate_vote(제안 수준) +
  VoteTally.with_vote(심의자 자격·중복)를 사용하고 검증 로직을 중복하지 않는다.
  이전 제안에 대한 늦은 투표는 ContractError가 아니라 **무시(None 반환)**로
  처리한다 — 에이전트 도구 오류로 되돌릴 가치가 없는 정상 경합이다.
- **심의자 스냅샷 불변** (D-018): open_proposal 시점의 생존 에이전트(제출자
  제외)로 확정하고 이후 변경하지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hwabaek.contracts import (
    ApprovalConfig,
    ContractError,
    ProposalOutcome,
    ProposalStatus,
    ResultProposal,
    Vote,
    VoteDecision,
    VoteTally,
    validate_vote,
)


class ConsensusError(ContractError):
    """합의 규칙 위반 — voting 중 중복 제안 등. 도구 오류로 에이전트에게 반환된다."""


@dataclass(frozen=True)
class ConsensusState:
    """현재 심의 상태 스냅샷 — SessionManager가 이벤트 발행에 사용."""

    proposal: ResultProposal
    tally: VoteTally
    outcome: ProposalOutcome


class ConsensusEngine:
    """세션 1개의 합의 상태를 관리한다. 모든 메서드는 동기(순수 상태 조작)다."""

    def __init__(
        self,
        session_id: str,
        approval: ApprovalConfig,
        *,
        clock: Callable[[], str],
        id_factory: Callable[[], str],
    ) -> None:
        self._session_id = session_id
        self._approval = approval
        self._clock = clock
        self._id_factory = id_factory
        # 활성(pending) 제안 상태 스냅샷 — 없으면 None.
        self._state: ConsensusState | None = None
        # 직전 반려 제안 — 다음 open_proposal에서 SUPERSEDED로 전환할 대기 슬롯.
        self._pending_supersede: ResultProposal | None = None
        # 직전 open_proposal이 SUPERSEDED로 전환한 제안 — 상위 계층의 영속화용.
        self._last_superseded: ResultProposal | None = None
        # 제안 version 카운터 — 1부터 단조 증가(반려 후 재제출이 +1).
        self._version = 0

    @property
    def active(self) -> ConsensusState | None:
        """활성(pending) 제안의 현재 상태. 없으면 None."""
        return self._state

    @property
    def last_superseded(self) -> ResultProposal | None:
        """직전 open_proposal이 SUPERSEDED로 전환한 반려 제안. 없으면 None.

        인터페이스 확장(시그니처 변경 아님) — SessionManager가 대체된 이전
        제안의 상태 변화를 영속화하는 데 사용한다.
        """
        return self._last_superseded

    def open_proposal(
        self, proposer: str, content: str, alive_agents: frozenset[str]
    ) -> ConsensusState:
        """새 제안을 연다 — running 상태에서 submit_result 처리.

        - 활성 제안이 있으면 ConsensusError (voting 중 중복 submit 거부).
        - 직전 반려 제안이 있으면 SUPERSEDED로 전환하고 version을 잇는다.
        - 심의자 스냅샷 = alive_agents - {proposer} (D-018).
        - 반환된 outcome이 즉시 APPROVED일 수 있다 (first 모드).
        """
        if self._state is not None:
            raise ConsensusError("cannot submit while a proposal is under vote")
        # 직전 반려 제안이 있으면 SUPERSEDED로 전환하고 version을 잇는다 (D-020).
        self._last_superseded = None
        if self._pending_supersede is not None:
            self._last_superseded = self._pending_supersede.with_status(
                ProposalStatus.SUPERSEDED
            )
            self._pending_supersede = None
        self._version += 1
        proposal = ResultProposal(
            id=self._id_factory(),
            session_id=self._session_id,
            proposer=proposer,
            version=self._version,
            content=content,
            created_at=self._clock(),
        )
        # 심의자 스냅샷 = 생존 에이전트 - 제출자 (D-018 불변 스냅샷).
        # 제출자가 alive에 없어도 차집합은 안전하게 동작한다.
        voters = frozenset(alive_agents) - {proposer}
        tally = VoteTally(voters=voters)
        outcome = tally.decide(self._approval)
        self._state = ConsensusState(proposal=proposal, tally=tally, outcome=outcome)
        return self._state

    def register_vote(
        self, voter: str, decision: VoteDecision, reason: str
    ) -> tuple[Vote, ConsensusState] | None:
        """투표를 등록하고 갱신된 판정을 반환한다.

        - 활성 제안이 없거나 늦은 투표(활성 제안 불일치)는 **None 반환(무시)**.
        - 자기 투표·비심의자·중복 투표는 ContractError — 에이전트 도구 오류로 반환.
        - Vote 레코드(id/created_at 부여)를 함께 반환한다 — 영속화·이벤트용.
        """
        if self._state is None:
            return None
        return self._apply_vote(voter, decision, reason)

    def register_vote_for(
        self, proposal_id: str, voter: str, decision: VoteDecision, reason: str
    ) -> tuple[Vote, ConsensusState] | None:
        """proposal_id를 지정한 투표 — 활성 제안과 대조 후 반영한다(인터페이스 확장).

        에이전트가 옛 proposal_id로 투표하는 경합을 상위 계층(SessionManager/agent)이
        명시적으로 무시하도록 제공한다. 활성 제안이 없거나 id가 불일치하면
        None(늦은 투표 무시). 일치하면 register_vote와 동일하게 검증·집계한다.
        """
        if self._state is None:
            return None
        if proposal_id != self._state.proposal.id:
            return None
        return self._apply_vote(voter, decision, reason)

    def _apply_vote(
        self, voter: str, decision: VoteDecision, reason: str
    ) -> tuple[Vote, ConsensusState]:
        """활성 제안에 투표 1건을 반영한다 — 검증 실패는 ContractError로 전파.

        순서: Vote 레코드 생성(reject 사유 필수를 Vote 계약이 검증) →
        validate_vote(제안 수준) → VoteTally.with_vote(자격·중복) → decide.
        """
        assert self._state is not None
        proposal = self._state.proposal
        vote = Vote(
            id=self._id_factory(),
            session_id=self._session_id,
            proposal_id=proposal.id,
            voter=voter,
            decision=decision,
            created_at=self._clock(),
            reason=reason,
        )
        validate_vote(vote, proposal)
        tally = self._state.tally.with_vote(voter, decision)
        outcome = tally.decide(self._approval)
        self._state = ConsensusState(proposal=proposal, tally=tally, outcome=outcome)
        return vote, self._state

    def expire_pending(self) -> ConsensusState | None:
        """voting_timeout 만료 처리 — 미응답 전원을 기권 처리하고 판정을 반환한다.

        활성 제안이 없으면 None. 반환된 outcome은 PENDING일 수 없다
        (기권 처리 후에는 모든 정책이 확정 판정을 낸다).
        """
        if self._state is None:
            return None
        tally = self._state.tally.with_abstained(self._state.tally.pending)
        outcome = tally.decide(self._approval)
        # 기권 처리 후에는 PENDING이 남을 수 없다 — 남으면 구현 버그.
        assert outcome is not ProposalOutcome.PENDING, (
            "expire_pending must yield a terminal outcome"
        )
        self._state = ConsensusState(
            proposal=self._state.proposal, tally=tally, outcome=outcome
        )
        return self._state

    def resolve(self, outcome: ProposalOutcome) -> ResultProposal:
        """판정 확정을 제안 상태에 반영한다 — APPROVED/REJECTED만 허용.

        SessionManager가 상태 전환 직전에 호출한다. 반환값은 갱신된 제안
        (영속화용). NO_QUORUM은 제안을 REJECTED로 마감한다(세션은 실패).
        """
        if self._state is None:
            raise ContractError("no active proposal to resolve")
        if outcome is ProposalOutcome.PENDING:
            raise ContractError("cannot resolve a pending outcome")
        proposal = self._state.proposal
        if outcome is ProposalOutcome.APPROVED:
            resolved = proposal.with_status(ProposalStatus.APPROVED)
            self._state = None
            return resolved
        # REJECTED / NO_QUORUM 모두 제안을 REJECTED로 마감하고 supersede 대기로 이동.
        resolved = proposal.with_status(ProposalStatus.REJECTED)
        self._state = None
        self._pending_supersede = resolved
        return resolved
