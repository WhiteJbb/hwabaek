"""세션 러너·레지스트리와 SSE 스트림 — M3 서버의 팬아웃/수명 관리 코어.

설계 핵심 (SSE 재전송의 무결성):
- SessionManager의 on_event 훅은 **동기**로, write-behind 영속화보다 먼저 호출된다
  (session.py `_emit` 참조). 따라서 "구독 후 store 조회"만으로는 방금 발행됐지만
  아직 저장 전인 이벤트가 누락되는 틈이 생긴다. 그래서 SessionRunner 자체가
  on_event 싱크가 되어 (a) 메모리 이벤트 로그를 쌓고 (b) 구독자 큐로 팬아웃한다 —
  둘 다 동기. 활성 세션의 재전송은 이 메모리 로그에서 나오므로 store 지연과
  무관하게 틈이 없다. 종료/과거 세션은 store에서 재전송한다.
- 모든 상태 변이(구독/해지/팬아웃/러너 교체)는 단일 이벤트 루프에서 동기 블록으로만
  일어난다 — await 사이에 끼어들지 않으므로 세트/리스트 접근에 락이 필요 없다.
  동시 세션 1개 검사와 생성만 asyncio.Lock으로 원자화한다 (D-013).

사용자 노출 문자열(예외 메시지)은 영어 ASCII, 주석·독스트링은 한국어.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from hwabaek.config import ConfigError, list_team_configs
from hwabaek.contracts import (
    AgentSpec,
    Event,
    FailReason,
    Session,
    SessionStatus,
    TeamConfig,
    make_session_status_event,
)
from hwabaek.session import SessionManager

if TYPE_CHECKING:
    from hwabaek.llm.base import LLMClient
    from hwabaek.store.base import Store

logger = logging.getLogger(__name__)
DEFAULT_SUBSCRIBER_QUEUE_SIZE = 512

# (team, task) -> per-agent LLM 팩토리. 실/가짜/스크립트 주입 지점 — 테스트가 실
# OpenAI 클라이언트를 만들지 않도록 서버 조립이 이 주입을 허용한다.
LLMFactoryProvider = Callable[[TeamConfig, str], Callable[["AgentSpec"], "LLMClient"]]


def team_summary(team: TeamConfig) -> dict:
    """대시보드에 노출할 팀 설정 요약. 시스템 프롬프트는 포함하지 않는다."""
    return {
        "name": team.name,
        "description": team.description,
        "default_model": team.default_model,
        "agents": [
            {
                "name": agent.name,
                "role": agent.role,
                "model": agent.model or team.default_model,
                "max_turns": agent.max_turns,
                "capabilities": sorted(c.value for c in agent.capabilities),
            }
            for agent in team.agents
        ],
        "termination": {
            "max_messages": team.termination.max_messages,
            "token_budget": team.termination.token_budget,
            "processed_token_limit": team.termination.effective_processed_token_limit,
            "synthesis_at": team.termination.effective_synthesis_at,
            "proposal_by": team.termination.effective_proposal_by,
            "call_reserve_tokens": team.termination.effective_call_reserve_tokens,
            "max_proposals": team.termination.effective_max_proposals,
            "idle_timeout": team.termination.idle_timeout,
            "approval": {
                "mode": team.termination.approval.mode.value,
                "voting_timeout": team.termination.approval.voting_timeout,
                "minimum_votes": team.termination.approval.minimum_votes,
            },
        },
    }


class ServerError(Exception):
    """M3 서버 계층의 도메인 오류 베이스."""


class ConcurrentSessionError(ServerError):
    """동시 세션 1개 규칙 위반 — 이미 실행 중인 세션이 있다 (D-013)."""

    def __init__(self, active_session_id: str) -> None:
        self.active_session_id = active_session_id
        super().__init__(
            "a session is already running or voting "
            f"(active session {active_session_id}); "
            "only one session may be active at a time"
        )


class UnknownTeamError(ServerError):
    """요청한 팀 이름이 configs에 없다."""

    def __init__(self, team_name: str) -> None:
        self.team_name = team_name
        super().__init__(f"unknown team {team_name!r}")


class SessionNotFoundError(ServerError):
    """해당 id의 세션이 없다."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"session {session_id!r} not found")


