"""contracts.py 계약 모듈 단위 테스트.

M1 계약(메시지/에이전트/팀/투표집계/세션/이벤트)의 검증 규칙·상태 기계·화백 합의
정족수 판정을 빠짐없이 검증한다. contracts.py는 시계를 읽지 않으므로 타임스탬프는
고정 문자열을 쓴다(결정적 테스트). 외부 의존성·네트워크 없음.
"""
import unittest

from hwabaek.contracts import (
    AGENT_NAME_RE,
    BROADCAST,
    AgentSpec,
    ApprovalPolicy,
    ContractError,
    Event,
    EventType,
    FailReason,
    InvalidTransition,
    Message,
    MessageType,
    ProposalOutcome,
    Session,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    Usage,
    VoteDecision,
    VoteTally,
    make_agent_state_event,
    make_message_event,
    make_result_event,
    make_session_status_event,
    make_usage_event,
    make_vote_status_event,
)
from hwabaek.contracts import AgentState

# 테스트 전역 고정 타임스탬프 (시계 의존 금지).
TS = "2026-07-14T00:00:00Z"
TS2 = "2026-07-14T01:00:00Z"


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

class TestUsage(unittest.TestCase):
    def test_default_is_all_zero(self) -> None:
        # 기본값은 4필드 모두 0.
        u = Usage()
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens,
                          u.cache_write_tokens), (0, 0, 0, 0))

    def test_rejects_negative(self) -> None:
        # 음수는 어느 필드든 거부.
        for kwargs in (
            {"input_tokens": -1},
            {"output_tokens": -5},
            {"cache_read_tokens": -1},
            {"cache_write_tokens": -100},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    Usage(**kwargs)

    def test_rejects_non_integer(self) -> None:
        # 비정수(float/str)는 거부.
        with self.assertRaises(ContractError):
            Usage(input_tokens=1.5)
        with self.assertRaises(ContractError):
            Usage(output_tokens="10")

    def test_add_sums_fieldwise(self) -> None:
        # __add__는 필드별 합산.
        a = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4)
        b = Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40)
        s = a + b
        self.assertEqual(s, Usage(11, 22, 33, 44))

    def test_add_with_non_usage_returns_notimplemented(self) -> None:
        # Usage가 아닌 피연산자에는 NotImplemented 반환(→ 최종 TypeError).
        self.assertIs(Usage().__add__(5), NotImplemented)
        with self.assertRaises(TypeError):
            _ = Usage() + 5

    def test_total_tokens_sums_all_four_fields(self) -> None:
        # total_tokens는 캐시 읽기/쓰기 포함 전체 합.
        u = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=4, cache_write_tokens=8)
        self.assertEqual(u.total_tokens, 15)

    def test_to_from_dict_roundtrip(self) -> None:
        # to_dict/from_dict 왕복 동일성.
        u = Usage(input_tokens=7, output_tokens=11, cache_read_tokens=13, cache_write_tokens=17)
        self.assertEqual(Usage.from_dict(u.to_dict()), u)
        self.assertEqual(u.to_dict(),
                         {"input_tokens": 7, "output_tokens": 11,
                          "cache_read_tokens": 13, "cache_write_tokens": 17})


# ---------------------------------------------------------------------------
# Message — 공통 규칙
# ---------------------------------------------------------------------------

def _chat(**overrides) -> Message:
    """유효한 CHAT 메시지 팩토리 — 오버라이드로 케이스 변형."""
    base = dict(
        id="m1", session_id="s1", sender="analyst", recipients=("writer",),
        type=MessageType.CHAT, content="hello", created_at=TS,
    )
    base.update(overrides)
    return Message(**base)


class TestMessageCommon(unittest.TestCase):
    def test_valid_chat_constructs(self) -> None:
        m = _chat()
        self.assertEqual(m.type, MessageType.CHAT)
        self.assertFalse(m.is_broadcast)

    def test_rejects_empty_required_fields(self) -> None:
        # id/session_id/sender/created_at 빈 값 거부.
        for field in ("id", "session_id", "sender", "created_at"):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    _chat(**{field: ""})

    def test_rejects_empty_recipients(self) -> None:
        # recipients는 비어 있으면 거부.
        with self.assertRaises(ContractError):
            _chat(recipients=())

    def test_rejects_non_tuple_recipients(self) -> None:
        # recipients가 튜플이 아니면 거부(리스트 등).
        with self.assertRaises(ContractError):
            _chat(recipients=["writer"])

    def test_broadcast_must_be_alone(self) -> None:
        # BROADCAST는 다른 수신자와 섞어 쓸 수 없다.
        with self.assertRaises(ContractError):
            _chat(recipients=(BROADCAST, "writer"))
        with self.assertRaises(ContractError):
            _chat(recipients=("writer", BROADCAST))

    def test_broadcast_alone_ok(self) -> None:
        # 단독 BROADCAST는 허용.
        m = _chat(recipients=(BROADCAST,))
        self.assertTrue(m.is_broadcast)

    def test_sender_must_not_be_broadcast_marker(self) -> None:
        # sender는 '*'일 수 없다.
        with self.assertRaises(ContractError):
            _chat(sender=BROADCAST)


