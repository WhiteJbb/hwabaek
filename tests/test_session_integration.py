"""SessionManager 통합 테스트 — bus -> agent 루프 -> consensus -> session 전체
스택을 Fake LLM으로 밀폐 검증한다 (Plan "M2 코어 엔진" 완료 기준의 실패 경로 목록).

"실패 경로가 제품이다" — 정상 합의 1건 외에는 대부분 실패/경합 경로다.

밀폐 원칙: 실키/실네트워크 금지. 고정 clock + 순번 id_factory 주입. 동기화는 고정
sleep이 아니라 이벤트(on_event 수집)/메시지 트리거로 처리하고, 행 방지용으로만
asyncio.wait_for(timeout)을 쓴다. 타이머는 아주 짧게 둔다. 데이터는 영어 ASCII,
주석은 한국어.
"""
from __future__ import annotations

import asyncio
import itertools
import unittest

from hwabaek.contracts import (
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    EventType,
    FailReason,
    MessageType,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    Usage,
)
from hwabaek.contracts import AgentCapability
from hwabaek.llm.base import LLMServerError
from hwabaek.llm.fake import text_response, tool_response
from hwabaek.session import SessionManager

CLOCK = "2026-07-14T00:00:00Z"
TIMEOUT = 5.0  # 개별 run()의 행 방지 상한


# ---------------------------------------------------------------------------
# 테스트용 LLM 스텁 — 스크립트 소비형(소진 후 조용한 END 응답)
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """스크립트를 순서대로 소비하는 LLMClient 스텁.

    정적 FakeLLMClient는 스크립트 소진 시 AssertionError를 던져 단위 테스트의
    누락을 드러내지만, 통합에서는 세션 종료 뒤 잔여 브로드캐스트(늦은 투표 등)로
    깨어난 에이전트가 한 번 더 호출을 시도하는 것이 정상 동작이다. 그래서 여기서는
    소진 후 조용한 text_response(END)를 돌려 에이전트를 다시 유휴로 보낸다.
    calls에 모든 요청을 기록해 호출 수 단언(취소 후 무증가 등)에 사용한다.
    """

    def __init__(self, script) -> None:
        self._script = list(script)
        self._i = 0
        self.calls = []

    async def complete(self, request):
        self.calls.append(request)
        if self._i < len(self._script):
            item = self._script[self._i]
            self._i += 1
            if isinstance(item, BaseException):  # LLMError 주입 = raise
                raise item
            return item
        return text_response("(idle)")


# 스크립트 편의 생성기 --------------------------------------------------------

def _submit(content: str):
    return tool_response("submit_result", {"content": content})


def _vote(decision: str, *, reason: str = "", proposal_id: str | None = None):
    args: dict = {"decision": decision}
    if reason:
        args["reason"] = reason
    if proposal_id is not None:
        args["proposal_id"] = proposal_id
    return tool_response("vote_result", args)


def _chat(content: str, recipients=("*",)):
    return tool_response("send_message", {"recipients": list(recipients), "content": content})


def _text(body: str = "thinking"):
    return text_response(body)


# ---------------------------------------------------------------------------
# 이벤트 수집기 — 순서/타입 단언 + 상태 도달/전원 유휴 게이트
# ---------------------------------------------------------------------------

