"""contracts.py 계약 모듈 단위 테스트.

M1 계약(메시지/에이전트/팀/투표집계/세션/이벤트)의 검증 규칙·상태 기계·화백 합의
정족수 판정을 빠짐없이 검증한다. contracts.py는 시계를 읽지 않으므로 타임스탬프는
고정 문자열을 쓴다(결정적 테스트). 외부 의존성·네트워크 없음.
"""
import unittest

from hwabaek.contracts import (
    AGENT_NAME_RE,
    BROADCAST,
    COMMAND_SEND_MESSAGE,
    COMMAND_SUBMIT_RESULT,
    COMMAND_VOTE_RESULT,
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    ContractError,
    Event,
    EventType,
    FailReason,
    InvalidTransition,
    Message,
    MessageType,
    ProposalOutcome,
    ProposalStatus,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    Usage,
    Vote,
    VoteDecision,
    VoteTally,
    allowed_commands,
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

    def test_work_and_processed_token_totals(self) -> None:
        # 작업량은 캐시 읽기를 제외하고, 처리량과 구형 별칭은 이를 포함한다.
        u = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=4, cache_write_tokens=8)
        self.assertEqual(u.work_tokens, 11)
        self.assertEqual(u.processed_tokens, 15)
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
        type=MessageType.CHAT, content="hello", created_at=TS, sequence=0,
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

    def test_sequence_accepts_non_negative_int(self) -> None:
        # sequence는 0 이상 정수 — 세션 단위 단조 증가(D-023).
        self.assertEqual(_chat(sequence=0).sequence, 0)
        self.assertEqual(_chat(sequence=42).sequence, 42)

    def test_sequence_rejects_negative(self) -> None:
        with self.assertRaises(ContractError):
            _chat(sequence=-1)

    def test_sequence_rejects_bool(self) -> None:
        # bool은 int 서브클래스지만 sequence로는 거부.
        with self.assertRaises(ContractError):
            _chat(sequence=True)

    def test_sequence_rejects_non_int(self) -> None:
        with self.assertRaises(ContractError):
            _chat(sequence=1.5)

    def test_rejects_self_addressing(self) -> None:
        # 자기 자신을 수신자로 지정할 수 없다(직접 지정도, 복수 수신자 안에서도).
        with self.assertRaises(ContractError):
            _chat(sender="analyst", recipients=("analyst",))
        with self.assertRaises(ContractError):
            _chat(sender="analyst", recipients=("writer", "analyst"))


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
    # RESULT_PROPOSAL은 자신이 실어 나르는 ResultProposal.id를 proposal_id로 가리킨다 (D-016).
    base = dict(
        id="m2", session_id="s1", sender="writer", recipients=(BROADCAST,),
        type=MessageType.RESULT_PROPOSAL, content="draft result", created_at=TS,
        sequence=1, proposal_id="p1",
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

    def test_forbids_vote(self) -> None:
        # 초안은 여전히 vote 금지(제출은 투표가 아님).
        with self.assertRaises(ContractError):
            _proposal(vote=VoteDecision.APPROVE)

    def test_requires_proposal_id(self) -> None:
        # D-016: 초안은 이제 proposal_id 필수(자기 ResultProposal.id) — None/빈 문자열 거부.
        with self.assertRaises(ContractError):
            _proposal(proposal_id=None)
        with self.assertRaises(ContractError):
            _proposal(proposal_id="")

    def test_carries_proposal_id(self) -> None:
        # 유효한 proposal_id는 그대로 보존된다.
        self.assertEqual(_proposal(proposal_id="p7").proposal_id, "p7")


# ---------------------------------------------------------------------------
# Message — VOTE 타입 규칙
# ---------------------------------------------------------------------------

def _vote(**overrides) -> Message:
    base = dict(
        id="m3", session_id="s1", sender="critic", recipients=(BROADCAST,),
        type=MessageType.VOTE, content="looks good", created_at=TS,
        sequence=2, vote=VoteDecision.APPROVE, proposal_id="p1",
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

    def test_dict_includes_sequence(self) -> None:
        # sequence는 직렬화에 포함되고 왕복에서 보존된다.
        m = _chat(sequence=7)
        d = m.to_dict()
        self.assertEqual(d["sequence"], 7)
        self.assertEqual(Message.from_dict(d), m)


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
        # approval은 이제 ApprovalConfig — 기본 mode는 UNANIMOUS.
        self.assertIsInstance(p.approval, ApprovalConfig)
        self.assertEqual(p.approval.mode, ApprovalPolicy.UNANIMOUS)
        self.assertGreater(p.max_messages, 0)

    def test_budget_control_defaults_are_derived_from_work_budget(self) -> None:
        p = TerminationPolicy(token_budget=60_000)
        self.assertIsNone(p.processed_token_limit)
        self.assertIsNone(p.synthesis_at)
        self.assertIsNone(p.proposal_by)
        self.assertIsNone(p.call_reserve_tokens)
        self.assertIsNone(p.max_proposals)
        self.assertEqual(p.effective_processed_token_limit, 150_000)
        self.assertEqual(p.effective_synthesis_at, 25_000)
        self.assertEqual(p.effective_proposal_by, 40_000)
        self.assertEqual(p.effective_call_reserve_tokens, 6_000)
        self.assertEqual(p.effective_max_proposals, 2)

    def test_explicit_budget_controls_override_derived_values(self) -> None:
        p = TerminationPolicy(
            token_budget=100,
            processed_token_limit=300,
            synthesis_at=20,
            proposal_by=70,
            call_reserve_tokens=5,
            max_proposals=3,
        )
        self.assertEqual(p.effective_processed_token_limit, 300)
        self.assertEqual(p.effective_synthesis_at, 20)
        self.assertEqual(p.effective_proposal_by, 70)
        self.assertEqual(p.effective_call_reserve_tokens, 5)
        self.assertEqual(p.effective_max_proposals, 3)

    def test_optional_budget_controls_are_positive_int_or_null(self) -> None:
        fields = (
            "processed_token_limit", "synthesis_at", "proposal_by",
            "call_reserve_tokens", "max_proposals",
        )
        for field_name in fields:
            for bad in (0, -1, True, 1.5, "1"):
                with self.subTest(field=field_name, bad=bad):
                    with self.assertRaises(ContractError):
                        TerminationPolicy(**{field_name: bad})

    def test_budget_control_cross_field_validation(self) -> None:
        invalid = (
            {"token_budget": 100, "processed_token_limit": 99},
            {"token_budget": 100, "synthesis_at": 70, "proposal_by": 70},
            {"token_budget": 100, "synthesis_at": 80, "proposal_by": 70},
            {"token_budget": 100, "synthesis_at": 100},
            {"token_budget": 100, "proposal_by": 100},
            {"token_budget": 100, "call_reserve_tokens": 100},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    TerminationPolicy(**kwargs)

    def test_tiny_valid_budget_derived_thresholds_remain_ordered(self) -> None:
        p = TerminationPolicy(token_budget=3)
        self.assertEqual(p.effective_synthesis_at, 1)
        self.assertEqual(p.effective_proposal_by, 2)
        self.assertEqual(p.effective_call_reserve_tokens, 1)

    def test_max_messages_positive_int(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    TerminationPolicy(max_messages=bad)

    def test_token_budget_positive_int(self) -> None:
        for bad in (0, -100, 1, 2):
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
# ApprovalConfig — 합의 승인 설정 (D-016, D-019)
# ---------------------------------------------------------------------------

class TestApprovalConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        # 기본: mode UNANIMOUS, voting_timeout 양수, minimum_votes None.
        c = ApprovalConfig()
        self.assertEqual(c.mode, ApprovalPolicy.UNANIMOUS)
        self.assertGreater(c.voting_timeout, 0)
        self.assertIsNone(c.minimum_votes)

    def test_voting_timeout_accepts_int_and_float(self) -> None:
        self.assertEqual(ApprovalConfig(voting_timeout=5).voting_timeout, 5)
        self.assertEqual(ApprovalConfig(voting_timeout=2.5).voting_timeout, 2.5)

    def test_rejects_non_positive_voting_timeout(self) -> None:
        for bad in (0, -0.1, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    ApprovalConfig(voting_timeout=bad)

    def test_rejects_bool_voting_timeout(self) -> None:
        # bool은 int 서브클래스지만 timeout으로는 거부.
        with self.assertRaises(ContractError):
            ApprovalConfig(voting_timeout=True)

    def test_minimum_votes_allowed_only_with_participating_unanimous(self) -> None:
        # minimum_votes는 participating_unanimous에서만 허용 — 그 외 모드는 거부.
        for mode in (ApprovalPolicy.UNANIMOUS, ApprovalPolicy.MAJORITY,
                     ApprovalPolicy.FIRST):
            with self.subTest(mode=mode):
                with self.assertRaises(ContractError):
                    ApprovalConfig(mode=mode, minimum_votes=1)

    def test_minimum_votes_with_participating_unanimous_ok(self) -> None:
        c = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                           minimum_votes=2)
        self.assertEqual(c.minimum_votes, 2)

    def test_minimum_votes_none_allowed(self) -> None:
        # None(미지정)은 어떤 모드에서도 허용.
        c = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                           minimum_votes=None)
        self.assertIsNone(c.minimum_votes)

    def test_rejects_non_positive_minimum_votes(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                                   minimum_votes=bad)

    def test_rejects_bool_minimum_votes(self) -> None:
        # bool은 int 서브클래스지만 minimum_votes로는 거부.
        with self.assertRaises(ContractError):
            ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                           minimum_votes=True)


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

    def test_single_agent_first_allowed(self) -> None:
        # D-018: 1인 팀 + first 모드는 허용(투표 없이 첫 제출 즉시 확정).
        cfg = TerminationPolicy(approval=ApprovalConfig(mode=ApprovalPolicy.FIRST))
        t = _team(agents=(_agent(name="solo"),), termination=cfg)
        self.assertEqual(len(t.agents), 1)

    def test_single_agent_non_first_rejected(self) -> None:
        # D-018: 투표가 있는 모드는 심의자가 필요하므로 1인 팀을 사전 거부한다
        # (제출자는 자기 제안에 투표할 수 없음).
        for mode in (ApprovalPolicy.UNANIMOUS, ApprovalPolicy.MAJORITY,
                     ApprovalPolicy.PARTICIPATING_UNANIMOUS):
            with self.subTest(mode=mode):
                cfg = TerminationPolicy(approval=ApprovalConfig(mode=mode))
                with self.assertRaises(ContractError):
                    _team(agents=(_agent(name="solo"),), termination=cfg)


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


class TestVoterDeathAbstains(unittest.TestCase):
    # D-018: 투표 대상(voters)은 voting 시작 시점의 스냅샷으로 고정되며 심의 중
    # 변경되지 않는다(with_voter_removed 삭제). 심의 중 사망한 에이전트는
    # voting_timeout 만료 시 기권 처리되고(with_abstained 경로), 기권은 어떤
    # 정책에서도 승인이 아니므로 unanimous에서는 no_quorum으로 이어진다.
    UNANIMOUS = ApprovalConfig(mode=ApprovalPolicy.UNANIMOUS)

    def test_dead_voter_abstained_yields_no_quorum(self) -> None:
        # a,b는 승인, c는 심의 중 사망 → 만료 시 c를 기권 처리 → 전원 승인 실패로 NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.UNANIMOUS), ProposalOutcome.PENDING)
        t = t.with_abstained(frozenset({"c"}))  # 사망자 만료 기권 처리
        self.assertEqual(t.abstained, frozenset({"c"}))
        self.assertEqual(t.decide(self.UNANIMOUS), ProposalOutcome.NO_QUORUM)

    def test_snapshot_voters_unchanged_after_abstain(self) -> None:
        # 기권 처리해도 voters 스냅샷은 그대로 — 대상 집합 불변, 새 인스턴스 반환.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}))
        t2 = t.with_abstained(frozenset({"c"}))
        self.assertEqual(t2.voters, frozenset({"a", "b", "c"}))
        self.assertEqual(t.abstained, frozenset())
        self.assertIsNot(t, t2)


# ---------------------------------------------------------------------------
# VoteTally.decide — 화백 합의 정족수 판정 (D-016, 가장 중요)
# ---------------------------------------------------------------------------

class TestDecideFirstAndEmpty(unittest.TestCase):
    def test_first_always_approved(self) -> None:
        # FIRST는 투표 무시하고 항상 확정 — 반대가 있어도 APPROVED.
        t = VoteTally(voters=frozenset({"a", "b"}), rejections=frozenset({"a"}))
        self.assertEqual(t.decide(ApprovalConfig(mode=ApprovalPolicy.FIRST)),
                         ProposalOutcome.APPROVED)

    def test_empty_voters_first_approved(self) -> None:
        # voters 빈 집합 + FIRST — 심의 생략, 즉시 확정.
        t = VoteTally(voters=frozenset())
        self.assertEqual(t.decide(ApprovalConfig(mode=ApprovalPolicy.FIRST)),
                         ProposalOutcome.APPROVED)

    def test_empty_voters_non_first_no_quorum(self) -> None:
        # D-018 반전: voters 빈 집합에서 FIRST가 아니면 확정 불가 → NO_QUORUM
        # (구 계약은 즉시 APPROVED였다). 팀 검증이 사전 차단하지만 계약도 방어한다.
        t = VoteTally(voters=frozenset())
        for mode in (ApprovalPolicy.UNANIMOUS, ApprovalPolicy.MAJORITY,
                     ApprovalPolicy.PARTICIPATING_UNANIMOUS):
            with self.subTest(mode=mode):
                self.assertEqual(t.decide(ApprovalConfig(mode=mode)),
                                 ProposalOutcome.NO_QUORUM)


class TestDecideUnanimous(unittest.TestCase):
    # D-016 UNANIMOUS: 생존 심의자 '전원' approve여야 확정. 반대 1표 즉시 반려.
    # 기권은 승인이 아니므로 전원 응답이라도 기권이 있으면 NO_QUORUM.
    P = ApprovalConfig(mode=ApprovalPolicy.UNANIMOUS)

    def test_single_reject_immediately_rejected(self) -> None:
        # 반대 1표면 미완이라도 즉시 반려.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), rejections=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_partial_approvals_pending(self) -> None:
        # 일부만 승인, 미응답 존재 → 아직 미완 → PENDING.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.PENDING)

    def test_all_approve_approved(self) -> None:
        # 생존 심의자 전원 승인 → 확정.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_approve_plus_abstain_is_not_approved(self) -> None:
        # D-016 핵심: 승인+기권 혼합은 APPROVED가 '아니다'. 전원 응답이지만
        # 기권이 남아 전원 승인 실패 → NO_QUORUM (구 unanimous와 달라진 지점).
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        outcome = t.decide(self.P)
        self.assertNotEqual(outcome, ProposalOutcome.APPROVED)
        self.assertEqual(outcome, ProposalOutcome.NO_QUORUM)

    def test_all_responded_with_abstain_no_quorum(self) -> None:
        # 반대 0, 승인 다수지만 기권 1명으로 전원 응답 → 기권 존재로 NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}), abstained=frozenset({"c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)

    def test_all_abstain_no_quorum(self) -> None:
        # 전원 기권 → 유효 투표 0 → NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      abstained=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)


class TestDecideParticipatingUnanimous(unittest.TestCase):
    # D-016 PARTICIPATING_UNANIMOUS: 유효 투표(approve/reject)를 한 전원이 approve면 확정
    # (기권 제외 판정 — 구 unanimous 규칙). 반대 1표 즉시 반려, 유효 투표 0이면 NO_QUORUM.
    P = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS)

    def test_single_reject_immediately_rejected(self) -> None:
        # 반대 1표면 즉시 반려.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), rejections=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_partial_approvals_pending(self) -> None:
        # 미응답 존재 → PENDING.
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.PENDING)

    def test_all_approve_approved(self) -> None:
        # 유효 투표 전원 승인 → 확정.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_approve_plus_abstain_approved(self) -> None:
        # 승인+기권 혼합: 기권을 제외한 유효 투표 전원이 승인 → 확정.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_all_abstain_no_quorum(self) -> None:
        # 유효 투표 0(전원 기권) → NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b"}),
                      abstained=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)