# ---------------------------------------------------------------------------
# Message — CHAT 타입 규칙
# ---------------------------------------------------------------------------

class TestMessageChat(unittest.TestCase):
    def test_content_required(self) -> None:
        # CHAT은 content 필수.
        with self.assertRaises(ContractError):
            _chat(content="")

    def test_forbids_vote(self) -> None:
        # CHAT은 vote를 실을 수 없다.
        with self.assertRaises(ContractError):
            _chat(vote=VoteDecision.APPROVE)

    def test_forbids_proposal_id(self) -> None:
        # CHAT은 proposal_id를 실을 수 없다.
        with self.assertRaises(ContractError):
            _chat(proposal_id="p1")

    def test_allows_specific_and_multiple_and_broadcast(self) -> None:
        # 특정 1인/복수/브로드캐스트 모두 허용.
        self.assertFalse(_chat(recipients=("writer",)).is_broadcast)
        self.assertFalse(_chat(recipients=("writer", "critic")).is_broadcast)
        self.assertTrue(_chat(recipients=(BROADCAST,)).is_broadcast)


# ---------------------------------------------------------------------------
# Message — RESULT_PROPOSAL 타입 규칙
# ---------------------------------------------------------------------------

def _proposal(**overrides) -> Message:
    base = dict(
        id="m2", session_id="s1", sender="writer", recipients=(BROADCAST,),
        type=MessageType.RESULT_PROPOSAL, content="draft result", created_at=TS,
    )
    base.update(overrides)
    return Message(**base)


class TestMessageResultProposal(unittest.TestCase):
    def test_valid_proposal(self) -> None:
        m = _proposal()
        self.assertTrue(m.is_broadcast)

    def test_content_required(self) -> None:
        # 초안 content 필수.
        with self.assertRaises(ContractError):
            _proposal(content="")

    def test_broadcast_enforced(self) -> None:
        # 특정 수신자 지정은 거부(전원 심의 강제).
        with self.assertRaises(ContractError):
            _proposal(recipients=("writer",))

    def test_forbids_vote_and_proposal_id(self) -> None:
        # 초안은 vote/proposal_id 금지.
        with self.assertRaises(ContractError):
            _proposal(vote=VoteDecision.APPROVE)
        with self.assertRaises(ContractError):
            _proposal(proposal_id="p1")


# ---------------------------------------------------------------------------
# Message — VOTE 타입 규칙
# ---------------------------------------------------------------------------

def _vote(**overrides) -> Message:
    base = dict(
        id="m3", session_id="s1", sender="critic", recipients=(BROADCAST,),
        type=MessageType.VOTE, content="looks good", created_at=TS,
        vote=VoteDecision.APPROVE, proposal_id="p1",
    )
    base.update(overrides)
    return Message(**base)


class TestMessageVote(unittest.TestCase):
    def test_valid_vote(self) -> None:
        m = _vote()
        self.assertEqual(m.vote, VoteDecision.APPROVE)
        self.assertEqual(m.proposal_id, "p1")

    def test_requires_vote(self) -> None:
        # vote 없으면 거부.
        with self.assertRaises(ContractError):
            _vote(vote=None)

    def test_requires_proposal_id(self) -> None:
        # proposal_id 없거나 비면 거부.
        with self.assertRaises(ContractError):
            _vote(proposal_id=None)
        with self.assertRaises(ContractError):
            _vote(proposal_id="")

    def test_broadcast_enforced(self) -> None:
        # 투표는 브로드캐스트 강제(공개 투표 — 화백).
        with self.assertRaises(ContractError):
            _vote(recipients=("writer",))

    def test_reject_vote_ok(self) -> None:
        m = _vote(vote=VoteDecision.REJECT, content="needs work")
        self.assertEqual(m.vote, VoteDecision.REJECT)

    def test_empty_content_allowed(self) -> None:
        # VOTE는 content(투표 사유)가 선택 — 빈 문자열도 허용됨(계약 현상태).
        m = _vote(content="")
        self.assertEqual(m.content, "")


# ---------------------------------------------------------------------------
# Message — 직렬화
# ---------------------------------------------------------------------------

