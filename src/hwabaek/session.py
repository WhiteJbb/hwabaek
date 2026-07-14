"""SessionManager — 세션 수명주기·상태 전환·종료 정책의 단일 조정자.

Plan 코어 의미론 §3~§7을 구현한다:
- **종료 직렬화 (D-021)**: 모든 상태 전환은 이 클래스의 동기 메서드에서만 일어난다.
  단일 asyncio 이벤트 루프에서 동기 블록은 원자적이므로, 종료는 "최초로 _finalize에
  도달한 사유"로 한 번만 확정된다 — 이 클래스가 곧 단일 coordinator다. 종료 후
  도착한 명령은 ToolError로 거부되고 감사 로그(이벤트 아님)에 남는다.
- **타이머 2종 단일 감시 (D-019)**: idle_timeout(running 전용)과
  voting_timeout(voting 전용)을 하나의 감시 태스크가 관리한다.
- **판정-전환 분리 (D-021)**: ConsensusEngine이 판정을 반환하면 여기서 전환한다.
- **미승인 초안 보존 (D-025)**: voting까지 갔지만 확정 없이 실패하면 마지막 제안을
  draft_result로 남긴다. no_quorum의 fail_detail에는 미투표/기권자를 기록한다.

동시 세션 1개 규칙(D-013)의 강제 지점은 세션을 생성하는 상위 계층(M3 서버의
세션 레지스트리)이다 — SessionManager는 세션 1개의 조정자이며 전역 상태를
가지지 않는다(싱글턴 금지 원칙).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

from hwabaek.agent import AgentLoop, SessionCommands, ToolError
from hwabaek.bus import MessageBus
from hwabaek.consensus import ConsensusEngine, ConsensusError, ConsensusState
from hwabaek.contracts import (
    BROADCAST,
    AgentCapability,
    AgentSpec,
    AgentState,
    ContractError,
    Event,
    FailReason,
    Message,
    MessageType,
    ProposalOutcome,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    Usage,
    VoteDecision,
    allowed_commands,
    make_agent_state_event,
    make_message_event,
    make_result_event,
    make_session_status_event,
    make_usage_event,
    make_vote_status_event,
)
from hwabaek.llm.base import LLMClient, LLMError
from hwabaek.store.base import Store


class SessionManager:
    """태스크 1건을 팀으로 실행한다. run()이 종료 상태의 Session을 반환한다."""

    def __init__(
        self,
        team: TeamConfig,
        task: str,
        *,
        llm_factory: Callable[[AgentSpec], LLMClient],
        clock: Callable[[], str],
        id_factory: Callable[[], str],
        on_event: Callable[[Event], None] | None = None,
        store: Store | None = None,
    ) -> None:
        """llm_factory는 AgentSpec을 받아 그 에이전트의 LLMClient를 반환한다.

        store를 주입하면 세션·메시지·제안·투표·이벤트가 write-behind로 영속화된다
        (D-017) — 단일 라이터 태스크가 순서를 보존하며, 저장 실패는 세션을 죽이지
        않고 로그로만 남긴다(관측 손실 < 세션 손실).
        """
        self._team = team
        self._task = task
        self._llm_factory = llm_factory
        self._clock = clock
        self._id_factory = id_factory
        self._on_event = on_event

        self._session = Session(
            id=id_factory(),
            task=task,
            team_name=team.name,
            created_at=clock(),
        )
        agent_names = tuple(spec.name for spec in team.agents)
        self._bus = MessageBus(
            self._session.id,
            agent_names,
            clock=clock,
            id_factory=id_factory,
            on_message=self._on_bus_message,
        )
        self._consensus = ConsensusEngine(
            self._session.id,
            team.termination.approval,
            clock=clock,
            id_factory=id_factory,
        )
        self._agent_states: dict[str, AgentState] = {
            name: AgentState.IDLE for name in agent_names
        }
        self._specs: dict[str, AgentSpec] = {spec.name: spec for spec in team.agents}
        self._per_agent_usage: dict[str, Usage] = {}
        self._event_seq = 0
        self._done = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._vote_deadline: float | None = None
        self._last_activity: float = 0.0
        self._rejected_commands: list[str] = []  # 종료 후 거부된 명령의 감사 기록
        self._store = store
        self._write_queue: asyncio.Queue[Callable[[], Coroutine]] | None = None

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    @property
    def rejected_commands(self) -> tuple[str, ...]:
        return tuple(self._rejected_commands)

    async def run(self) -> Session:
        """에이전트 기동 → 종료 조건 도달까지 조정 → 종료 상태 Session 반환."""
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        writer: asyncio.Task | None = None
        if self._store is not None:
            self._write_queue = asyncio.Queue()
            writer = asyncio.create_task(self._write_behind(), name="store-writer")
            self._persist_team_snapshot()
            self._persist_session()
        self._emit_session_status()

        for spec in self._team.agents:
            agent = AgentLoop(
                name=spec.name,
                system_prompt=spec.system_prompt,
                model=self._team.model_for(spec.name),
                task=self._task,
                llm=self._llm_factory(spec),
                bus=self._bus,
                commands=_Commands(self),
                hooks=_Hooks(self),
                max_turns=spec.max_turns,
            )
            self._tasks.append(asyncio.create_task(agent.run(), name=f"agent:{spec.name}"))
        watcher = asyncio.create_task(self._watch_timers(), name="watcher")
        self._tasks.append(watcher)

        try:
            await self._done.wait()
        finally:
            # 종료 확정 이후 추가 LLM 호출·메시지가 없도록 전 태스크를 취소한다
            # (취소 후 추가 API 호출 금지 원칙).
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            # 보류 중인 영속화 쓰기를 전부 반영한 뒤 라이터를 내린다 (flush).
            if writer is not None and self._write_queue is not None:
                await self._write_queue.join()
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
        return self._session

    def cancel(self) -> None:
        """사용자 취소 — 최우선 종료 사유 (D-021 우선순위)."""
        self._finalize(SessionStatus.CANCELLED)

    # ------------------------------------------------------------------
    # 영속화 (write-behind, D-017)
    # ------------------------------------------------------------------

    async def _write_behind(self) -> None:
        """단일 라이터 — 큐 순서대로 store에 반영한다. 실패는 로그만(세션 보호)."""
        assert self._write_queue is not None
        while True:
            factory = await self._write_queue.get()
            try:
                await factory()
            except Exception:
                logger.exception("store write failed (session continues)")
            finally:
                self._write_queue.task_done()

    def _enqueue_write(self, factory: Callable[[], Coroutine]) -> None:
        if self._write_queue is not None:
            self._write_queue.put_nowait(factory)

    def _persist_session(self) -> None:
        if self._store is not None:
            session = self._session  # 현재 스냅샷을 캡처 (이후 변이와 무관)
            self._enqueue_write(lambda: self._store.save_session(session))

    def _persist_team_snapshot(self) -> None:
        if self._store is not None:
            self._enqueue_write(
                lambda: self._store.save_team_snapshot(self._session.id, self._team)
            )

    def _persist_proposal(self, proposal: ResultProposal) -> None:
        if self._store is not None:
            self._enqueue_write(lambda: self._store.save_proposal(proposal))

    # ------------------------------------------------------------------
    # 명령 처리 (에이전트 → 세션) — 상태별 허용 규칙 §17
    # ------------------------------------------------------------------

    def _guard(self, command: str, sender: str) -> None:
        """상태별 허용 규칙(§17) + 에이전트별 권한(D-027)의 이중 검증."""
        allowed = allowed_commands(self._session.status)
        if command not in allowed:
            note = f"{command} rejected: session is {self._session.status.value}"
            if self._session.is_terminal:
                self._rejected_commands.append(f"{sender}: {note}")
            raise ToolError(note)
        spec = self._specs.get(sender)
        if spec is not None and command not in {
            capability.value for capability in spec.capabilities
        }:
            raise ToolError(
                f"{command} rejected: agent {sender!r} does not have this capability"
            )

    def send_message(self, sender: str, recipients: list[str], content: str) -> str:
        self._guard("send_message", sender)
        try:
            message = self._bus.post(
                sender=sender,
                recipients=tuple(recipients),
                type=MessageType.CHAT,
                content=content,
            )
        except ContractError as error:
            raise ToolError(str(error)) from error
        return f"delivered (message {message.id})"

    def submit_result(self, sender: str, content: str) -> str:
        self._guard("submit_result", sender)
        # 심의자 스냅샷 자격 = 생존 ∧ vote_result 권한 (D-018/D-027) — 투표할 수
        # 없는 에이전트를 심의자로 넣으면 unanimous가 항상 no_quorum이 된다.
        alive = frozenset(
            name for name, state in self._agent_states.items()
            if state is not AgentState.DEAD
            and AgentCapability.VOTE_RESULT in self._specs[name].capabilities
        )
        try:
            state = self._consensus.open_proposal(sender, content, alive)
        except ConsensusError as error:
            raise ToolError(str(error)) from error
        self._persist_proposal(state.proposal)
        superseded = self._consensus.last_superseded
        if superseded is not None:
            self._persist_proposal(superseded)
        self._bus.post(
            sender=sender,
            recipients=(BROADCAST,),
            type=MessageType.RESULT_PROPOSAL,
            content=content,
            proposal_id=state.proposal.id,
        )
        self._transition(SessionStatus.VOTING)
        self._vote_deadline = (
            asyncio.get_running_loop().time()
            + self._team.termination.approval.voting_timeout
        )
        self._emit_vote_status(state)
        # 제안 시점의 즉시 판정 처리 — first 모드는 APPROVED, 심의자 0명(동료 전원
        # 사망)은 NO_QUORUM이 곧바로 나온다. 대기 없이 일원화된 경로로 반영한다.
        self._apply_outcome(state)
        if state.outcome is ProposalOutcome.APPROVED:
            return f"proposal {state.proposal.id} approved immediately"
        if state.outcome is ProposalOutcome.NO_QUORUM:
            return f"proposal {state.proposal.id} failed: no eligible voters"
        return (
            f"proposal {state.proposal.id} (version {state.proposal.version}) "
            "submitted; teammates are voting"
        )

    def vote_result(
        self, sender: str, proposal_id: str, decision: str, reason: str
    ) -> str:
        self._guard("vote_result", sender)
        try:
            vote_decision = VoteDecision(decision)
        except ValueError:
            raise ToolError(
                f"invalid decision {decision!r}: use approve or reject"
            ) from None
        try:
            if proposal_id:
                result = self._consensus.register_vote_for(
                    proposal_id, sender, vote_decision, reason
                )
            else:
                # proposal_id 생략 = 활성 제안에 투표 (LLM의 id 오기입에 견고).
                result = self._consensus.register_vote(sender, vote_decision, reason)
        except ContractError as error:
            raise ToolError(str(error)) from error
        if result is None:
            return "vote ignored: stale or unknown proposal"
        vote, state = result
        if self._store is not None:
            self._enqueue_write(lambda: self._store.append_vote(vote))
        self._bus.post(
            sender=sender,
            recipients=(BROADCAST,),
            type=MessageType.VOTE,
            content=reason,
            vote=vote_decision,
            proposal_id=vote.proposal_id,
        )
        self._emit_vote_status(state)
        self._apply_outcome(state)
        return f"vote recorded ({vote_decision.value})"

    # ------------------------------------------------------------------
    # 판정 반영 / 종료 (단일 코디네이터 — D-021)
    # ------------------------------------------------------------------

    def _apply_outcome(self, state: ConsensusState) -> None:
        if state.outcome is ProposalOutcome.PENDING:
            return
        if self._session.is_terminal:
            return
        if state.outcome is ProposalOutcome.APPROVED:
            self._complete(state)
        elif state.outcome is ProposalOutcome.REJECTED:
            rejected = self._consensus.resolve(ProposalOutcome.REJECTED)
            self._persist_proposal(rejected)
            self._vote_deadline = None
            self._transition(SessionStatus.RUNNING)
        else:  # NO_QUORUM
            proposal = self._consensus.resolve(ProposalOutcome.NO_QUORUM)
            self._persist_proposal(proposal)
            detail = (
                "no quorum: pending="
                + ",".join(sorted(state.tally.pending))
                + " abstained="
                + ",".join(sorted(state.tally.abstained))
            )
            self._finalize(
                SessionStatus.FAILED,
                fail_reason=FailReason.NO_QUORUM,
                fail_detail=detail,
                draft=proposal,
            )

    def _complete(self, state: ConsensusState) -> None:
        proposal = self._consensus.resolve(ProposalOutcome.APPROVED)
        self._persist_proposal(proposal)
        self._finalize(
            SessionStatus.COMPLETED,
            result=proposal.content,
            submitted_by=proposal.proposer,
        )

    def _transition(self, new_status: SessionStatus) -> None:
        self._session = self._session.with_status(new_status)
        self._persist_session()
        self._emit_session_status()

    def _finalize(
        self,
        status: SessionStatus,
        *,
        fail_reason: FailReason | None = None,
        fail_detail: str | None = None,
        result: str | None = None,
        submitted_by: str | None = None,
        draft: ResultProposal | None = None,
    ) -> None:
        """종료 1회 확정 — 이미 종료면 무시(최초 유효 사유 승리, D-021)."""
        if self._session.is_terminal:
            return
        # 실패인데 심의 중이던 제안이 있으면 미승인 초안으로 보존 (D-025).
        if draft is None and status is SessionStatus.FAILED:
            active = self._consensus.active
            if active is not None:
                draft = active.proposal
        self._session = self._session.with_status(
            status,
            fail_reason=fail_reason,
            fail_detail=fail_detail,
            result=result,
            submitted_by=submitted_by,
            draft_result=draft.content if draft else None,
            draft_proposer=draft.proposer if draft else None,
            finished_at=self._clock(),
        )
        self._persist_session()
        self._emit_session_status()
        if status is SessionStatus.COMPLETED:
            self._emit(make_result_event(
                self._id_factory(), self._next_seq(), self._session, self._clock()
            ))
        self._done.set()

    # ------------------------------------------------------------------
    # 감시 태스크 — 타이머 2종의 단일 주체 (D-019)
    # ------------------------------------------------------------------

    async def _watch_timers(self) -> None:
        loop = asyncio.get_running_loop()
        idle_timeout = self._team.termination.idle_timeout
        tick = max(min(idle_timeout, self._team.termination.approval.voting_timeout) / 10, 0.01)
        while not self._session.is_terminal:
            await asyncio.sleep(tick)
            now = loop.time()
            if self._session.status is SessionStatus.VOTING:
                if self._vote_deadline is not None and now >= self._vote_deadline:
                    self._vote_deadline = None
                    state = self._consensus.expire_pending()
                    if state is not None:
                        self._emit_vote_status(state)
                        self._apply_outcome(state)
            elif self._session.status is SessionStatus.RUNNING:
                if self._all_idle() and (now - self._last_activity) >= idle_timeout:
                    self._finalize(
                        SessionStatus.FAILED, fail_reason=FailReason.IDLE
                    )

    def _all_idle(self) -> bool:
        for name, state in self._agent_states.items():
            if state is AgentState.THINKING:
                return False
            if state is not AgentState.DEAD and self._bus.pending_count(name) > 0:
                return False
        return True

    # ------------------------------------------------------------------
    # 훅 (버스·에이전트 → 세션)
    # ------------------------------------------------------------------

    def _on_bus_message(self, message: Message) -> None:
        self._touch()
        if self._store is not None:
            self._enqueue_write(lambda: self._store.append_message(message))
        self._emit(make_message_event(self._id_factory(), self._next_seq(), message))
        if self._bus.total_posted() > self._team.termination.max_messages:
            self._finalize(SessionStatus.FAILED, fail_reason=FailReason.MESSAGES)

    def _on_agent_state(
        self, agent: str, state: AgentState, detail: str | None = None
    ) -> None:
        if self._session.is_terminal:
            return
        # DEAD는 에이전트의 종결 상태 — 이후 보고(IDLE 등)가 덮어쓰지 못하게 한다.
        # 덮어쓰면 생존자 수가 부풀어 agent_error 판정이 누락된다 (이중 방어;
        # 1차 방어는 AgentLoop가 fatal 후 루프를 끝내는 것).
        if (
            self._agent_states.get(agent) is AgentState.DEAD
            and state is not AgentState.DEAD
        ):
            return
        if self._agent_states.get(agent) is state and detail is None:
            return
        self._agent_states[agent] = state
        if state is AgentState.THINKING:
            self._touch()
        self._emit(make_agent_state_event(
            self._id_factory(), self._next_seq(), self._session.id,
            agent, state, self._clock(), detail,
        ))

    def _on_agent_usage(self, agent: str, usage: Usage) -> None:
        if self._session.is_terminal:
            return
        self._session = self._session.with_usage(usage)
        self._persist_session()
        self._per_agent_usage[agent] = (
            self._per_agent_usage.get(agent, Usage()) + usage
        )
        self._emit(make_usage_event(
            self._id_factory(), self._next_seq(), self._session.id,
            self._session.usage, self._team.termination.token_budget,
            self._clock(), per_agent=dict(self._per_agent_usage),
        ))
        if self._session.usage.total_tokens > self._team.termination.token_budget:
            self._finalize(SessionStatus.FAILED, fail_reason=FailReason.BUDGET)

    def _on_agent_fatal(self, agent: str, error: LLMError) -> None:
        """재시도 소진 오류 — dead 처리, 생존 부족 시 세션 실패 (귀책 기록)."""
        detail = (
            f"dead: {error.category.value} (blame={error.blame.value}, "
            f"retryable={error.retryable})"
        )
        self._on_agent_state(agent, AgentState.DEAD, detail=detail)
        alive = [
            name for name, state in self._agent_states.items()
            if state is not AgentState.DEAD
        ]
        if len(alive) <= 1 and not self._session.is_terminal:
            self._finalize(
                SessionStatus.FAILED,
                fail_reason=FailReason.AGENT_ERROR,
                fail_detail=f"agent {agent} {detail}; survivors: {','.join(alive) or 'none'}",
            )

    # ------------------------------------------------------------------
    # 이벤트 발행
    # ------------------------------------------------------------------

    def _touch(self) -> None:
        try:
            self._last_activity = asyncio.get_running_loop().time()
        except RuntimeError:  # 루프 밖(테스트의 동기 호출) — idle 판정에만 쓰인다
            pass

    def _next_seq(self) -> int:
        seq = self._event_seq
        self._event_seq += 1
        return seq

    def _emit(self, event: Event) -> None:
        if self._store is not None:
            self._enqueue_write(lambda: self._store.append_event(event))
        if self._on_event is not None:
            self._on_event(event)

    def _emit_session_status(self) -> None:
        self._emit(make_session_status_event(
            self._id_factory(), self._next_seq(), self._session, self._clock()
        ))

    def _emit_vote_status(self, state: ConsensusState) -> None:
        self._emit(make_vote_status_event(
            self._id_factory(), self._next_seq(), self._session.id,
            state.proposal, state.tally, self._clock(),
        ))


class _Commands(SessionCommands):
    """AgentLoop에 주입되는 명령 어댑터 — SessionManager로 위임."""

    def __init__(self, manager: SessionManager) -> None:
        self._m = manager

    def send_message(self, sender: str, recipients: list[str], content: str) -> str:
        return self._m.send_message(sender, recipients, content)

    def submit_result(self, sender: str, content: str) -> str:
        return self._m.submit_result(sender, content)

    def vote_result(
        self, sender: str, proposal_id: str, decision: str, reason: str
    ) -> str:
        return self._m.vote_result(sender, proposal_id, decision, reason)


class _Hooks:
    """AgentLoop에 주입되는 상태·사용량 훅 어댑터."""

    def __init__(self, manager: SessionManager) -> None:
        self._m = manager

    def on_state(self, agent: str, state: AgentState, detail: str | None = None) -> None:
        self._m._on_agent_state(agent, state, detail)

    def on_usage(self, agent: str, usage: Usage) -> None:
        self._m._on_agent_usage(agent, usage)

    def on_fatal_error(self, agent: str, error: LLMError) -> None:
        self._m._on_agent_fatal(agent, error)
