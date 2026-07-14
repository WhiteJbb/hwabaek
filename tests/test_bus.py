"""MessageBus 검증 — 밀폐(고정 clock·순번 id_factory 주입) 비동기 단위 테스트.

밀폐 원칙: datetime.now()/random 금지, 외부 의존성·네트워크 없이 unittest만으로
구동한다. 동기화는 고정 sleep이 아니라 asyncio.Event로 결정적으로 처리한다
(플레이키 방지). sequence 시작값은 0으로 고정한다(구현 선택 — 테스트로 못박음).
"""
from __future__ import annotations

import asyncio
import unittest

from hwabaek.bus import MessageBus
from hwabaek.contracts import (
    BROADCAST,
    ContractError,
    MessageType,
    VoteDecision,
)

FIXED_CLOCK = "2026-07-14T00:00:00Z"


class MessageBusTest(unittest.IsolatedAsyncioTestCase):
    """asyncio 단일 이벤트 루프 위의 인박스/배달/대기 규칙 검증."""

    def _make_bus(self, agents=("alice", "bob", "carol"), on_message=None) -> MessageBus:
        """고정 clock과 순번 id_factory를 주입한 버스를 만든다 (밀폐)."""
        self._id_counter = 0

        def id_factory() -> str:
            self._id_counter += 1
            return f"msg-{self._id_counter:04d}"

        def clock() -> str:
            return FIXED_CLOCK

        return MessageBus(
            session_id="sess-1",
            agent_names=agents,
            clock=clock,
            id_factory=id_factory,
            on_message=on_message,
        )

    @staticmethod
    def _chat(bus: MessageBus, sender: str, recipients, content: str = "hi"):
        return bus.post(
            sender=sender,
            recipients=recipients,
            type=MessageType.CHAT,
            content=content,
        )

    # ------------------------------------------------------------------
    # post: id/created_at/sequence 부여
    # ------------------------------------------------------------------
    async def test_post_assigns_sequence_id_and_created_at(self) -> None:
        bus = self._make_bus()
        m0 = self._chat(bus, "alice", ("bob",), "first")
        m1 = self._chat(bus, "alice", ("bob",), "second")
        # sequence는 0부터 세션 단위 단조 증가.
        self.assertEqual(m0.sequence, 0)
        self.assertEqual(m1.sequence, 1)
        # id는 주입한 순번 factory에서, created_at은 고정 clock에서.
        self.assertEqual(m0.id, "msg-0001")
        self.assertEqual(m1.id, "msg-0002")
        self.assertEqual(m0.created_at, FIXED_CLOCK)
        self.assertEqual(m1.created_at, FIXED_CLOCK)
        self.assertEqual(m0.session_id, "sess-1")

    # ------------------------------------------------------------------
    # 직접 배달
    # ------------------------------------------------------------------
    async def test_direct_delivery_only_to_named_recipient(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob",))
        self.assertEqual(bus.pending_count("bob"), 1)
        self.assertEqual(bus.pending_count("carol"), 0)
        # 발신자 자신에게는 배달되지 않는다.
        self.assertEqual(bus.pending_count("alice"), 0)

    async def test_direct_delivery_to_multiple_named_recipients(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob", "carol"))
        self.assertEqual(bus.pending_count("bob"), 1)
        self.assertEqual(bus.pending_count("carol"), 1)
        self.assertEqual(bus.pending_count("alice"), 0)

    # ------------------------------------------------------------------
    # 브로드캐스트 + on_message
    # ------------------------------------------------------------------
    async def test_broadcast_excludes_sender_and_notifies_once(self) -> None:
        delivered: list = []
        bus = self._make_bus(on_message=delivered.append)
        msg = self._chat(bus, "alice", (BROADCAST,), "hello all")
        # 발신자 제외 전원 배달.
        self.assertEqual(bus.pending_count("bob"), 1)
        self.assertEqual(bus.pending_count("carol"), 1)
        self.assertEqual(bus.pending_count("alice"), 0)
        # 브로드캐스트라도 원본 기준 정확히 1회 통지.
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0], msg)

    async def test_on_message_fires_once_per_direct_post(self) -> None:
        delivered: list = []
        bus = self._make_bus(on_message=delivered.append)
        self._chat(bus, "alice", ("bob",))
        self._chat(bus, "alice", ("bob", "carol"))
        self.assertEqual(len(delivered), 2)

    # ------------------------------------------------------------------
    # 동일 id 중복 배달 무시(멱등) — 중복 수신자 방어
    # ------------------------------------------------------------------
    async def test_duplicate_recipient_deduped_per_inbox(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob", "bob"))
        # 같은 인박스에는 동일 id가 한 번만 들어간다.
        self.assertEqual(bus.pending_count("bob"), 1)

    # ------------------------------------------------------------------
    # drain: 원자성 / 오름차순 / 빈 인박스
    # ------------------------------------------------------------------
    async def test_drain_returns_ascending_and_empties_inbox(self) -> None:
        bus = self._make_bus()
        m0 = self._chat(bus, "alice", ("bob",), "a")
        m1 = self._chat(bus, "carol", ("bob",), "b")
        batch = bus.drain("bob")
        self.assertEqual([m.id for m in batch], [m0.id, m1.id])
        self.assertEqual([m.sequence for m in batch], [0, 1])
        # drain 후 인박스는 비어 있어야 한다.
        self.assertEqual(bus.pending_count("bob"), 0)

    async def test_drain_atomic_snapshot_next_post_is_next_batch(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob",), "a")
        self._chat(bus, "alice", ("bob",), "b")
        first = bus.drain("bob")
        self.assertEqual([m.sequence for m in first], [0, 1])
        # drain 이후 도착분은 다음 배치로만 나온다.
        m2 = self._chat(bus, "carol", ("bob",), "c")
        second = bus.drain("bob")
        self.assertEqual([m.id for m in second], [m2.id])
        self.assertEqual([m.sequence for m in second], [2])

    async def test_drain_empty_inbox_returns_empty_list(self) -> None:
        bus = self._make_bus()
        self.assertEqual(bus.drain("bob"), [])
        # 재호출도 빈 리스트.
        self.assertEqual(bus.drain("bob"), [])

    # ------------------------------------------------------------------
    # wait_for_messages: 즉시 반환 / post 시 깨어남 / 취소 전파
    # ------------------------------------------------------------------
    async def test_wait_returns_immediately_when_messages_present(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob",))
        # 이미 있으므로 즉시 반환 — wait_for로 타임아웃 방어.
        await asyncio.wait_for(bus.wait_for_messages("bob"), timeout=1.0)

    async def test_wait_wakes_up_on_post(self) -> None:
        bus = self._make_bus()
        entered = asyncio.Event()

        async def waiter() -> None:
            # 대기 진입 직전임을 알린다. 이후 인박스가 비어 event.wait()에서 블록된다.
            entered.set()
            await bus.wait_for_messages("bob")

        task = asyncio.create_task(waiter())
        # entered.set() 이후 waiter는 곧바로 event.wait()로 진입해 블록되고,
        # 그 다음에야 이 코루틴이 재개된다(단일 루프 스케줄 순서). 고정 sleep 불필요.
        await entered.wait()
        self.assertFalse(task.done())
        # 아직 bob 인박스는 비어 있어야 한다.
        self.assertEqual(bus.pending_count("bob"), 0)
        # post가 대기자를 깨운다.
        self._chat(bus, "alice", ("bob",))
        await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(task.done())
        self.assertIsNone(task.exception())

    async def test_notice_wakes_waiter_without_posting_domain_message(self) -> None:
        delivered: list = []
        bus = self._make_bus(on_message=delivered.append)
        entered = asyncio.Event()

        async def waiter() -> None:
            entered.set()
            await bus.wait_for_messages("bob")

        task = asyncio.create_task(waiter())
        await entered.wait()
        self.assertFalse(task.done())

        bus.post_notice("bob", "prepare the proposal")
        await asyncio.wait_for(task, timeout=1.0)

        self.assertEqual(bus.pending_count("bob"), 1)
        self.assertEqual(bus.total_posted(), 0)
        self.assertEqual(delivered, [])
        self.assertEqual(bus.drain_notices("bob"), ["prepare the proposal"])
        self.assertEqual(bus.pending_count("bob"), 0)

    async def test_wait_returns_immediately_when_notice_present(self) -> None:
        bus = self._make_bus()
        bus.post_notice("bob", "synthesize now")
        await asyncio.wait_for(bus.wait_for_messages("bob"), timeout=1.0)

    async def test_notice_drain_preserves_order(self) -> None:
        bus = self._make_bus()
        bus.post_notice("bob", "first")
        bus.post_notice("bob", "second")
        self.assertEqual(bus.drain_notices("bob"), ["first", "second"])
        self.assertEqual(bus.drain_notices("bob"), [])

    async def test_shared_wake_stays_set_until_messages_and_notices_drained(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob",), "domain")
        bus.post_notice("bob", "control")

        self.assertEqual(bus.pending_count("bob"), 2)
        self.assertEqual(len(bus.drain("bob")), 1)
        self.assertEqual(bus.pending_count("bob"), 1)
        # 메시지만 비운 뒤에도 남은 알림 때문에 즉시 반환한다.
        await asyncio.wait_for(bus.wait_for_messages("bob"), timeout=1.0)

        self.assertEqual(bus.drain_notices("bob"), ["control"])
        self.assertEqual(bus.pending_count("bob"), 0)

        # 반대 순서로 비워도 메시지가 남아 있는 동안 즉시 반환해야 한다.
        bus.post_notice("bob", "control-2")
        self._chat(bus, "alice", ("bob",), "domain-2")
        self.assertEqual(bus.drain_notices("bob"), ["control-2"])
        await asyncio.wait_for(bus.wait_for_messages("bob"), timeout=1.0)
        self.assertEqual(len(bus.drain("bob")), 1)
        self.assertEqual(bus.pending_count("bob"), 0)

    async def test_wait_propagates_cancellation(self) -> None:
        bus = self._make_bus()
        entered = asyncio.Event()

        async def waiter() -> None:
            entered.set()
            await bus.wait_for_messages("bob")

        task = asyncio.create_task(waiter())
        await entered.wait()
        self.assertFalse(task.done())
        task.cancel()
        # CancelledError는 잡히지 않고 전파되어야 한다.
        with self.assertRaises(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # 미등록 sender/수신자, 자기송신 거부
    # ------------------------------------------------------------------
    async def test_unregistered_sender_rejected_with_name(self) -> None:
        bus = self._make_bus()
        with self.assertRaises(ContractError) as ctx:
            self._chat(bus, "dave", ("bob",))
        self.assertIn("dave", str(ctx.exception))
        # 거부된 post는 배달/카운트에 영향이 없다.
        self.assertEqual(bus.total_posted(), 0)
        self.assertEqual(bus.pending_count("bob"), 0)

    async def test_unregistered_recipient_rejected_with_name(self) -> None:
        bus = self._make_bus()
        with self.assertRaises(ContractError) as ctx:
            self._chat(bus, "alice", ("dave",))
        self.assertIn("dave", str(ctx.exception))
        self.assertEqual(bus.total_posted(), 0)

    async def test_self_send_rejected_via_contract(self) -> None:
        bus = self._make_bus()
        # 수신자 alice는 등록돼 있으므로 버스 검증은 통과하고, 자기송신 금지는
        # Message 계약이 거부한다(계약 경유 확인).
        with self.assertRaises(ContractError):
            self._chat(bus, "alice", ("alice",))
        self.assertEqual(bus.total_posted(), 0)

    async def test_unknown_agent_query_rejected(self) -> None:
        bus = self._make_bus()
        with self.assertRaises(ContractError):
            bus.drain("ghost")
        with self.assertRaises(ContractError):
            bus.pending_count("ghost")
        with self.assertRaises(ContractError):
            await bus.wait_for_messages("ghost")
        with self.assertRaises(ContractError):
            bus.post_notice("ghost", "control")
        with self.assertRaises(ContractError):
            bus.drain_notices("ghost")

    async def test_empty_notice_is_rejected_without_side_effects(self) -> None:
        delivered: list = []
        bus = self._make_bus(on_message=delivered.append)
        with self.assertRaises(ContractError):
            bus.post_notice("bob", "")
        self.assertEqual(bus.pending_count("bob"), 0)
        self.assertEqual(bus.total_posted(), 0)
        self.assertEqual(delivered, [])

    async def test_deactivated_agent_drops_pending_and_future_delivery(self) -> None:
        bus = self._make_bus()
        self._chat(bus, "alice", ("bob",))
        bus.post_notice("bob", "control")
        bus.deactivate("bob")

        self.assertEqual(bus.pending_count("bob"), 0)
        self._chat(bus, "carol", ("bob",))
        self._chat(bus, "carol", (BROADCAST,))
        bus.post_notice("bob", "late control")
        self.assertEqual(bus.pending_count("bob"), 0)

    # ------------------------------------------------------------------
    # total_posted / pending_count
    # ------------------------------------------------------------------
    async def test_total_posted_counts_only_successful_posts(self) -> None:
        bus = self._make_bus()
        self.assertEqual(bus.total_posted(), 0)
        self._chat(bus, "alice", ("bob",))
        self._chat(bus, "alice", (BROADCAST,))
        self.assertEqual(bus.total_posted(), 2)
        # bob은 직접 + 브로드캐스트, carol은 브로드캐스트만.
        self.assertEqual(bus.pending_count("bob"), 2)
        self.assertEqual(bus.pending_count("carol"), 1)
        self.assertEqual(bus.pending_count("alice"), 0)
        # 실패한 post는 총수를 올리지 않고, 다음 유효 post의 sequence에 구멍을 내지 않는다.
        with self.assertRaises(ContractError):
            self._chat(bus, "ghost", ("bob",))
        self.assertEqual(bus.total_posted(), 2)
        m = self._chat(bus, "carol", ("bob",))
        self.assertEqual(m.sequence, 2)
        self.assertEqual(bus.total_posted(), 3)

    # ------------------------------------------------------------------
    # VOTE / RESULT_PROPOSAL 타입도 배달됨 (브로드캐스트 강제는 계약이 검증)
    # ------------------------------------------------------------------
    async def test_result_proposal_and_vote_are_delivered(self) -> None:
        bus = self._make_bus()
        prop = bus.post(
            sender="alice",
            recipients=(BROADCAST,),
            type=MessageType.RESULT_PROPOSAL,
            content="draft result",
            proposal_id="prop-1",
        )
        self.assertEqual(prop.type, MessageType.RESULT_PROPOSAL)
        # 브로드캐스트 → 발신자 제외 전원.
        self.assertEqual(bus.pending_count("bob"), 1)
        self.assertEqual(bus.pending_count("carol"), 1)
        self.assertEqual(bus.pending_count("alice"), 0)

        vote = bus.post(
            sender="bob",
            recipients=(BROADCAST,),
            type=MessageType.VOTE,
            content="looks good",
            vote=VoteDecision.APPROVE,
            proposal_id="prop-1",
        )
        self.assertEqual(vote.type, MessageType.VOTE)
        self.assertEqual(vote.vote, VoteDecision.APPROVE)
        # bob의 투표는 alice/carol에게만.
        self.assertEqual(bus.pending_count("alice"), 1)
        self.assertEqual(bus.pending_count("carol"), 2)
        self.assertEqual(bus.pending_count("bob"), 1)

    async def test_non_broadcast_result_proposal_rejected_by_contract(self) -> None:
        bus = self._make_bus()
        # RESULT_PROPOSAL의 브로드캐스트 강제는 계약이 검증한다(버스는 재구현하지 않음).
        with self.assertRaises(ContractError):
            bus.post(
                sender="alice",
                recipients=("bob",),
                type=MessageType.RESULT_PROPOSAL,
                content="draft",
                proposal_id="prop-1",
            )
        self.assertEqual(bus.total_posted(), 0)


if __name__ == "__main__":
    unittest.main()