class TestDecideParticipatingUnanimousMinimumVotes(unittest.TestCase):
    # D-016/D-019 minimum_votes: participating_unanimous에서 유효 승인 수가
    # minimum_votes(기본 1) 이상이어야 APPROVED, 미달이면 NO_QUORUM.
    def test_default_minimum_is_one(self) -> None:
        # minimum_votes 미지정 → 기본 1 — 유효 승인 1(나머지 기권)이면 충족.
        cfg = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS)
        t = VoteTally(voters=frozenset({"a", "b"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b"}))
        self.assertEqual(t.decide(cfg), ProposalOutcome.APPROVED)

    def test_meets_minimum_votes_approved(self) -> None:
        # minimum_votes=2, 유효 승인 2 → 충족 → APPROVED.
        cfg = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                             minimum_votes=2)
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}), abstained=frozenset({"c"}))
        self.assertEqual(t.decide(cfg), ProposalOutcome.APPROVED)

    def test_below_minimum_votes_no_quorum(self) -> None:
        # minimum_votes=2, 유효 승인 1(전원 응답, 나머지 기권) → 미달 → NO_QUORUM.
        cfg = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                             minimum_votes=2)
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        self.assertEqual(t.decide(cfg), ProposalOutcome.NO_QUORUM)

    def test_pending_until_all_responded(self) -> None:
        # minimum_votes 충족 여부는 전원 응답 후 판정 — 미응답 존재 시 PENDING.
        cfg = ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS,
                             minimum_votes=1)
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}))
        self.assertEqual(t.decide(cfg), ProposalOutcome.PENDING)


