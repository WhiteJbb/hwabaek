"""메시지 버스 — 에이전트별 인박스와 배달 규칙 (Plan 코어 의미론 §1, D-023).

이 파일은 M2a 인터페이스 확정본이다. 구현 규칙:

- **id/created_at/sequence 부여는 버스의 책임** — 계약(contracts)은 시계를 읽지
  않으므로 clock/id_factory를 주입받는다. sequence는 세션 단위 단조 증가
  (메시지만 카운트 — Event.sequence와 독립).
- **배달**: 특정 수신자는 해당 인박스에만, 브로드캐스트(`*`)는 발신자를 제외한
  전원의 인박스에 배달. 동일 message id의 중복 배달은 무시(멱등).
- **원자적 drain**: drain()은 호출 시점까지 쌓인 메시지 전부를 한 번에 비워
  sequence 오름차순으로 반환하고, 그 이후 도착분은 다음 배치로 넘긴다.
  asyncio 단일 이벤트 루프에서 await 없이 완결되어야 원자성이 보장된다.
- **관측 훅**: 배달된 모든 메시지는 on_message 콜백으로 통지된다 —
  SessionManager가 message 이벤트 발행·영속화·max_messages 판정에 사용.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from hwabaek.contracts import (
    BROADCAST,
    ContractError,
    Message,
    MessageType,
    VoteDecision,
)


class MessageBus:
    """세션 1개의 메시지 버스. 에이전트 이름 목록은 생성 시 고정된다."""

    def __init__(
        self,
        session_id: str,
        agent_names: tuple[str, ...],
        *,
        clock: Callable[[], str],
        id_factory: Callable[[], str],
        on_message: Callable[[Message], None] | None = None,
    ) -> None:
        """clock은 ISO 8601 UTC 문자열을, id_factory는 유일 id를 반환해야 한다."""
        if not session_id:
            raise ContractError("session_id must be non-empty")
        # 에이전트 이름 순서를 보존한다(브로드캐스트 배달 순서 = 등록 순서).
        self._session_id = session_id
        self._agents: tuple[str, ...] = tuple(agent_names)
        self._clock = clock
        self._id_factory = id_factory
        self._on_message = on_message
        # 에이전트별 인박스와 도착 알림 이벤트. 불변식: event.is_set() ⇔ 인박스 비어있지 않음.
        self._inboxes: dict[str, list[Message]] = {name: [] for name in self._agents}
        self._events: dict[str, asyncio.Event] = {
            name: asyncio.Event() for name in self._agents
        }
        # 세션 단위 단조 증가 시퀀스 겸 발행 총수. sequence는 0부터 시작한다.
        self._posted = 0

    def post(
        self,
        *,
        sender: str,
        recipients: tuple[str, ...],
        type: MessageType,
        content: str,
        vote: VoteDecision | None = None,
        proposal_id: str | None = None,
    ) -> Message:
        """메시지를 생성(id/created_at/sequence 부여)·검증·배달하고 반환한다.

        검증은 Message 계약이 수행한다(자기송신 금지 포함). 추가로 버스는
        미등록 수신자를 ContractError로 거부한다. 배달 후 on_message 통지.
        """
        # 버스 책임: 미등록 sender/수신자 거부(계약이 모르는 소속 검증).
        if sender not in self._inboxes:
            raise ContractError(f"unknown sender: {sender!r}")
        for recipient in recipients:
            if recipient != BROADCAST and recipient not in self._inboxes:
                raise ContractError(f"unknown recipient: {recipient!r}")
        # 나머지 검증(자기송신 금지·타입별 규칙 등)은 Message 계약이 수행한다 — 재구현 금지.
        message = Message(
            id=self._id_factory(),
            session_id=self._session_id,
            sender=sender,
            recipients=recipients,
            type=type,
            content=content,
            created_at=self._clock(),
            sequence=self._posted,
            vote=vote,
            proposal_id=proposal_id,
        )
        # 여기까지 예외 없이 도달했으면 검증 통과 — 이제서야 시퀀스를 확정한다.
        # (실패한 post는 시퀀스/총수를 소비하지 않아 배치 순번에 구멍이 없다.)
        self._posted += 1
        self._deliver(message)
        # 배달 성공한 원본 메시지당 정확히 1회 통지(브로드캐스트도 1회).
        if self._on_message is not None:
            self._on_message(message)
        return message

    def _deliver(self, message: Message) -> None:
        """배달 규칙 적용: 직접 수신자는 해당 인박스에만, 브로드캐스트는 발신자 제외 전원."""
        if message.is_broadcast:
            targets: tuple[str, ...] = tuple(
                name for name in self._agents if name != message.sender
            )
        else:
            targets = message.recipients
        for target in targets:
            inbox = self._inboxes[target]
            # 동일 id 중복 배달 무시(멱등) — 중복 수신자/방어적 재배달 대비.
            if any(existing.id == message.id for existing in inbox):
                continue
            inbox.append(message)
            # 대기 중인 소비자를 깨운다. 인박스가 비지 않았음을 표시(불변식 유지).
            self._events[target].set()

    async def wait_for_messages(self, agent_name: str) -> None:
        """해당 인박스에 메시지가 생길 때까지 대기한다 (이미 있으면 즉시 반환).

        취소(asyncio.CancelledError)는 그대로 전파한다 — 세션 종료 시
        SessionManager가 대기 중인 에이전트 태스크를 취소한다.
        """
        self._require_agent(agent_name)
        # 이미 쌓인 메시지가 있으면 즉시 반환.
        if self._inboxes[agent_name]:
            return
        # 없으면 다음 post가 이벤트를 set할 때까지 대기. CancelledError는 잡지 않는다.
        await self._events[agent_name].wait()

    def drain(self, agent_name: str) -> list[Message]:
        """인박스를 원자적으로 비워 sequence 오름차순 배치로 반환한다."""
        self._require_agent(agent_name)
        inbox = self._inboxes[agent_name]
        if not inbox:
            return []
        # await 없는 동기 처리 — 단일 이벤트 루프에서 스냅샷·비움이 원자적이다.
        batch = sorted(inbox, key=lambda m: m.sequence)
        inbox.clear()
        # 인박스가 비었으므로 알림 이벤트도 내린다(불변식 유지).
        self._events[agent_name].clear()
        return batch

    def pending_count(self, agent_name: str) -> int:
        """인박스에 대기 중인 메시지 수 — idle 판정 재료."""
        self._require_agent(agent_name)
        return len(self._inboxes[agent_name])

    def total_posted(self) -> int:
        """세션에서 지금까지 발행된 메시지 총수 — max_messages 판정 재료."""
        return self._posted

    def _require_agent(self, agent_name: str) -> None:
        """미등록 에이전트 조회를 ContractError로 거부한다(이름 포함)."""
        if agent_name not in self._inboxes:
            raise ContractError(f"unknown agent: {agent_name!r}")