class SessionNotActiveError(ServerError):
    """종료된 세션에는 취소가 허용되지 않는다."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"session {session_id!r} is not active and cannot be cancelled"
        )


def format_sse(event: Event) -> str:
    """Event를 SSE 와이어 프레임으로 직렬화한다 (EventContract §5 확정).

    `id:`에 세션 내 sequence를 실어 Last-Event-ID 재구독의 기준으로 삼는다.
    `event:`는 EventType, `data:`는 Event.to_dict()의 JSON 1줄이다.
    """
    data = json.dumps(event.to_dict(), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.type.value}\ndata: {data}\n\n"


class SessionRunner:
    """세션 1개의 백그라운드 태스크 + 라이브 이벤트 팬아웃을 감싼다.

    on_event(동기)로 들어온 이벤트를 메모리 로그에 append하고 모든 구독자 큐에
    put_nowait로 팬아웃한다. 러너 종료 시 각 구독자 큐에 None 센티널을 넣어
    스트림 종료를 알린다.
    """

    def __init__(
        self, subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE
    ) -> None:
        if subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        self._manager: SessionManager | None = None
        self._events: list[Event] = []
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._subscriber_queue_size = subscriber_queue_size
        self._done = asyncio.Event()
        self.task: asyncio.Task | None = None

    def attach(self, manager: SessionManager) -> None:
        """생성된 SessionManager를 연결한다 (on_event는 이미 self.on_event로 주입됨)."""
        self._manager = manager

    @property
    def manager(self) -> SessionManager:
        assert self._manager is not None, "runner has no manager attached"
        return self._manager

    @property
    def session_id(self) -> str:
        return self.manager.session.id

    @property
    def session(self) -> Session:
        return self.manager.session

    @property
    def done(self) -> bool:
        return self._done.is_set()

    # ---- on_event 싱크 (SessionManager가 동기로 호출) ----

    def on_event(self, event: Event) -> None:
        self._events.append(event)
        for queue in tuple(self._subscribers):
            if queue.qsize() >= self._subscriber_queue_size:
                self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
                logger.warning("disconnecting slow SSE subscriber")
                continue
            queue.put_nowait(event)

    def snapshot(self) -> list[Event]:
        """현재까지의 메모리 이벤트 로그 사본 (재전송용)."""
        return list(self._events)

    def subscribe(self) -> asyncio.Queue[Event | None]:
        # 논리 상한 + 종료 센티널 1칸. 느린 소비자가 상한을 채우면 on_event가
        # 연결을 닫고 클라이언트는 마지막 SSE id로 재접속한다.
        queue: asyncio.Queue[Event | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size + 1
        )
        self._subscribers.add(queue)
        # 이미 종료된 러너에 뒤늦게 구독하면 _run의 센티널 발송을 놓친다 —
        # 스트림이 영원히 대기하지 않도록 이 구독자에게 직접 센티널을 넣는다.
        # (구독은 await 없이 원자적이므로, 종료 경합에서 센티널은 정확히 1개다:
        #  done 관측 전 구독이면 _run이, done 관측 후 구독이면 여기서 넣는다.)
        if self._done.is_set():
            queue.put_nowait(None)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        self._subscribers.discard(queue)

    # ---- 수명주기 ----

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"session:{self.session_id}")

    async def _run(self) -> None:
        try:
            await self.manager.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 세션 러너의 예외가 서버를 죽이지 않게 격리한다
            error_type = type(exc).__name__
            if not error_type.isascii():
                error_type = "non_ascii_exception"
            logger.error("session runner crashed (%s)", error_type)
        finally:
            self._done.set()
            # 남아 있는 구독자에게 스트림 종료를 알린다.
            for queue in self._subscribers:
                queue.put_nowait(None)

    async def wait_done(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._done.wait()
            return
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except asyncio.TimeoutError:
            pass


class SessionRegistry:
    """서버 전역 세션 조정자 — 동시 세션 1개 강제(D-013)와 러너 수명 관리.

    전역 싱글턴이 아니라 앱 인스턴스(app.state)에 매인다 — 테스트가 독립 앱을
    각자 조립할 수 있다.
    """

    def __init__(
        self,
        *,
        store: "Store | None",
        teams_dir: str | Path,
        llm_factory_provider: LLMFactoryProvider,
        clock: Callable[[], str],
        id_factory_provider: Callable[[], Callable[[], str]],
        team_override: TeamConfig | None = None,
        default_team: str = "default",
    ) -> None:
        self._store = store
        self._teams_dir = Path(teams_dir)
        self._llm_factory_provider = llm_factory_provider
        self._clock = clock
        self._id_factory_provider = id_factory_provider
        self._team_override = team_override
        self._default_team = default_team
        self._lock = asyncio.Lock()
        self._active: SessionRunner | None = None

    @property
    def store(self) -> "Store | None":
        return self._store

    @property
    def default_team_name(self) -> str:
        """요청에서 team을 생략했을 때 선택되는 팀 이름."""
        if self._team_override is not None:
            return self._team_override.name
        return self._default_team

    # ---- 팀 조회 ----

    async def list_teams(self) -> list[TeamConfig]:
        if self._team_override is not None:
            return [self._team_override]
        # YAML 파일 I/O는 요청 경로를 블로킹하지 않도록 스레드로 위임한다.
        teams = await asyncio.to_thread(list_team_configs, self._teams_dir)
        # HTML select는 selected 항목이 없으면 첫 옵션을 암묵 선택한다. 잘못된 서버
        # 기본값을 비용이 큰 다른 팀으로 조용히 대체하지 않도록 목록 경계에서 막는다.
        if not any(team.name == self._default_team for team in teams):
            raise UnknownTeamError(self._default_team)
        return teams

    async def _resolve_team(self, team_name: str | None) -> TeamConfig:
        if self._team_override is not None:
            if team_name is not None and team_name != self._team_override.name:
                raise UnknownTeamError(team_name)
            return self._team_override
        wanted = team_name or self._default_team
        teams = await asyncio.to_thread(list_team_configs, self._teams_dir)
        for team in teams:
            if team.name == wanted:
                return team
        raise UnknownTeamError(wanted)

    # ---- 세션 생성 (동시 세션 1개 검사 + 생성 원자화) ----

    async def create_session(
        self, task: str, team_name: str | None = None
    ) -> Session:
        # 팀 로드·LLM 팩토리 조립은 락 밖에서 — 락 구간은 검사+생성만 원자화한다.
        team = await self._resolve_team(team_name)
        llm_factory = self._llm_factory_provider(team, task)
        async with self._lock:
            if self._active is not None and not self._active.done:
                raise ConcurrentSessionError(self._active.session_id)
            runner = SessionRunner()
            manager = SessionManager(
                team,
                task,
                llm_factory=llm_factory,
                clock=self._clock,
                id_factory=self._id_factory_provider(),
                on_event=runner.on_event,
                store=self._store,
            )
            runner.attach(manager)
            self._active = runner
            runner.start()
            return manager.session

    # ---- 조회 ----

    def get_runner(self, session_id: str) -> SessionRunner | None:
        active = self._active
        if active is not None and active.session_id == session_id:
            return active
        return None

    def get_runner_for_shutdown(self) -> SessionRunner | None:
        """종료 훅에서 활성 러너를 정리하기 위한 접근자."""
        return self._active

    async def current_session(self, session_id: str) -> Session | None:
        """활성 러너의 라이브 세션을 우선 반환 — 없으면 store 조회."""
        runner = self.get_runner(session_id)
        if runner is not None:
            return runner.session
        if self._store is not None:
            return await self._store.get_session(session_id)
        return None

    async def list_sessions(self, *, limit: int = 50) -> list[Session]:
        if self._store is not None:
            return await self._store.list_sessions(limit=limit)
        # store 없는 데모 모드 — 활성 세션만 노출한다.
        if self._active is not None:
            return [self._active.session]
        return []

    async def get_session_detail(self, session_id: str) -> dict:
        session = await self.current_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        runner = self.get_runner(session_id)
        if runner is not None and session.finished_at is not None and not runner.done:
            # 종료 상태는 write-behind 저장이 모두 끝난 뒤 반환해 혼합 스냅샷을 피한다.
            await runner.wait_done()
            session = runner.session
        messages: list = []
        proposals: list = []
        votes: list = []
        if self._store is not None:
            messages = await self._store.list_messages(session_id)
            proposals = await self._store.list_proposals(session_id)
            votes = await self._store.list_votes(session_id)
        team: TeamConfig | None = None
        if self._store is not None:
            team = await self._store.get_team_snapshot(session_id)
        if team is None:
            try:
                team = await self._resolve_team(session.team_name)
            except (ConfigError, UnknownTeamError):
                # 과거 세션의 팀 설정 파일이 삭제됐고 snapshot도 없을 수 있다.
                team = None
        return {
            "session": session.to_dict(),
            "team": team_summary(team) if team is not None else None,
            "messages": [m.to_dict() for m in messages],
            "proposals": [p.to_dict() for p in proposals],
            "votes": [v.to_dict() for v in votes],
        }

    # ---- 취소 ----

    async def cancel_session(self, session_id: str) -> Session:
        runner = self.get_runner(session_id)
        if runner is not None and not runner.done:
            runner.manager.cancel()  # 동기 finalize — 세션은 즉시 cancelled
            # 러너 종료(태스크 취소·store flush)까지 잠시 기다린 뒤 최신 상태를 반환.
            await runner.wait_done(timeout=5.0)
            return runner.session
        # 활성이 아니면 존재 여부를 확인해 404/409를 구분한다.
        session = await self.current_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        raise SessionNotActiveError(session_id)

    # ---- SSE 스트림 ----

    async def event_stream(
        self, session_id: str, after_sequence: int
    ) -> AsyncIterator[str]:
        runner = self.get_runner(session_id)
        if runner is not None:
            async for chunk in self._stream_from_runner(runner, after_sequence):
                yield chunk
            return
        # 과거(종료) 세션 — store에서 재전송 후 종료.
        if self._store is not None:
            for event in await self._store.list_events(
                session_id, after_sequence=after_sequence
            ):
                yield format_sse(event)

    async def _stream_from_runner(
        self, runner: SessionRunner, after_sequence: int
    ) -> AsyncIterator[str]:
        # 먼저 구독해야 구독 시점 이후 이벤트가 큐에 담긴다 — 틈 방지.
        queue = runner.subscribe()
        last = after_sequence
        try:
            # 구독 시점까지의 메모리 로그를 재전송 (sequence 초과분만). yield는
            # 제너레이터를 중단시키는 await 지점이라, 재전송 중 러너가 새 이벤트를
            # 내거나 종료할 수 있다 — 그 이벤트는 큐에만 담긴다.
            for event in runner.snapshot():
                if event.sequence > last:
                    yield format_sse(event)
                    last = event.sequence
            # 라이브 스트림 — 센티널(None)까지 큐를 비운다. 재전송 중/후에 도착한
            # 이벤트도 여기서 전달된다(종료 세션이어도 큐에 센티널이 보장됨).
            # snapshot과 겹치는 이벤트는 sequence 비교로 중복 제거된다.
            while True:
                event = await queue.get()
                if event is None:
                    break
                if event.sequence > last:
                    yield format_sse(event)
                    last = event.sequence
        finally:
            runner.unsubscribe(queue)


async def mark_interrupted_sessions(
    store: "Store", clock: Callable[[], str]
) -> int:
    """서버 시작 시 이전 running/voting 세션을 failed(interrupted)로 처리한다 (D-021).

    처리한 세션 수를 반환한다. store 조회/저장만 사용한다.
    """
    count = 0
    for status in (SessionStatus.RUNNING, SessionStatus.VOTING):
        for session in await store.list_sessions_by_status(status):
            created_at = clock()
            interrupted = session.with_status(
                SessionStatus.FAILED,
                fail_reason=FailReason.INTERRUPTED,
                fail_detail="server restarted while the session was active",
                finished_at=created_at,
            )
            events = await store.list_events(session.id)
            next_sequence = max((event.sequence for event in events), default=-1) + 1
            event = make_session_status_event(
                uuid4().hex,
                next_sequence,
                interrupted,
                created_at,
            )
            await store.save_session(interrupted)
            await store.append_event(event)
            count += 1
    return count