class TestDecideUnanimousVsParticipating(unittest.TestCase):
    # 동일한 tally(승인+기권 혼합, 반대 0, 전원 응답)에 두 정책을 적용해 차이를 대비한다.
    # UNANIMOUS는 기권을 승인 실패로 보아 NO_QUORUM, PARTICIPATING_UNANIMOUS는
    # 기권을 제외한 유효 투표 전원 승인이므로 APPROVED.
    def test_same_tally_diverges(self) -> None:
        tally = VoteTally(voters=frozenset({"a", "b", "c"}),
                          approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        self.assertEqual(tally.decide(ApprovalConfig(mode=ApprovalPolicy.UNANIMOUS)),
                         ProposalOutcome.NO_QUORUM)
        self.assertEqual(
            tally.decide(ApprovalConfig(mode=ApprovalPolicy.PARTICIPATING_UNANIMOUS)),
            ProposalOutcome.APPROVED)


class TestDecideMajority(unittest.TestCase):
    # D-016 MAJORITY: 생존 심의자 '전체'의 과반 approve로 확정(유효 투표 과반이 아님).
    # 남은 미응답을 전부 approve로 가정해도 과반 불가면 조기 종료 —
    # 반대표가 있으면 REJECTED, 기권만으로 불가하면 NO_QUORUM.
    P = ApprovalConfig(mode=ApprovalPolicy.MAJORITY)

    def test_whole_majority_approve_early(self) -> None:
        # 전체 과반 승인 → 미완이라도 조기 확정 (n=3, 승인 2).
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.APPROVED)

    def test_majority_reject_early(self) -> None:
        # 반대가 과반 → 남은 미응답이 모두 승인해도 과반 불가 → 조기 반려 (n=3, 반대 2).
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      rejections=frozenset({"a", "b"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_tie_completed_rejected(self) -> None:
        # 동수(전원 응답, 2-2) → 승인이 전체 과반에 미달 + 반대 존재 → 반려.
        t = VoteTally(voters=frozenset({"a", "b", "c", "d"}),
                      approvals=frozenset({"a", "b"}), rejections=frozenset({"c", "d"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_plurality_without_whole_majority_rejected(self) -> None:
        # D-016 핵심: 승인 > 반대여도 '전체' 과반에 못 미치면 확정되지 않는다.
        # 반대표가 있으므로 REJECTED (n=5, 2승인 1반대 2기권 — 승인 2는 과반 3 미달).
        t = VoteTally(voters=frozenset({"a", "b", "c", "d", "e"}),
                      approvals=frozenset({"a", "b"}), rejections=frozenset({"c"}),
                      abstained=frozenset({"d", "e"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.REJECTED)

    def test_abstentions_block_majority_no_quorum(self) -> None:
        # 반대는 없지만 기권으로 전체 과반이 불가능해짐 → NO_QUORUM
        # (n=3, 승인 1 + 기권 2 — 승인 1은 과반 2 미달, 반대 없음).
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      approvals=frozenset({"a"}), abstained=frozenset({"b", "c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)

    def test_all_abstain_no_quorum(self) -> None:
        # 유효 투표 0(전원 기권) → NO_QUORUM.
        t = VoteTally(voters=frozenset({"a", "b", "c"}),
                      abstained=frozenset({"a", "b", "c"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.NO_QUORUM)

    def test_incomplete_pending(self) -> None:
        # 아직 과반도 반려도 불가능(미응답이 승인하면 과반 가능) → PENDING (n=3, 승인 1 미응답 2).
        t = VoteTally(voters=frozenset({"a", "b", "c"}), approvals=frozenset({"a"}))
        self.assertEqual(t.decide(self.P), ProposalOutcome.PENDING)


# ---------------------------------------------------------------------------
# ResultProposal — 결과 제안 도메인 레코드 (D-016)
# ---------------------------------------------------------------------------

def _result_proposal(**overrides) -> ResultProposal:
    base = dict(
        id="p1", session_id="s1", proposer="writer", version=1,
        content="draft result", created_at=TS,
    )
    base.update(overrides)
    return ResultProposal(**base)


class TestResultProposal(unittest.TestCase):
    def test_valid(self) -> None:
        p = _result_proposal()
        self.assertEqual(p.id, "p1")
        self.assertEqual(p.version, 1)

    def test_rejects_empty_required_fields(self) -> None:
        # id/session_id/proposer/content/created_at 빈 값 거부.
        for field in ("id", "session_id", "proposer", "content", "created_at"):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    _result_proposal(**{field: ""})

    def test_rejects_non_positive_version(self) -> None:
        # version은 양의 int — 0/음수 거부.
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    _result_proposal(version=bad)

    def test_rejects_bool_version(self) -> None:
        # bool은 int의 서브클래스지만 version으로는 거부.
        with self.assertRaises(ContractError):
            _result_proposal(version=True)

    def test_rejects_non_int_version(self) -> None:
        # float 등 비정수 version 거부.
        with self.assertRaises(ContractError):
            _result_proposal(version=1.5)

    def test_allows_higher_version(self) -> None:
        # 반려 후 재제출은 version을 올린 새 제안 — 2 이상도 허용.
        self.assertEqual(_result_proposal(version=3).version, 3)

    def test_to_from_dict_roundtrip(self) -> None:
        # to_dict/from_dict 왕복 동일성 — status 필드 포함(기본 pending).
        p = _result_proposal(version=2, content="revised draft")
        self.assertEqual(ResultProposal.from_dict(p.to_dict()), p)
        self.assertEqual(p.to_dict(), {
            "id": "p1", "session_id": "s1", "proposer": "writer",
            "version": 2, "content": "revised draft", "created_at": TS,
            "status": "pending",
        })


class TestResultProposalStatus(unittest.TestCase):
    # D-020: 제안 수명주기 상태 전이 — PENDING→APPROVED/REJECTED, REJECTED→SUPERSEDED만 허용.
    def test_default_status_pending(self) -> None:
        self.assertEqual(_result_proposal().status, ProposalStatus.PENDING)

    def test_pending_to_approved(self) -> None:
        p = _result_proposal().with_status(ProposalStatus.APPROVED)
        self.assertEqual(p.status, ProposalStatus.APPROVED)

    def test_pending_to_rejected(self) -> None:
        p = _result_proposal().with_status(ProposalStatus.REJECTED)
        self.assertEqual(p.status, ProposalStatus.REJECTED)

    def test_rejected_to_superseded(self) -> None:
        p = (_result_proposal()
             .with_status(ProposalStatus.REJECTED)
             .with_status(ProposalStatus.SUPERSEDED))
        self.assertEqual(p.status, ProposalStatus.SUPERSEDED)

    def test_forbidden_pending_to_superseded(self) -> None:
        # PENDING→SUPERSEDED 직행 금지.
        with self.assertRaises(InvalidTransition):
            _result_proposal().with_status(ProposalStatus.SUPERSEDED)

    def test_forbidden_pending_to_pending(self) -> None:
        with self.assertRaises(InvalidTransition):
            _result_proposal().with_status(ProposalStatus.PENDING)

    def test_forbidden_rejected_to_approved(self) -> None:
        rejected = _result_proposal().with_status(ProposalStatus.REJECTED)
        with self.assertRaises(InvalidTransition):
            rejected.with_status(ProposalStatus.APPROVED)

    def test_approved_is_terminal(self) -> None:
        # APPROVED에서는 어떤 전이도 금지.
        approved = _result_proposal().with_status(ProposalStatus.APPROVED)
        for target in ProposalStatus:
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    approved.with_status(target)

    def test_superseded_is_terminal(self) -> None:
        # SUPERSEDED에서는 어떤 전이도 금지.
        superseded = (_result_proposal()
                      .with_status(ProposalStatus.REJECTED)
                      .with_status(ProposalStatus.SUPERSEDED))
        for target in ProposalStatus:
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    superseded.with_status(target)

    def test_invalid_transition_is_contract_error(self) -> None:
        self.assertTrue(issubclass(InvalidTransition, ContractError))

    def test_with_status_returns_new_instance(self) -> None:
        p = _result_proposal()
        p2 = p.with_status(ProposalStatus.APPROVED)
        self.assertEqual(p.status, ProposalStatus.PENDING)
        self.assertIsNot(p, p2)

    def test_superseded_roundtrip(self) -> None:
        # 전이 후 상태가 직렬화에 반영되고 왕복에서 보존된다.
        p = (_result_proposal()
             .with_status(ProposalStatus.REJECTED)
             .with_status(ProposalStatus.SUPERSEDED))
        d = p.to_dict()
        self.assertEqual(d["status"], "superseded")
        self.assertEqual(ResultProposal.from_dict(d), p)


# ---------------------------------------------------------------------------
# Vote — 투표 도메인 레코드 (D-020)
# ---------------------------------------------------------------------------

def _vote_record(**overrides) -> Vote:
    base = dict(
        id="v1", session_id="s1", proposal_id="p1", voter="critic",
        decision=VoteDecision.APPROVE, created_at=TS,
    )
    base.update(overrides)
    return Vote(**base)


class TestVoteRecord(unittest.TestCase):
    def test_valid_approve_without_reason(self) -> None:
        # APPROVE는 사유를 생략할 수 있다(기본 빈 문자열).
        v = _vote_record()
        self.assertEqual(v.decision, VoteDecision.APPROVE)
        self.assertEqual(v.reason, "")

    def test_approve_with_reason_ok(self) -> None:
        v = _vote_record(reason="looks solid")
        self.assertEqual(v.reason, "looks solid")

    def test_reject_requires_reason(self) -> None:
        # REJECT는 제출자 보완을 위해 사유가 필수.
        with self.assertRaises(ContractError):
            _vote_record(decision=VoteDecision.REJECT)
        with self.assertRaises(ContractError):
            _vote_record(decision=VoteDecision.REJECT, reason="")

    def test_reject_with_reason_ok(self) -> None:
        v = _vote_record(decision=VoteDecision.REJECT, reason="needs sources")
        self.assertEqual(v.decision, VoteDecision.REJECT)
        self.assertEqual(v.reason, "needs sources")

    def test_rejects_empty_required_fields(self) -> None:
        # id/session_id/proposal_id/voter/created_at 빈 값 거부.
        for field in ("id", "session_id", "proposal_id", "voter", "created_at"):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    _vote_record(**{field: ""})

    def test_approve_roundtrip_serializes_enum(self) -> None:
        v = _vote_record()
        d = v.to_dict()
        self.assertEqual(d["decision"], "approve")
        self.assertEqual(Vote.from_dict(d), v)

    def test_reject_roundtrip_serializes_enum(self) -> None:
        v = _vote_record(decision=VoteDecision.REJECT, reason="insufficient detail")
        d = v.to_dict()
        self.assertEqual(d["decision"], "reject")
        self.assertEqual(d["reason"], "insufficient detail")
        self.assertEqual(Vote.from_dict(d), v)


# ---------------------------------------------------------------------------
# FailReason / allowed_commands — 상태별 명령 허용 (D-021, D-024)
# ---------------------------------------------------------------------------

class TestFailReason(unittest.TestCase):
    def test_interrupted_exists(self) -> None:
        # D-021: 서버 재시작 시 이전 running/voting 세션 처리용 사유.
        self.assertEqual(FailReason.INTERRUPTED.value, "interrupted")


class TestAllowedCommands(unittest.TestCase):
    # D-032: running={send_message, submit_result}, voting={vote_result},
    # 종료 상태 3종은 빈 집합.
    def test_running(self) -> None:
        self.assertEqual(
            allowed_commands(SessionStatus.RUNNING),
            frozenset({COMMAND_SEND_MESSAGE, COMMAND_SUBMIT_RESULT}),
        )

    def test_voting(self) -> None:
        self.assertEqual(
            allowed_commands(SessionStatus.VOTING),
            frozenset({COMMAND_VOTE_RESULT}),
        )

    def test_terminal_states_empty(self) -> None:
        for status in (SessionStatus.COMPLETED, SessionStatus.FAILED,
                       SessionStatus.CANCELLED):
            with self.subTest(status=status):
                self.assertEqual(allowed_commands(status), frozenset())


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

    def test_interrupted_roundtrip(self) -> None:
        # D-021: INTERRUPTED는 유효한 종료 사유이며 왕복에서 보존된다.
        s = _session(status=SessionStatus.FAILED,
                     fail_reason=FailReason.INTERRUPTED, finished_at=TS2)
        d = s.to_dict()
        self.assertEqual(d["fail_reason"], "interrupted")
        self.assertEqual(Session.from_dict(d), s)


# ---------------------------------------------------------------------------
# Event + make_* 헬퍼
# ---------------------------------------------------------------------------

def _event(**overrides) -> Event:
    # D-022 봉투: event_id/session_id/type/sequence/created_at/payload.
    base = dict(event_id="e1", session_id="s1", type=EventType.USAGE,
                sequence=0, created_at=TS, payload={})
    base.update(overrides)
    return Event(**base)


class TestEventValidation(unittest.TestCase):
    def test_valid_event(self) -> None:
        e = _event(sequence=0)
        self.assertEqual(e.sequence, 0)
        self.assertEqual(e.event_id, "e1")

    def test_rejects_negative_sequence(self) -> None:
        with self.assertRaises(ContractError):
            _event(sequence=-1)

    def test_rejects_non_int_sequence(self) -> None:
        with self.assertRaises(ContractError):
            _event(sequence=1.0)

    def test_rejects_empty_event_id(self) -> None:
        # event_id는 필수(전역 유일 식별자) — 빈 값 거부.
        with self.assertRaises(ContractError):
            _event(event_id="")

    def test_rejects_empty_session_id_and_created_at(self) -> None:
        with self.assertRaises(ContractError):
            _event(session_id="")
        with self.assertRaises(ContractError):
            _event(created_at="")

    def test_to_dict(self) -> None:
        # to_dict 키 개편: event_id/sequence/created_at.
        e = _event(event_id="e2", type=EventType.MESSAGE, sequence=2,
                   payload={"k": "v"})
        self.assertEqual(e.to_dict(), {
            "event_id": "e2", "session_id": "s1", "type": "message",
            "sequence": 2, "created_at": TS, "payload": {"k": "v"},
        })


class TestMakeEventHelpers(unittest.TestCase):
    def test_session_status_event_running(self) -> None:
        s = _session()
        e = make_session_status_event("e1", 5, s, TS)
        self.assertEqual(e.type, EventType.SESSION_STATUS)
        self.assertEqual(e.event_id, "e1")
        self.assertEqual(e.sequence, 5)
        self.assertEqual(e.session_id, "sess1")
        self.assertEqual(e.created_at, TS)
        self.assertEqual(
            e.payload, {"status": "running", "fail_reason": None, "fail_detail": None}
        )

    def test_session_status_event_failed_includes_reason(self) -> None:
        s = _session(status=SessionStatus.FAILED,
                     fail_reason=FailReason.MESSAGES, finished_at=TS2)
        e = make_session_status_event("e2", 1, s, TS2)
        self.assertEqual(
            e.payload,
            {"status": "failed", "fail_reason": "messages", "fail_detail": None},
        )

    def test_message_event(self) -> None:
        m = _chat()
        e = make_message_event("e3", 3, m)
        self.assertEqual(e.type, EventType.MESSAGE)
        self.assertEqual(e.event_id, "e3")
        self.assertEqual(e.sequence, 3)
        self.assertEqual(e.session_id, m.session_id)
        self.assertEqual(e.created_at, m.created_at)
        self.assertEqual(e.payload, m.to_dict())

    def test_agent_state_event(self) -> None:
        e = make_agent_state_event("e4", 4, "s1", "analyst", AgentState.THINKING, TS)
        self.assertEqual(e.type, EventType.AGENT_STATE)
        self.assertEqual(
            e.payload, {"agent": "analyst", "state": "thinking", "detail": None}
        )

    def test_usage_event(self) -> None:
        u = Usage(input_tokens=100, output_tokens=50)
        e = make_usage_event("e6", 6, "s1", u, token_budget=200000, created_at=TS)
        self.assertEqual(e.type, EventType.USAGE)
        self.assertEqual(
            e.payload,
            {
                "usage": u.to_dict(),
                "token_budget": 200000,
                "work_tokens": 150,
                "processed_tokens": 150,
                "processed_token_limit": None,
                "phase": None,
                "reserved_tokens": 0,
                "per_agent": {},
            },
        )

    def test_vote_status_event_includes_version_and_sorts_members(self) -> None:
        # make_vote_status_event는 ResultProposal 객체를 받아 payload에
        # proposal_version을 포함한다(대시보드 '제안 N차' 표시).
        p = _result_proposal(id="p1", version=2)
        t = VoteTally(
            voters=frozenset({"a", "b", "c", "d"}),
            approvals=frozenset({"c", "a"}),
            rejections=frozenset({"b"}),
        )
        e = make_vote_status_event("e7", 7, "s1", p, t, TS)
        self.assertEqual(e.type, EventType.VOTE_STATUS)
        self.assertEqual(e.payload, {
            "proposal_id": "p1",
            "proposal_version": 2,
            "approvals": ["a", "c"],
            "rejections": ["b"],
            "abstained": [],
            "pending": ["d"],
        })

    def test_result_event_requires_completed(self) -> None:
        # completed가 아닌 세션은 결과 이벤트 생성 거부.
        with self.assertRaises(ContractError):
            make_result_event("e8", 8, _session(), TS)
        with self.assertRaises(ContractError):
            make_result_event("e8", 8, _session().with_status(SessionStatus.VOTING), TS)

    def test_result_event_payload(self) -> None:
        s = _session(status=SessionStatus.COMPLETED, result="final answer",
                     submitted_by="writer", finished_at=TS2)
        e = make_result_event("e9", 9, s, TS2)
        self.assertEqual(e.type, EventType.RESULT)
        self.assertEqual(e.payload,
                         {"result": "final answer", "submitted_by": "writer"})

    def test_agent_state_event_with_detail(self) -> None:
        # DEAD 전이는 detail에 귀책 포함 사유를 싣는다 (오류 귀책 원칙).
        e = make_agent_state_event(
            "e10", 10, "s1", "researcher", AgentState.DEAD, TS,
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
        e = make_usage_event("e11", 11, "s1", session_total, token_budget=200000,
                             created_at=TS, per_agent=per_agent)
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
        e = make_session_status_event("e12", 12, s, TS2)
        self.assertEqual(e.payload["fail_detail"], "agent writer dead: provider outage")


class TestSessionDraft(unittest.TestCase):
    # 미승인 초안 보존 (D-025) — 투표까지 갔지만 확정되지 못한 실패 세션에서
    # 마지막 제안 내용을 보존한다. FAILED에서만, draft_result/draft_proposer 동반 필수.
    def test_failed_session_carries_draft_and_roundtrips(self) -> None:
        s = _session(
            status=SessionStatus.FAILED,
            fail_reason=FailReason.NO_QUORUM,
            fail_detail="voting timed out: pending voters analyst",
            draft_result="Draft answer: use SQLite for persistence.",
            draft_proposer="writer",
            finished_at=TS2,
        )
        self.assertEqual(Session.from_dict(s.to_dict()), s)
        d = s.to_dict()
        self.assertEqual(d["draft_result"], "Draft answer: use SQLite for persistence.")
        self.assertEqual(d["draft_proposer"], "writer")

    def test_non_failed_rejects_draft(self) -> None:
        with self.assertRaises(ContractError):
            _session(draft_result="draft", draft_proposer="writer")
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.COMPLETED, result="final",
                     submitted_by="writer", draft_result="draft",
                     draft_proposer="writer", finished_at=TS2)

    def test_draft_fields_must_be_set_together(self) -> None:
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.FAILED, fail_reason=FailReason.NO_QUORUM,
                     draft_result="draft", finished_at=TS2)
        with self.assertRaises(ContractError):
            _session(status=SessionStatus.FAILED, fail_reason=FailReason.NO_QUORUM,
                     draft_proposer="writer", finished_at=TS2)

    def test_with_status_carries_draft(self) -> None:
        s = _session().with_status(SessionStatus.VOTING).with_status(
            SessionStatus.FAILED,
            fail_reason=FailReason.NO_QUORUM,
            draft_result="unratified draft content",
            draft_proposer="writer",
            finished_at=TS2,
        )
        self.assertEqual(s.draft_result, "unratified draft content")
        self.assertEqual(s.draft_proposer, "writer")

    def test_default_session_has_no_draft(self) -> None:
        s = _session()
        self.assertIsNone(s.draft_result)
        self.assertIsNone(s.draft_proposer)


class TestBoolRejection(unittest.TestCase):
    # bool은 int의 서브클래스지만 계약에서는 타입 오류로 거부한다.
    def test_usage_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            Usage(input_tokens=True)

    def test_event_sequence_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            Event(event_id="e1", session_id="s1", type=EventType.USAGE,
                  sequence=True, created_at=TS, payload={})

    def test_agent_spec_rejects_bool_max_turns(self) -> None:
        with self.assertRaises(ContractError):
            _agent(max_turns=True)

    def test_termination_rejects_bool(self) -> None:
        with self.assertRaises(ContractError):
            TerminationPolicy(max_messages=True)
        with self.assertRaises(ContractError):
            TerminationPolicy(idle_timeout=True)


# ---------------------------------------------------------------------------
# validate_vote — 제안 수준 불변조건 검증 (D-016)
# 심의자 자격(voters 스냅샷 포함 여부)과 중복 투표는 VoteTally.with_vote 소관이라
# 여기서는 다루지 않는다.
# ---------------------------------------------------------------------------

from hwabaek.contracts import validate_vote


class TestValidateVote(unittest.TestCase):
    def test_valid_vote_by_other_agent_passes(self) -> None:
        # 활성(pending) 제안에 타인이 투표 → 예외 없음.
        proposal = _result_proposal(proposer="writer")
        vote = _vote_record(voter="critic")
        validate_vote(vote, proposal)  # 예외가 발생하지 않아야 통과.

    def test_session_mismatch_raises(self) -> None:
        # 세션이 다른 투표는 거부.
        proposal = _result_proposal(session_id="s1")
        vote = _vote_record(session_id="s2")
        with self.assertRaises(ContractError):
            validate_vote(vote, proposal)

    def test_proposal_id_mismatch_raises_with_both_ids_in_message(self) -> None:
        # 늦은/미지의 투표: 활성 제안과 다른 proposal_id는 거부하고 두 id를 메시지에 남긴다.
        proposal = _result_proposal(id="p1")
        vote = _vote_record(proposal_id="p2")
        with self.assertRaises(ContractError) as ctx:
            validate_vote(vote, proposal)
        self.assertIn("p1", str(ctx.exception))
        self.assertIn("p2", str(ctx.exception))

    def test_rejected_proposal_rejects_vote(self) -> None:
        # pending이 아닌(REJECTED) 제안에는 투표할 수 없다.
        proposal = _result_proposal().with_status(ProposalStatus.REJECTED)
        vote = _vote_record()
        with self.assertRaises(ContractError):
            validate_vote(vote, proposal)

    def test_superseded_proposal_rejects_vote(self) -> None:
        # pending이 아닌(SUPERSEDED) 제안에는 투표할 수 없다.
        proposal = (_result_proposal()
                    .with_status(ProposalStatus.REJECTED)
                    .with_status(ProposalStatus.SUPERSEDED))
        vote = _vote_record()
        with self.assertRaises(ContractError):
            validate_vote(vote, proposal)

    def test_proposer_self_vote_rejected(self) -> None:
        # 제출자는 자기 제안에 투표할 수 없다 (D-020).
        proposal = _result_proposal(proposer="writer")
        vote = _vote_record(voter="writer")
        with self.assertRaises(ContractError):
            validate_vote(vote, proposal)


# ---------------------------------------------------------------------------
# AgentCapability / AgentSpec.capabilities / TeamConfig 권한 검증 (D-027)
# 런타임(SessionManager)이 프롬프트가 아니라 이 권한 목록으로 도구 호출을 강제한다.
# ---------------------------------------------------------------------------

from hwabaek.contracts import ALL_CAPABILITIES, AgentCapability


class TestAgentCapability(unittest.TestCase):
    def test_values_match_command_constants(self) -> None:
        # 권한 값은 명령 이름과 동일해야 한다(런타임이 이 값으로 호출을 대조).
        self.assertEqual(AgentCapability.SEND_MESSAGE.value, COMMAND_SEND_MESSAGE)
        self.assertEqual(AgentCapability.SUBMIT_RESULT.value, COMMAND_SUBMIT_RESULT)
        self.assertEqual(AgentCapability.VOTE_RESULT.value, COMMAND_VOTE_RESULT)

    def test_all_capabilities_is_full_set(self) -> None:
        # ALL_CAPABILITIES는 3종 전체를 담은 frozenset.
        self.assertIsInstance(ALL_CAPABILITIES, frozenset)
        self.assertEqual(ALL_CAPABILITIES, frozenset(AgentCapability))
        self.assertEqual(len(ALL_CAPABILITIES), 3)
        self.assertEqual(
            {c.value for c in ALL_CAPABILITIES},
            {COMMAND_SEND_MESSAGE, COMMAND_SUBMIT_RESULT, COMMAND_VOTE_RESULT},
        )


class TestAgentSpecCapabilities(unittest.TestCase):
    # capabilities는 frozenset[AgentCapability] — 생략 시 전체 권한, 빈 집합 허용.
    def test_default_is_all_capabilities(self) -> None:
        self.assertEqual(_agent().capabilities, ALL_CAPABILITIES)

    def test_explicit_frozenset_preserved(self) -> None:
        # 명시한 frozenset은 그대로 보존된다.
        caps = frozenset({AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT})
        self.assertEqual(_agent(capabilities=caps).capabilities, caps)

    def test_rejects_non_frozenset(self) -> None:
        # frozenset이 아닌 타입(list/set 등)은 거부 — 불변 집합만 허용.
        for bad in ([AgentCapability.SEND_MESSAGE], {AgentCapability.SEND_MESSAGE}):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(ContractError):
                    _agent(capabilities=bad)

    def test_rejects_string_members(self) -> None:
        # 원소가 문자열(명령 이름)인 frozenset은 거부 — AgentCapability 인스턴스만 허용.
        with self.assertRaises(ContractError):
            _agent(capabilities=frozenset({"send_message"}))

    def test_empty_frozenset_allowed(self) -> None:
        # 빈 권한(관찰 전용 에이전트 — 어떤 도구도 호출 불가)도 허용된다.
        spec = _agent(capabilities=frozenset())
        self.assertEqual(spec.capabilities, frozenset())


class TestTeamConfigCapabilities(unittest.TestCase):
    # D-027 권한 정합성: 제출 가능 에이전트 최소 1명, 투표 모드에서는 각 제출자마다
    # 자기 제안을 심의할 수 있는(다른, vote_result 보유) 에이전트가 1명 이상 필요.
    _SUBMIT_ONLY = frozenset({AgentCapability.SUBMIT_RESULT})
    _SEND_VOTE = frozenset({AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT})
    _SEND_ONLY = frozenset({AgentCapability.SEND_MESSAGE})

    def test_rejects_no_submitter(self) -> None:
        # 제출 가능 에이전트 0명 → 거부(오류 메시지에 submit_result 언급).
        with self.assertRaises(ContractError) as ctx:
            _team(agents=(
                _agent(name="analyst", capabilities=self._SEND_VOTE),
                _agent(name="reviewer", capabilities=self._SEND_VOTE),
            ))
        self.assertIn("submit_result", str(ctx.exception))

    def test_rejects_submitter_without_eligible_voter(self) -> None:
        # unanimous(기본): 제출자 writer 외에 vote_result 보유자가 없으면 거부
        # (제출자 이름을 오류 메시지에 포함).
        with self.assertRaises(ContractError) as ctx:
            _team(agents=(
                _agent(name="writer", capabilities=self._SUBMIT_ONLY),
                _agent(name="helper", capabilities=self._SEND_ONLY),
            ))
        self.assertIn("writer", str(ctx.exception))

    def test_first_mode_allows_no_voter(self) -> None:
        # first 모드는 투표를 생략하므로 심의 가능 에이전트가 없어도 허용된다.
        cfg = TerminationPolicy(approval=ApprovalConfig(mode=ApprovalPolicy.FIRST))
        t = _team(
            agents=(
                _agent(name="writer", capabilities=self._SUBMIT_ONLY),
                _agent(name="helper", capabilities=self._SEND_ONLY),
            ),
            termination=cfg,
        )
        self.assertEqual(len(t.agents), 2)

    def test_valid_proposal_team_shape(self) -> None:
        # 제안 팀 형태: 제출 전용 1 + 투표 가능 2 → unanimous 기본에서 통과.
        t = _team(agents=(
            _agent(name="proposer", capabilities=self._SUBMIT_ONLY),
            _agent(name="voter1", capabilities=self._SEND_VOTE),
            _agent(name="voter2", capabilities=self._SEND_VOTE),
        ))
        self.assertEqual(len(t.agents), 3)


if __name__ == "__main__":
    unittest.main()