class TestMessageSerialization(unittest.TestCase):
    def test_chat_roundtrip(self) -> None:
        m = _chat(recipients=("writer", "critic"))
        self.assertEqual(Message.from_dict(m.to_dict()), m)

    def test_proposal_roundtrip(self) -> None:
        m = _proposal()
        self.assertEqual(Message.from_dict(m.to_dict()), m)

    def test_vote_roundtrip_serializes_enum(self) -> None:
        # enum이 문자열로 직렬화되고 다시 복원되는지 확인.
        m = _vote(vote=VoteDecision.REJECT)
        d = m.to_dict()
        self.assertEqual(d["type"], "vote")
        self.assertEqual(d["vote"], "reject")
        self.assertIsInstance(d["recipients"], list)
        self.assertEqual(Message.from_dict(d), m)

    def test_to_dict_none_vote(self) -> None:
        # vote가 None인 CHAT은 dict에서도 None.
        d = _chat().to_dict()
        self.assertIsNone(d["vote"])
        self.assertEqual(d["type"], "chat")


# ---------------------------------------------------------------------------
# AgentSpec
# ---------------------------------------------------------------------------

def _agent(**overrides) -> AgentSpec:
    base = dict(name="analyst", role="analysis", system_prompt="You analyze.")
    base.update(overrides)
    return AgentSpec(**base)


class TestAgentSpec(unittest.TestCase):
    def test_valid_names(self) -> None:
        # 소문자 시작 + 소문자/숫자/밑줄/하이픈, 최대 32자.
        for name in ("a", "analyst", "agent-1", "agent_2", "x9", "a" * 32):
            with self.subTest(name=name):
                self.assertTrue(AGENT_NAME_RE.match(name))
                self.assertEqual(_agent(name=name).name, name)

    def test_rejects_uppercase(self) -> None:
        with self.assertRaises(ContractError):
            _agent(name="Analyst")

    def test_rejects_space(self) -> None:
        with self.assertRaises(ContractError):
            _agent(name="an alyst")

    def test_rejects_too_long(self) -> None:
        # 33자는 거부(최대 32).
        with self.assertRaises(ContractError):
            _agent(name="a" * 33)

    def test_rejects_leading_digit_and_empty(self) -> None:
        with self.assertRaises(ContractError):
            _agent(name="1agent")
        with self.assertRaises(ContractError):
            _agent(name="")

    def test_rejects_empty_role_and_prompt(self) -> None:
        with self.assertRaises(ContractError):
            _agent(role="")
        with self.assertRaises(ContractError):
            _agent(system_prompt="")

    def test_model_none_ok_but_empty_rejected(self) -> None:
        # model은 생략(None) 가능, 빈 문자열은 거부.
        self.assertIsNone(_agent(model=None).model)
        self.assertEqual(_agent(model="gpt-x").model, "gpt-x")
        with self.assertRaises(ContractError):
            _agent(model="")

    def test_max_turns_positive_int(self) -> None:
        self.assertEqual(_agent(max_turns=1).max_turns, 1)
        for bad in (0, -3):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    _agent(max_turns=bad)
        with self.assertRaises(ContractError):
            _agent(max_turns=1.5)


# ---------------------------------------------------------------------------
# TerminationPolicy
# ---------------------------------------------------------------------------

