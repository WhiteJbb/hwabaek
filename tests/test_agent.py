"""AgentLoop 순수 함수 검증 — merge_batch 타입별 렌더링 (M2b).

배경: 실 스모크에서 result_proposal이 일반 채팅과 동일하게 렌더링돼 심의자들이
vote_result 대신 채팅으로만 동의를 표하다 전원 기권(no_quorum) 처리됐다.
merge_batch는 제안·투표를 채팅과 구별되게 표기하고, 제안에는 즉시 투표하라는
행동 지시를 싣는다.
"""
from __future__ import annotations

import unittest

from hwabaek.agent import merge_batch
from hwabaek.contracts import Message, MessageType, VoteDecision


def _message(
    mtype: MessageType,
    *,
    sender: str = "peer",
    content: str = "hello",
    vote: VoteDecision | None = None,
    proposal_id: str | None = None,
    recipients: tuple[str, ...] = ("me",),
) -> Message:
    return Message(
        id="m-1",
        session_id="s-1",
        sender=sender,
        recipients=recipients,
        type=mtype,
        content=content,
        created_at="2026-07-14T00:00:00Z",
        sequence=0,
        vote=vote,
        proposal_id=proposal_id,
    )


class MergeBatchTest(unittest.TestCase):
    def test_chat_renders_sender_tag(self) -> None:
        merged = merge_batch([_message(MessageType.CHAT, content="hi there")])
        self.assertIn("[from: peer]", merged)
        self.assertIn("hi there", merged)

    def test_result_proposal_renders_marker_and_vote_instruction(self) -> None:
        merged = merge_batch([
            _message(
                MessageType.RESULT_PROPOSAL,
                sender="proposer",
                content="final draft",
                proposal_id="p-1",
                recipients=("*",),
            )
        ])
        self.assertIn("[result proposal from proposer]", merged)
        # 활성 제안 id를 명시한다 — 심의자가 id를 지어내 투표하는 것을 방지.
        self.assertIn("(proposal_id: p-1)", merged)
        self.assertIn("final draft", merged)
        self.assertIn("[action required]", merged)
        self.assertIn("vote_result", merged)
        self.assertIn("omit proposal_id", merged)
        # 채팅은 투표가 아님을 명시한다.
        self.assertIn("does NOT count as a vote", merged)

    def test_vote_renders_decision_and_reason(self) -> None:
        merged = merge_batch([
            _message(
                MessageType.VOTE,
                sender="voter",
                content="looks solid",
                vote=VoteDecision.APPROVE,
                proposal_id="p-1",
                recipients=("*",),
            )
        ])
        self.assertIn("[vote from voter: approve]", merged)
        self.assertIn("looks solid", merged)

    def test_batch_preserves_order_and_separators(self) -> None:
        merged = merge_batch([
            _message(MessageType.CHAT, sender="a", content="first"),
            _message(MessageType.CHAT, sender="b", content="second"),
        ])
        self.assertLess(merged.index("first"), merged.index("second"))
        self.assertIn("[from: a]", merged)
        self.assertIn("[from: b]", merged)


if __name__ == "__main__":
    unittest.main()