class _Collector:
    """on_event 콜백. 이벤트를 순서대로 모으고, 특정 세션 상태 도달과
    '전원 최소 1회 유휴' 시점을 asyncio.Event로 노출한다(게이트)."""

    def __init__(self) -> None:
        self.events = []
        self._status_gates: dict[str, asyncio.Event] = {}
        self._idle_agents: set[str] = set()
        self._expected_idle: int | None = None
        self._parked: asyncio.Event | None = None

    def __call__(self, event) -> None:
        self.events.append(event)
        if event.type is EventType.SESSION_STATUS:
            status = event.payload["status"]
            gate = self._status_gates.get(status)
            if gate is not None:
                gate.set()
        elif event.type is EventType.AGENT_STATE and event.payload["state"] == "idle":
            self._idle_agents.add(event.payload["agent"])
            if (
                self._expected_idle is not None
                and self._parked is not None
                and len(self._idle_agents) >= self._expected_idle
            ):
                self._parked.set()

    def status_gate(self, status: SessionStatus) -> asyncio.Event:
        """해당 상태에 도달하면 set되는 게이트(이미 지났으면 즉시 set)."""
        gate = self._status_gates.get(status.value)
        if gate is None:
            gate = asyncio.Event()
            self._status_gates[status.value] = gate
            for e in self.events:
                if e.type is EventType.SESSION_STATUS and e.payload["status"] == status.value:
                    gate.set()
                    break
        return gate

    def all_idle_gate(self, n: int) -> asyncio.Event:
        """n명이 각자 최소 1회 유휴 상태를 발행하면 set되는 게이트."""
        self._expected_idle = n
        self._parked = asyncio.Event()
        if len(self._idle_agents) >= n:
            self._parked.set()
        return self._parked

    # 조회 헬퍼 --------------------------------------------------------------

    def statuses(self) -> list[str]:
        return [
            e.payload["status"] for e in self.events
            if e.type is EventType.SESSION_STATUS
        ]

    def messages(self, mtype: MessageType | None = None) -> list:
        out = []
        for e in self.events:
            if e.type is EventType.MESSAGE and (
                mtype is None or e.payload["type"] == mtype.value
            ):
                out.append(e)
        return out

    def vote_status(self) -> list:
        return [e for e in self.events if e.type is EventType.VOTE_STATUS]

    def agent_states(self) -> list:
        return [e for e in self.events if e.type is EventType.AGENT_STATE]

    def has_type(self, etype: EventType) -> bool:
        return any(e.type is etype for e in self.events)


# ---------------------------------------------------------------------------
# 공통 하네스
# ---------------------------------------------------------------------------

class SessionIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """전체 스택 밀폐 통합 — 코드로 직접 TeamConfig 구성(YAML 불필요)."""

    def _spec(self, name: str) -> AgentSpec:
        return AgentSpec(
            name=name,
            role="tester",
            system_prompt="You are a hermetic test agent.",
        )

    def _build(
        self,
        agents_scripts,
        *,
        mode: ApprovalPolicy = ApprovalPolicy.UNANIMOUS,
        idle_timeout: float = 0.05,
        voting_timeout: float = 0.05,
        max_messages: int = 100,
        token_budget: int = 200_000,
        task: str = "produce the deliverable",
    ):
        """(name, script) 목록으로 팀/매니저를 조립한다. 반환: (manager, coll, fakes)."""
        specs = tuple(self._spec(name) for name, _ in agents_scripts)
        team = TeamConfig(
            name="team",
            agents=specs,
            termination=TerminationPolicy(
                max_messages=max_messages,
                token_budget=token_budget,
                idle_timeout=idle_timeout,
                approval=ApprovalConfig(mode=mode, voting_timeout=voting_timeout),
            ),
        )
        fakes = {name: ScriptedLLM(script) for name, script in agents_scripts}
        coll = _Collector()
        counter = itertools.count()

        def id_factory() -> str:
            return f"id-{next(counter):05d}"

        manager = SessionManager(
            team,
            task,
            llm_factory=lambda spec: fakes[spec.name],
            clock=lambda: CLOCK,
            id_factory=id_factory,
            on_event=coll,
        )
        return manager, coll, fakes

    async def _run(self, manager) -> "object":
        return await asyncio.wait_for(manager.run(), TIMEOUT)

    # -----------------------------------------------------------------------
    # 1) 정상 합의 (3인 unanimous)
    # -----------------------------------------------------------------------
    async def test_happy_unanimous_consensus(self) -> None:
        """writer submit -> 나머지 2명 approve -> completed."""
        manager, coll, _ = self._build([
            ("writer", [_submit("the final deliverable")]),
            ("analyst", [_text(), _vote("approve")]),
            ("reviewer", [_text(), _vote("approve")]),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(session.result, "the final deliverable")
        self.assertEqual(session.submitted_by, "writer")
        # result 이벤트 존재.
        self.assertTrue(coll.has_type(EventType.RESULT))
        # session_status 이벤트 순서: running -> voting -> completed.
        self.assertEqual(coll.statuses(), ["running", "voting", "completed"])

    # -----------------------------------------------------------------------
    # 2) 반려 후 재제출 (version 1 -> 2)
    # -----------------------------------------------------------------------
    async def test_reject_then_resubmit_v2(self) -> None:
        """analyst reject(사유) -> running 복귀 -> writer 재제출(v2) -> approve -> completed."""
        reason = "needs more supporting detail"
        manager, coll, _ = self._build([
            # 제출 v1 -> (park) -> 재제출 v2. 중간 text가 없으면 voting 중 재제출이라 거부됨.
            ("writer", [_submit("draft one"), _text("await votes"), _submit("draft two final")]),
            ("analyst", [_text(), _vote("reject", reason=reason), _text("await v2"), _vote("approve")]),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(session.result, "draft two final")
        # vote_status의 proposal_version이 1에서 2로 단조 증가.
        versions = [e.payload["proposal_version"] for e in coll.vote_status()]
        self.assertIn(1, versions)
        self.assertIn(2, versions)
        self.assertEqual(versions, sorted(versions))  # 1들이 2들보다 앞
        self.assertEqual(versions[-1], 2)
        # 반려 사유가 vote 메시지 이벤트에 존재.
        rejects = [
            e for e in coll.messages(MessageType.VOTE)
            if e.payload["vote"] == "reject"
        ]
        self.assertTrue(rejects)
        self.assertIn(reason, rejects[0].payload["content"])

    # -----------------------------------------------------------------------
    # 3) voting 중 중복 submit 거부 (도구 오류, 세션은 계속 진행)
    # -----------------------------------------------------------------------
    async def test_duplicate_submit_during_voting_is_rejected(self) -> None:
        """voting 중 다른 에이전트의 submit_result는 tool error로 돌아가고 세션은 completed."""
        manager, coll, _ = self._build([
            ("writer", [_submit("the deliverable")]),
            # analyst가 voting 중 submit 시도(거부) 후 정상 투표.
            ("analyst", [_text(), _submit("sneaky second proposal"), _vote("approve")]),
            ("reviewer", [_text(), _vote("approve")]),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        # 거부되었으므로 제안 메시지는 정확히 1건(중복 submit은 제안을 만들지 못함).
        self.assertEqual(len(coll.messages(MessageType.RESULT_PROPOSAL)), 1)
        # 판정은 v1로만 진행됨.
        self.assertTrue(all(e.payload["proposal_version"] == 1 for e in coll.vote_status()))

    # -----------------------------------------------------------------------
    # 4) 늦은 투표 무시 (stale proposal_id)
    # -----------------------------------------------------------------------
    async def test_stale_vote_is_ignored(self) -> None:
        """잘못된 proposal_id로 투표하면 무시되고 판정에 반영되지 않는다."""
        manager, coll, _ = self._build([
            ("writer", [_submit("the deliverable")]),
            # stale-id 투표(무시) 후 활성 제안에 정상 투표.
            ("analyst", [_text(), _vote("approve", proposal_id="stale-id"), _vote("approve")]),
            ("reviewer", [_text(), _vote("approve")]),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        # 무시된 투표는 VOTE 메시지를 만들지 않는다 -> 유효 투표 2건만.
        self.assertEqual(len(coll.messages(MessageType.VOTE)), 2)

    # -----------------------------------------------------------------------
    # 5) voting_timeout -> no_quorum (미투표자 + 초안 보존, D-025)
    # -----------------------------------------------------------------------
    async def test_voting_timeout_no_quorum_preserves_draft(self) -> None:
        """심의자 1명이 투표하지 않으면 voting_timeout 만료 후 failed(no_quorum)."""
        manager, coll, _ = self._build(
            [
                ("writer", [_submit("preserved draft body")]),
                ("analyst", [_text(), _vote("approve")]),
                ("reviewer", []),  # 절대 투표하지 않음 -> 기권
            ],
            voting_timeout=0.05,
            idle_timeout=0.5,  # RUNNING 유휴가 개입하지 않도록 크게
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.fail_reason, FailReason.NO_QUORUM)
        # fail_detail에 미투표자 이름.
        self.assertIn("reviewer", session.fail_detail)
        # 미승인 초안 보존(D-025).
        self.assertEqual(session.draft_result, "preserved draft body")
        self.assertEqual(session.draft_proposer, "writer")
        self.assertIsNone(session.result)

    # -----------------------------------------------------------------------
    # 6) idle -> failed(idle) (초안 없음)
    # -----------------------------------------------------------------------
    async def test_all_idle_fails_idle(self) -> None:
        """모든 에이전트가 초기 호출 후 아무 도구도 쓰지 않으면 failed(idle)."""
        manager, coll, _ = self._build(
            [("analyst", []), ("reviewer", [])],
            idle_timeout=0.05,
            voting_timeout=0.5,
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.fail_reason, FailReason.IDLE)
        self.assertIsNone(session.draft_result)
        self.assertIsNone(session.result)

    # -----------------------------------------------------------------------
    # 7) 메시지 상한 -> failed(messages)
    # -----------------------------------------------------------------------
    async def test_message_cap_fails_messages(self) -> None:
        """max_messages를 작게 두고 수다 스크립트로 상한 초과 유도."""
        manager, coll, _ = self._build(
            [
                ("chatty", [_chat("m1", ["helper"]), _chat("m2", ["helper"]), _chat("m3", ["helper"])]),
                ("helper", []),
            ],
            max_messages=2,
            idle_timeout=0.5,
            voting_timeout=0.5,
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.fail_reason, FailReason.MESSAGES)

    # -----------------------------------------------------------------------
    # 8) 예산 초과 -> failed(budget)
    # -----------------------------------------------------------------------
    async def test_token_budget_fails_budget(self) -> None:
        """usage가 큰 응답을 주입하고 token_budget을 작게 두면 failed(budget)."""
        big = text_response("expensive", usage=Usage(input_tokens=1000))
        manager, coll, _ = self._build(
            [("big", [big]), ("helper", [])],
            token_budget=100,
            idle_timeout=1.0,
            voting_timeout=1.0,
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.fail_reason, FailReason.BUDGET)
        self.assertTrue(coll.has_type(EventType.USAGE))

    # -----------------------------------------------------------------------
    # 9) 에이전트 사망 -> failed(agent_error) (귀책 detail 포함)
    # -----------------------------------------------------------------------
    async def test_agent_death_fails_agent_error(self) -> None:
        """2인 팀에서 1명의 LLM이 서버 오류 -> dead -> 생존 1명 -> failed(agent_error)."""
        manager, coll, _ = self._build(
            [
                ("dying", [LLMServerError("upstream 500")]),
                ("survivor", []),
            ],
            idle_timeout=1.0,
            voting_timeout=1.0,
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.fail_reason, FailReason.AGENT_ERROR)
        # fail_detail에 에이전트명 + 귀책 category.
        self.assertIn("dying", session.fail_detail)
        self.assertIn("provider_error", session.fail_detail)
        # DEAD agent_state 이벤트에 귀책 detail이 실린다.
        dead = [
            e for e in coll.agent_states()
            if e.payload["agent"] == "dying" and e.payload["state"] == "dead"
        ]
        self.assertTrue(dead)
        self.assertIn("provider_error", dead[0].payload["detail"])

    # -----------------------------------------------------------------------
    # 10) 취소 -> cancelled (취소 후 추가 API 호출 없음)
    # -----------------------------------------------------------------------
    async def test_cancel_stops_and_makes_no_more_calls(self) -> None:
        """진행 중 세션을 게이트로 붙잡고 cancel() -> cancelled + fake.calls 무증가."""
        manager, coll, fakes = self._build(
            [("a", []), ("b", []), ("c", [])],
            idle_timeout=10.0,  # 취소 전에 idle이 발동하지 않도록 크게
            voting_timeout=10.0,
        )
        parked = coll.all_idle_gate(3)
        run_task = asyncio.create_task(manager.run())
        # 전원이 초기 호출을 마치고 유휴에 든 시점을 게이트로 대기.
        await asyncio.wait_for(parked.wait(), TIMEOUT)

        calls_before = sum(len(f.calls) for f in fakes.values())
        self.assertEqual(calls_before, 3)  # 각자 초기 호출 1회

        manager.cancel()
        session = await asyncio.wait_for(run_task, TIMEOUT)

        self.assertEqual(session.status, SessionStatus.CANCELLED)
        self.assertIsNone(session.result)
        # 취소 이후 어떤 에이전트도 추가 LLM 호출을 하지 않았다.
        self.assertEqual(sum(len(f.calls) for f in fakes.values()), calls_before)

    # -----------------------------------------------------------------------
    # 11) idle/voting 타이머 레이스 없음
    # -----------------------------------------------------------------------
    async def test_idle_timer_does_not_fire_during_voting(self) -> None:
        """voting 중 에이전트가 유휴여도, idle_timeout << voting_timeout이라도
        세션이 failed(idle)로 죽지 않고 투표 완료 후 completed."""
        manager, coll, _ = self._build(
            [
                ("writer", [_submit("the deliverable")]),
                # 두 심의자는 제안 수신 후 일단 ack(text)만 하고 유휴로 든다 -> 트리거 후 투표.
                ("analyst", [_text(), _text("ack"), _vote("approve")]),
                ("reviewer", [_text(), _text("ack"), _vote("approve")]),
            ],
            idle_timeout=0.02,
            voting_timeout=1.0,
        )
        run_task = asyncio.create_task(manager.run())
        await asyncio.wait_for(coll.status_gate(SessionStatus.VOTING).wait(), TIMEOUT)

        # idle_timeout(0.02)의 5배를 흘려보내 '유휴여도 idle이 발동하지 않음'을 실증한다
        # (타이머 부재 증명 목적의 대기 — voting_timeout 1.0에는 한참 못 미친다).
        await asyncio.sleep(0.1)
        self.assertIs(manager.session.status, SessionStatus.VOTING)

        # 이제 심의자들을 깨워 투표시킨다(voting 중 send_message 허용).
        manager.send_message("writer", ["analyst", "reviewer"], "please cast your votes")
        session = await asyncio.wait_for(run_task, TIMEOUT)

        self.assertEqual(session.status, SessionStatus.COMPLETED)

    # -----------------------------------------------------------------------
    # 12) 종료 후 명령 거부 (감사 기록)
    # -----------------------------------------------------------------------
    async def test_command_after_termination_is_rejected(self) -> None:
        """세션 종료 뒤 도착한 send_message는 상태를 바꾸지 못하고 감사 기록에 남는다."""
        # first 모드: worker의 submit이 즉시 completed로 확정. late의 초기 호출은
        # 이미 종료된 세션을 향해 send_message를 시도한다.
        manager, coll, _ = self._build(
            [
                ("worker", [_submit("done immediately")]),
                ("late", [_chat("too late", ["worker"])]),
            ],
            mode=ApprovalPolicy.FIRST,
            idle_timeout=1.0,
            voting_timeout=1.0,
        )
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(session.result, "done immediately")
        # 세션 상태 불변 + 감사 기록.
        self.assertTrue(manager.rejected_commands)
        self.assertTrue(
            any("late" in r and "send_message" in r for r in manager.rejected_commands)
        )

    # -----------------------------------------------------------------------
    # 13) voting 중 send_message 허용
    # -----------------------------------------------------------------------
    async def test_send_message_allowed_during_voting(self) -> None:
        """voting 중 chat이 배달되고 세션은 정상 완료된다."""
        manager, coll, _ = self._build([
            ("writer", [_submit("the deliverable")]),
            # analyst가 voting 중 브로드캐스트 chat 후 투표.
            ("analyst", [_text(), _chat("discuss the draft before voting"), _vote("approve")]),
            ("reviewer", [_text(), _vote("approve")]),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        chats = coll.messages(MessageType.CHAT)
        self.assertTrue(
            any("discuss the draft before voting" in e.payload["content"] for e in chats)
        )

    # -----------------------------------------------------------------------
    # 권한(capabilities, D-027) 축 — 조립 헬퍼
    # -----------------------------------------------------------------------
    def _build_with_capabilities(
        self,
        agents_scripts,
        *,
        mode: ApprovalPolicy = ApprovalPolicy.UNANIMOUS,
        idle_timeout: float = 1.0,
        voting_timeout: float = 1.0,
        max_messages: int = 100,
        token_budget: int = 200_000,
        task: str = "produce the deliverable",
    ):
        """_build와 동일하나 (name, script, capabilities) 3튜플로 에이전트별 권한을 지정한다.

        권한 강제는 메시지 흐름으로 검증하므로 타이머는 넉넉히 둬(기본 1.0s) 정상
        승인 경로를 idle/voting 만료가 선점하지 않게 한다 — 완료는 승인 트리거로
        빠르게 일어나므로 고정 sleep은 쓰지 않는다.
        """
        specs = tuple(
            AgentSpec(
                name=name,
                role="tester",
                system_prompt="You are a hermetic test agent.",
                capabilities=capabilities,
            )
            for name, _, capabilities in agents_scripts
        )
        team = TeamConfig(
            name="team",
            agents=specs,
            termination=TerminationPolicy(
                max_messages=max_messages,
                token_budget=token_budget,
                idle_timeout=idle_timeout,
                approval=ApprovalConfig(mode=mode, voting_timeout=voting_timeout),
            ),
        )
        fakes = {name: ScriptedLLM(script) for name, script, _ in agents_scripts}
        coll = _Collector()
        counter = itertools.count()

        def id_factory() -> str:
            return f"id-{next(counter):05d}"

        manager = SessionManager(
            team,
            task,
            llm_factory=lambda spec: fakes[spec.name],
            clock=lambda: CLOCK,
            id_factory=id_factory,
            on_event=coll,
        )
        return manager, coll, fakes

    # -----------------------------------------------------------------------
    # 14) 권한 밖 submit 거부 (투표 전용 에이전트의 submit_result -> tool error)
    # -----------------------------------------------------------------------
    async def test_submit_without_capability_is_rejected(self) -> None:
        """제출 권한이 없는(투표 전용) 에이전트가 running 중 submit_result를 시도하면
        capability 가드가 tool error로 되돌리고 세션은 계속된다. 제출 권한을 가진
        proposer가 정상 제출하고 전원 승인해 completed — 제안 메시지는 정확히 1건."""
        submit_only = frozenset({AgentCapability.SUBMIT_RESULT})
        send_vote = frozenset(
            {AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT}
        )
        manager, coll, _ = self._build_with_capabilities([
            # proposer는 즉시 제출하지 않고(running 유지) 요청을 받은 뒤 제출한다 —
            # analyst의 submit 시도가 running 상태에서 일어나 상태 가드가 아니라
            # capability 가드로 거부되도록 보장한다.
            ("proposer", [_text(), _submit("the final deliverable")], submit_only),
            # analyst: running에서 submit 시도(권한 없음 -> 거부) -> proposer에게
            # 제출 요청 -> 유휴 -> 제안 수신 후 승인.
            (
                "analyst",
                [
                    _submit("analyst has no submit right"),
                    _chat("please submit the result", ["proposer"]),
                    _text(),
                    _vote("approve"),
                ],
                send_vote,
            ),
            ("reviewer", [_text(), _vote("approve")], send_vote),
        ])
        session = await self._run(manager)

        # proposer만 제출에 성공 — analyst의 submit은 제안을 만들지 못했다.
        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(session.submitted_by, "proposer")
        self.assertEqual(session.result, "the final deliverable")
        proposals = coll.messages(MessageType.RESULT_PROPOSAL)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].payload["sender"], "proposer")

    # -----------------------------------------------------------------------
    # 15) 심의자 스냅샷 자격 제외 (투표 권한 없는 생존 에이전트는 voters에서 빠짐)
    # -----------------------------------------------------------------------
    async def test_snapshot_excludes_agent_without_vote_capability(self) -> None:
        """proposer 제출 시 심의자 스냅샷 = 생존 & vote_result. observer는 살아있지만
        투표 권한이 없어 스냅샷에서 제외되므로 voter1의 approve 1표로 unanimous 확정.
        vote_status의 어떤 그룹에도 observer가 등장하지 않는다."""
        manager, coll, _ = self._build_with_capabilities([
            ("proposer", [_submit("the deliverable")],
             frozenset({AgentCapability.SUBMIT_RESULT})),
            ("voter1", [_text(), _vote("approve")],
             frozenset({AgentCapability.SEND_MESSAGE, AgentCapability.VOTE_RESULT})),
            ("observer", [_text()],
             frozenset({AgentCapability.SEND_MESSAGE})),
        ])
        session = await self._run(manager)

        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(session.submitted_by, "proposer")
        self.assertEqual(session.result, "the deliverable")
        # 최소 1건의 vote_status가 발행되었고, 어떤 이벤트/그룹에도 observer가 없다.
        vote_events = coll.vote_status()
        self.assertTrue(vote_events)
        for e in vote_events:
            for group in ("pending", "approvals", "rejections", "abstained"):
                self.assertNotIn("observer", e.payload[group])
        # 확정 시점 스냅샷 = {voter1} — voter1이 유일한 승인자로 집계된다.
        self.assertIn("voter1", vote_events[-1].payload["approvals"])


if __name__ == "__main__":
    unittest.main()