class TestTerminationPolicy(unittest.TestCase):
    def test_defaults(self) -> None:
        p = TerminationPolicy()
        self.assertEqual(p.approval, ApprovalPolicy.UNANIMOUS)
        self.assertGreater(p.max_messages, 0)

    def test_max_messages_positive_int(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    TerminationPolicy(max_messages=bad)

    def test_token_budget_positive_int(self) -> None:
        for bad in (0, -100):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    TerminationPolicy(token_budget=bad)

    def test_idle_timeout_positive_number(self) -> None:
        # 정수/실수 모두 허용, 0 이하는 거부.
        self.assertEqual(TerminationPolicy(idle_timeout=5).idle_timeout, 5)
        self.assertEqual(TerminationPolicy(idle_timeout=2.5).idle_timeout, 2.5)
        for bad in (0, -0.1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    TerminationPolicy(idle_timeout=bad)


# ---------------------------------------------------------------------------
# TeamConfig
# ---------------------------------------------------------------------------

def _team(**overrides) -> TeamConfig:
    base = dict(
        name="research_team",
        agents=(
            _agent(name="analyst"),
            _agent(name="writer", model="gpt-writer"),
        ),
    )
    base.update(overrides)
    return TeamConfig(**base)


class TestTeamConfig(unittest.TestCase):
    def test_valid(self) -> None:
        t = _team()
        self.assertEqual(len(t.agents), 2)

    def test_rejects_invalid_team_name(self) -> None:
        with self.assertRaises(ContractError):
            _team(name="Research Team")

    def test_rejects_empty_default_model(self) -> None:
        with self.assertRaises(ContractError):
            _team(default_model="")

    def test_rejects_no_agents(self) -> None:
        with self.assertRaises(ContractError):
            _team(agents=())

    def test_rejects_non_tuple_agents(self) -> None:
        with self.assertRaises(ContractError):
            _team(agents=[_agent()])

    def test_rejects_duplicate_agent_names(self) -> None:
        with self.assertRaises(ContractError):
            _team(agents=(_agent(name="analyst"), _agent(name="analyst")))

    def test_model_for_individual_override(self) -> None:
        # 개별 지정 모델을 그대로 반환.
        t = _team()
        self.assertEqual(t.model_for("writer"), "gpt-writer")

    def test_model_for_team_default(self) -> None:
        # 개별 지정이 없으면 팀 기본값.
        t = _team(default_model="team-default-model")
        self.assertEqual(t.model_for("analyst"), "team-default-model")

    def test_model_for_unknown_agent(self) -> None:
        with self.assertRaises(ContractError):
            _team().model_for("ghost")


# ---------------------------------------------------------------------------
# VoteTally — 구성/반영
# ---------------------------------------------------------------------------

class TestVoteTallyConstruction(unittest.TestCase):
    def test_valid(self) -> None:
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), rejections=frozenset({"b"}))
        self.assertEqual(t.pending, frozenset({"c"}))

    def test_rejects_non_voter_in_group(self) -> None:
        # 비투표자가 어느 그룹에든 들어가면 거부.
        with self.assertRaises(ContractError):
            VoteTally(voters=frozenset({"a"}), approvals=frozenset({"x"}))

    def test_rejects_agent_in_two_groups(self) -> None:
        # 한 에이전트가 두 그룹에 동시 소속이면 거부.
        with self.assertRaises(ContractError):
            VoteTally(voters=frozenset({"a"}),
                      approvals=frozenset({"a"}), rejections=frozenset({"a"}))


class TestVoteTallyWithVote(unittest.TestCase):
    def test_approve_and_reject(self) -> None:
        t = VoteTally(voters=frozenset({"a", "b"}))
        t = t.with_vote("a", VoteDecision.APPROVE)
        t = t.with_vote("b", VoteDecision.REJECT)
        self.assertEqual(t.approvals, frozenset({"a"}))
        self.assertEqual(t.rejections, frozenset({"b"}))

    def test_rejects_non_voter(self) -> None:
        t = VoteTally(voters=frozenset({"a"}))
        with self.assertRaises(ContractError):
            t.with_vote("x", VoteDecision.APPROVE)

    def test_rejects_duplicate_response(self) -> None:
        # 이미 응답(승인/반려/기권)한 에이전트의 재투표는 거부.
        t = VoteTally(voters=frozenset({"a"})).with_vote("a", VoteDecision.APPROVE)
        with self.assertRaises(ContractError):
            t.with_vote("a", VoteDecision.REJECT)
        t2 = VoteTally(voters=frozenset({"a"})).with_abstained(frozenset({"a"}))
        with self.assertRaises(ContractError):
            t2.with_vote("a", VoteDecision.APPROVE)


class TestVoteTallyWithAbstained(unittest.TestCase):
    def test_abstains_pending_voters_only(self) -> None:
        # 미응답 투표자만 기권 처리.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        t = t.with_abstained(frozenset({"b", "c"}))
        self.assertEqual(t.abstained, frozenset({"b", "c"}))
        self.assertEqual(t.pending, frozenset())

    def test_ignores_already_responded(self) -> None:
        # 이미 응답한 에이전트는 무시(기권에 추가되지 않음).
        t = VoteTally(voters=frozenset({"a", "b"}), approvals=frozenset({"a"}))
        t = t.with_abstained(frozenset({"a", "b"}))
        self.assertEqual(t.abstained, frozenset({"b"}))

    def test_ignores_non_voters(self) -> None:
        # 비투표자는 무시.
        t = VoteTally(voters=frozenset({"a"}))
        t = t.with_abstained(frozenset({"a", "x", "y"}))
        self.assertEqual(t.abstained, frozenset({"a"}))


# ---------------------------------------------------------------------------
# VoteTally.decide — 화백 합의 정족수 판정 (D-011, 가장 중요)
# ---------------------------------------------------------------------------

class TestDecideFirstAndEmpty(unittest.TestCase):
    def test_first_always_approved(self) -> None:
        # FIRST는 투표 무시하고 항상 확정 — 반대가 있어도 APPROVED.
        t = VoteTally(voters=frozenset({"a", "b"}), rejections=frozenset({"a"}))
        self.assertEqual(t.decide(ApprovalPolicy.FIRST), ProposalOutcome.APPROVED)

    def test_empty_voters_approved_all_policies(self) -> None:
        # voters 빈 집합(1인 팀) — 심의자 없으므로 어느 정책이든 즉시 확정.
        t = VoteTally(voters=frozenset())
        for policy in ApprovalPolicy:
            with self.subTest(policy=policy):
                self.assertEqual(t.decide(policy), ProposalOutcome.APPROVED)


class TestDecideUnanimous(unittest.TestCase):
    P = ApprovalPolicy.UNANIMOUS

    def test_single_reject_immediately_rejected(self) -> None:
        # 반대 1표면 미완이라도 즉시 반려.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), rejections=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_partial_approvals_pending(self) -> None:
        # 일부만 승인, 미응답 존재 → PENDING.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.PENDING)

    def test_all_approve_approved(self) -> None:
        # 전원 승인 → 확정.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_approve_plus_abstain_approved(self) -> None:
        # 승인+기권 혼합(기권 제외 전원 승인, 반대 0) → 확정.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_all_abstain_no_quorum(self) -> None:
        # 전원 기권 → 유효 투표 0 → NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      abstained=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)


class TestDecideMajority(unittest.TestCase):
    P = ApprovalPolicy.MAJORITY

    def test_majority_approve_early(self) -> None:
        # 과반 승인 → 미완이라도 조기 확정 (n=3, 승인 2).
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_half_reject_early(self) -> None:
        # 절반 이상 반대 → 조기 반려 (n=4, 반대 2).
        t = VoteTally(voters=frozenset({"a", "b", "c", "d"}),
                      rejections=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_tie_completed_rejected(self) -> None:
        # 동수(1-1) + 기권으로 전원 응답 완료 → 반려.
        t = VoteTally(voters=frozenset({"a", "b", "c", "d"}),
                      approvals=frozenset({"a"}), rejections=frozenset({"b"}),
                      abstained=frozenset({"c", "d"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_completed_approvals_beat_rejections(self) -> None:
        # 전원 응답 완료, 조기 과반은 아니지만 승인 > 반대 → 확정 (n=5, 2승인 1반대 2기권).
        t = VoteTally(voters=frozenset({"a", "b", "c", "d", "e"}),
                      approvals=frozenset({"a", "b"}), rejections=frozenset({"c"}),
                      abstained=frozenset({"d", "e"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_all_abstain_no_quorum(self) -> None:
        # 유효 투표 0(전원 기권) → NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      abstained=frozenset({"a", "b", "c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)

    def test_incomplete_pending(self) -> None:
        # 미완(과반/절반반대 모두 미달) → PENDING (n=3, 승인 1).
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.PENDING)


# ---------------------------------------------------------------------------
# Session — 생성 불변식
# ---------------------------------------------------------------------------

def _session(**overrides) -> Session:
    """기본 running 세션 팩토리."""
    base = dict(id="sess1", task="do the thing", team_name="research_team",
                created_at=TS)
    base.update(overrides)
    return Session(**base)


class TestSessionInvariants(unittest.TestCase):
    def test_default_running(self) -> None:
        s = _session()
        self.assertEqual(s.status, SessionStatus.RUNNING)
        self.assertFalse(s.is_terminal)

    def test_rejects_empty_required_fields(self) -> None:
        for field in ("id", "task", "team_name", "created_at"):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    _session(**{field: ""})

    def test_failed_requires_fail_reason(self) -> None:
        # FAILED인데 fail_reason 없으면 거부.
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.FAILED, finished_at=TS2)

    def test_fail_reason_forbidden_when_not_failed(self) -> None:
        # 비-FAILED 상태에 fail_reason 있으면 거부.
        with self.assertRaises(ContractError):
            _session(fail_reason=FailReason.BUDGET)

    def test_completed_requires_result_and_submitted_by(self) -> None:
        # COMPLETED는 result와 submitted_by 필수.
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.COMPLETED, result="r", finished_at=TS2)
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.COMPLETED, submitted_by="a", finished_at=TS2)

    def test_terminal_requires_finished_at(self) -> None:
        # 종료 상태는 finished_at 필수.
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.CANCELLED)
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.COMPLETED, result="r", submitted_by="a")

    def test_finished_at_forbidden_when_not_terminal(self) -> None:
        # 비-종료 상태에 finished_at 있으면 거부.
        with self.assertRaises(ContractError):
            _session(finished_at=TS2)

    def test_valid_terminal_states(self) -> None:
        # 유효한 종료 상태 직접 구성.
        self.assertTrue(_session(status=SessionStatus.COMPLETED, result="r",
                                 submitted_by="a", finished_at=TS2).is_terminal)
        self.assertTrue(_session(status=SessionStatus.FAILED,
                                 fail_reason=FailReason.IDLE, finished_at=TS2).is_terminal)
        self.assertTrue(_session(status=SessionStatus.CANCELLED,
                                 finished_at=TS2).is_terminal)


# ---------------------------------------------------------------------------
# Session — 상태 기계 전이
# ---------------------------------------------------------------------------

class TestSessionTransitions(unittest.TestCase):
    def test_running_to_voting(self) -> None:
        s = _session().with_status(SessionStatus.VOTING)
        self.assertEqual(s.status, SessionStatus.VOTING)
        self.assertIsNone(s.finished_at)

    def test_voting_to_running(self) -> None:
        s = _session().with_status(SessionStatus.VOTING)
        s = s.with_status(SessionStatus.RUNNING)
        self.assertEqual(s.status, SessionStatus.RUNNING)

    def test_voting_to_completed(self) -> None:
        s = _session().with_status(SessionStatus.VOTING)
        s = s.with_status(SessionStatus.COMPLETED, result="final",
                          submitted_by="writer", finished_at=TS2)
        self.assertEqual(s.status, SessionStatus.COMPLETED)
        self.assertEqual(s.result, "final")
        self.assertEqual(s.submitted_by, "writer")
        self.assertEqual(s.finished_at, TS2)

    def test_running_to_failed(self) -> None:
        s = _session().with_status(SessionStatus.FAILED,
                                   fail_reason=FailReason.BUDGET, finished_at=TS2)
        self.assertEqual(s.status, SessionStatus.FAILED)
        self.assertEqual(s.fail_reason, FailReason.BUDGET)

    def test_running_to_cancelled(self) -> None:
        s = _session().with_status(SessionStatus.CANCELLED, finished_at=TS2)
        self.assertEqual(s.status, SessionStatus.CANCELLED)

    def test_voting_to_failed_and_cancelled(self) -> None:
        v = _session().with_status(SessionStatus.VOTING)
        self.assertEqual(
            v.with_status(SessionStatus.FAILED, fail_reason=FailReason.NO_QUORUM,
                          finished_at=TS2).status, SessionStatus.FAILED)
        self.assertEqual(
            v.with_status(SessionStatus.CANCELLED, finished_at=TS2).status,
            SessionStatus.CANCELLED)

    def test_forbidden_running_to_completed(self) -> None:
        # running → completed 직행 금지(반드시 voting 경유).
        with self.assertRaises(InvalidTransition):
            _session().with_status(SessionStatus.COMPLETED, result="r",
                                   submitted_by="a", finished_at=TS2)

    def test_forbidden_running_to_running(self) -> None:
        with self.assertRaises(InvalidTransition):
            _session().with_status(SessionStatus.RUNNING)

    def test_forbidden_voting_to_voting(self) -> None:
        v = _session().with_status(SessionStatus.VOTING)
        with self.assertRaises(InvalidTransition):
            v.with_status(SessionStatus.VOTING)

    def test_no_transition_from_terminal(self) -> None:
        # 모든 종료 상태에서 어떤 전이도 불가.
        terminals = [
            _session(status=SessionStatus.COMPLETED, result="r",
                     submitted_by="a", finished_at=TS2),
            _session(status=SessionStatus.FAILED,
                     fail_reason=FailReason.IDLE, finished_at=TS2),
            _session(status=SessionStatus.CANCELLED, finished_at=TS2),
        ]
        for s in terminals:
            for target in SessionStatus:
                with self.subTest(src=s.status, target=target):
                    with self.assertRaises(InvalidTransition):
                        s.with_status(target)

    def test_invalid_transition_is_contract_error(self) -> None:
        # InvalidTransition은 ContractError의 하위 타입.
        self.assertTrue(issubclass(InvalidTransition, ContractError))

    def test_reject_resets_result_and_submitted_by(self) -> None:
        # VOTING→RUNNING 반려 시 result/submitted_by가 초기화된다.
        v = _session(status=SessionStatus.VOTING, result="draft",
                     submitted_by="writer")
        r = v.with_status(SessionStatus.RUNNING)
        self.assertIsNone(r.result)
        self.assertIsNone(r.submitted_by)
        self.assertEqual(r.status, SessionStatus.RUNNING)

    def test_with_status_returns_new_instance(self) -> None:
        # 불변 — 원본은 그대로.
        s = _session()
        s2 = s.with_status(SessionStatus.VOTING)
        self.assertEqual(s.status, SessionStatus.RUNNING)
        self.assertIsNot(s, s2)


# ---------------------------------------------------------------------------
# Session — 사용량 누적
# ---------------------------------------------------------------------------

class TestSessionUsage(unittest.TestCase):
    def test_with_usage_accumulates(self) -> None:
        s = _session()
        s = s.with_usage(Usage(input_tokens=10, output_tokens=5))
        s = s.with_usage(Usage(input_tokens=1, cache_read_tokens=4))
        self.assertEqual(s.usage, Usage(input_tokens=11, output_tokens=5,
                                        cache_read_tokens=4))

    def test_with_usage_rejected_after_terminal(self) -> None:
        # 종료 세션에는 사용량 추가 불가.
        s = _session(status=SessionStatus.CANCELLED, finished_at=TS2)
        with self.assertRaises(ContractError):
            s.with_usage(Usage(input_tokens=1))


# ---------------------------------------------------------------------------
# Session — 직렬화
# ---------------------------------------------------------------------------

class TestSessionSerialization(unittest.TestCase):
    def test_running_roundtrip(self) -> None:
        s = _session().with_usage(Usage(input_tokens=3))
        self.assertEqual(Session.from_dict(s.to_dict()), s)

    def test_completed_roundtrip(self) -> None:
        s = _session(status=SessionStatus.COMPLETED, result="final",
                     submitted_by="writer", finished_at=TS2)
        d = s.to_dict()
        self.assertEqual(d["status"], "completed")
        self.assertEqual(Session.from_dict(d), s)

    def test_failed_roundtrip_serializes_fail_reason(self) -> None:
        s = _session(status=SessionStatus.FAILED,
                     fail_reason=FailReason.NO_QUORUM, finished_at=TS2)
        d = s.to_dict()
        self.assertEqual(d["fail_reason"], "no_quorum")
        self.assertEqual(Session.from_dict(d), s)

    def test_to_dict_none_fail_reason(self) -> None:
        d = _session().to_dict()
        self.assertIsNone(d["fail_reason"])
        self.assertIsInstance(d["usage"], dict)


# ---------------------------------------------------------------------------
# Event + make_* 헬퍼
# ---------------------------------------------------------------------------

class TestEventValidation(unittest.TestCase):
    def test_valid_event(self) -> None:
        e = Event(seq=0, session_id="s1", type=EventType.USAGE, at=TS, payload={})
        self.assertEqual(e.seq, 0)

    def test_rejects_negative_seq(self) -> None:
        with self.assertRaises(ContractError):
            Event(seq=-1, session_id="s1", type=EventType.USAGE, at=TS, payload={})

    def test_rejects_non_int_seq(self) -> None:
        with self.assertRaises(ContractError):
            Event(seq=1.0, session_id="s1", type=EventType.USAGE, at=TS, payload={})

    def test_rejects_empty_session_id_and_at(self) -> None:
        with self.assertRaises(ContractError):
            Event(seq=1, session_id="", type=EventType.USAGE, at=TS, payload={})
        with self.assertRaises(ContractError):
            Event(seq=1, session_id="s1", type=EventType.USAGE, at="", payload={})

    def test_to_dict(self) -> None:
        e = Event(seq=2, session_id="s1", type=EventType.MESSAGE, at=TS,
                  payload={"k": "v"})
        self.assertEqual(e.to_dict(), {
            "seq": 2, "session_id": "s1", "type": "message", "at": TS,
            "payload": {"k": "v"},
        })


class TestMakeEventHelpers(unittest.TestCase):
    def test_session_status_event_running(self) -> None:
        s = _session()
        e = make_session_status_event(5, s, TS)
        self.assertEqual(e.type, EventType.SESSION_STATUS)
        self.assertEqual(e.seq, 5)
        self.assertEqual(e.session_id, "sess1")
        self.assertEqual(e.at, TS)
        self.assertEqual(
            e.payload, {"status": "running", "fail_reason": None, "fail_detail": None}
        )

    def test_session_status_event_failed_includes_reason(self) -> None:
        s = _session(status=SessionStatus.FAILED,
                     fail_reason=FailReason.MESSAGES, finished_at=TS2)
        e = make_session_status_event(1, s, TS2)
        self.assertEqual(
            e.payload,
            {"status": "failed", "fail_reason": "messages", "fail_detail": None},
        )

    def test_message_event(self) -> None:
        m = _chat()
        e = make_message_event(3, m)
        self.assertEqual(e.type, EventType.MESSAGE)
        self.assertEqual(e.session_id, m.session_id)
        self.assertEqual(e.at, m.created_at)
        self.assertEqual(e.payload, m.to_dict())

    def test_agent_state_event(self) -> None:
        e = make_agent_state_event(4, "s1", "analyst", AgentState.THINKING, TS)
        self.assertEqual(e.type, EventType.AGENT_STATE)
        self.assertEqual(
            e.payload, {"agent": "analyst", "state": "thinking", "detail": None}
        )

    def test_usage_event(self) -> None:
        u = Usage(input_tokens=100, output_tokens=50)
        e = make_usage_event(6, "s1", u, token_budget=200000, at=TS)
        self.assertEqual(e.type, EventType.USAGE)
        self.assertEqual(
            e.payload,
            {"usage": u.to_dict(), "token_budget": 200000, "per_agent": {}},
        )

    def test_vote_status_event_sorts_members(self) -> None:
        t = VoteTally(
            voters=frozenset({"a", "b", "c", "d"}),
            approvals=frozenset({"c", "a"}),
            rejections=frozenset({"b"}),
        )
        e = make_vote_status_event(7, "s1", "p1", t, TS)
        self.assertEqual(e.type, EventType.VOTE_STATUS)
        self.assertEqual(e.payload, {
            "proposal_id": "p1",
            "approvals": ["a", "c"],
            "rejections": ["b"],
            "abstained": [],
            "pending": ["d"],
        })

    def test_result_event_requires_completed(self) -> None:
        # completed가 아닌 세션은 결과 이벤트 생성 거부.
        with self.assertRaises(ContractError):
            make_result_event(8, _session(), TS)
        with self.assertRaises(ContractError):
            make_result_event(8, _session().with_status(SessionStatus.VOTING), TS)

    def test_result_event_payload(self) -> None:
        s = _session(status=SessionStatus.COMPLETED, result="final answer",
                     submitted_by="writer", finished_at=TS2)
        e = make_result_event(9, s, TS2)
        self.assertEqual(e.type, EventType.RESULT)
        self.assertEqual(e.payload,
                         {"result": "final answer", "submitted_by": "writer"})

    def test_agent_state_event_with_detail(self) -> None:
        # DEAD 전이는 detail에 귀책 포함 사유를 싣는다 (오류 귀책 원칙).
        e = make_agent_state_event(
            10, "s1", "researcher", AgentState.DEAD, TS,
            detail="dead: repeated provider errors (rate_limit)",
        )
        self.assertEqual(e.payload, {
            "agent": "researcher",
            "state": "dead",
            "detail": "dead: repeated provider errors (rate_limit)",
        })

    def test_usage_event_with_per_agent(self) -> None:
        # per_agent는 에이전트별 누적치 전체 맵 — 이름 오름차순으로 직렬화.
        session_total = Usage(input_tokens=100, output_tokens=50)
        per_agent = {
            "writer": Usage(input_tokens=60, output_tokens=30),
            "analyst": Usage(input_tokens=40, output_tokens=20),
        }
        e = make_usage_event(11, "s1", session_total, token_budget=200000, at=TS,
                             per_agent=per_agent)
        self.assertEqual(list(e.payload["per_agent"]), ["analyst", "writer"])
        self.assertEqual(e.payload["per_agent"]["writer"],
                         {"input_tokens": 60, "output_tokens": 30,
                          "cache_read_tokens": 0, "cache_write_tokens": 0})


class TestSessionFailDetail(unittest.TestCase):
    # fail_detail은 실패 상세(귀책 포함) — FAILED에서만 허용 (Plan 코어 의미론 §3).
    def test_failed_session_carries_detail_and_roundtrips(self) -> None:
        s = _session(
            status=SessionStatus.FAILED,
            fail_reason=FailReason.AGENT_ERROR,
            fail_detail="agent researcher dead: provider rate limited",
            finished_at=TS2,
        )
        self.assertEqual(Session.from_dict(s.to_dict()), s)
        self.assertEqual(s.to_dict()["fail_detail"],
                         "agent researcher dead: provider rate limited")

    def test_non_failed_rejects_detail(self) -> None:
        with self.assertRaises(ContractError):
            _session(fail_detail="something broke")

    def test_with_status_carries_detail(self) -> None:
        s = _session().with_status(
            SessionStatus.FAILED,
            fail_reason=FailReason.AGENT_ERROR,
            fail_detail="agent writer dead: bad request (client bug)",
            finished_at=TS2,
        )
        self.assertEqual(s.fail_detail, "agent writer dead: bad request (client bug)")

    def test_session_status_event_includes_detail(self) -> None:
        s = _session(
            status=SessionStatus.FAILED,
            fail_reason=FailReason.AGENT_ERROR,
            fail_detail="agent writer dead: provider outage",
            finished_at=TS2,
        )
        e = make_session_status_event(12, s, TS2)
        self.assertEqual(e.payload["fail_detail"], "agent writer dead: provider outage")


class TestBoolRejection(unittest.TestCase):
    # bool은 int의 서브클래스지만 계약에서는 타입 오류로 거부한다.
    def test_usage_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            Usage(input_tokens=True)

    def test_event_seq_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            Event(seq=True, session_id="s1", type=EventType.USAGE, at=TS, payload={})

    def test_agent_spec_rejects_bool_max_turns(self) -> None:
        with self.assertRaises(ContractError):
            _agent(max_turns=True)

    def test_termination_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            TerminationPolicy(max_messages=True)
        with self.assertRaises(ContractError):
            TerminationPolicy(idle_timeout=True)


if __name__ == "__main__":
    unittest.main()
