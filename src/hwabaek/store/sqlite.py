"""SQLite 기반 Store 구현 (D-017, M2b).

표준 라이브러리 sqlite3만 사용한다 — aiosqlite·ORM·이벤트 소싱 프레임워크는 도입하지
않는다(D-017). 모든 async 메서드는 내부 동기 함수를 asyncio.to_thread로 위임해 요청
경로를 블로킹하지 않는다(스케줄링은 호출자 책임 — store/base.py 참조).

동시성 모델 (M2b: 단일 세션·단일 프로세스 전제):
  단일 영속 연결을 check_same_thread=False로 열고, threading.Lock으로 모든 DB 접근을
  직렬화한다. to_thread는 작업을 스레드풀의 임의 워커 스레드에서 실행하므로 (a) 연결이
  여러 스레드에서 쓰일 수 있어 check_same_thread=False가 필요하고, (b) sqlite3 연결
  객체는 스레드 동시 사용에 안전하지 않으므로 Lock으로 한 번에 한 스레드만 접근하게
  한다. 연결-per-호출 방식(매번 open/close) 대신 이 방식을 택한 이유: WAL/PRAGMA
  재설정 비용을 매 호출 반복하지 않고, 단일 세션 전제에서 직렬화의 처리량 손해가 없다.
  WAL 모드로 내구성과 (향후) 읽기 동시성 여지를 둔다.

저장 형태: 각 도메인 객체를 to_dict → json.dumps 한 JSON 컬럼 + 조회용 인덱스 컬럼.
API 키 등 비밀은 계약 객체에 존재하지 않으므로 경로·데이터 어디에도 저장되지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from hwabaek.contracts import (
    AgentCapability,
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    Event,
    EventType,
    Message,
    ResultProposal,
    Session,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
    Vote,
)

# 스키마 — 첫 연결 시 IF NOT EXISTS로 멱등 생성한다.
# JSON 직렬화 컬럼(data)에 전체 상태를 담고, 별도 컬럼은 조회/정렬 인덱스 용도다.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_snapshots (
    session_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    version    INTEGER NOT NULL,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals(session_id, version);
CREATE INDEX IF NOT EXISTS idx_votes_session ON votes(session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, sequence);
"""


# ---------------------------------------------------------------------------
# TeamConfig 직렬화 — 계약 파일(contracts.py)에 to_dict/from_dict가 없으므로 여기서
# 자체 왕복 변환을 정의한다. capabilities(AgentCapability frozenset)는 값 리스트로
# 저장하고, ApprovalPolicy/TerminationPolicy/ApprovalConfig 중첩까지 전부 보존한다.
# ---------------------------------------------------------------------------

def _team_to_dict(team: TeamConfig) -> dict:
    """TeamConfig를 JSON 직렬화 가능한 dict로 변환한다(재현성 스냅샷용)."""
    return {
        "name": team.name,
        "description": team.description,
        "default_model": team.default_model,
        "agents": [
            {
                "name": agent.name,
                "role": agent.role,
                "system_prompt": agent.system_prompt,
                "model": agent.model,
                "max_turns": agent.max_turns,
                # frozenset은 순서가 없으므로 정렬해 결정적으로 저장한다.
                "capabilities": sorted(c.value for c in agent.capabilities),
            }
            for agent in team.agents
        ],
        "termination": {
            "max_messages": team.termination.max_messages,
            "token_budget": team.termination.token_budget,
            "processed_token_limit": team.termination.processed_token_limit,
            "synthesis_at": team.termination.synthesis_at,
            "proposal_by": team.termination.proposal_by,
            "call_reserve_tokens": team.termination.call_reserve_tokens,
            "max_proposals": team.termination.max_proposals,
            "idle_timeout": team.termination.idle_timeout,
            "approval": {
                "mode": team.termination.approval.mode.value,
                "voting_timeout": team.termination.approval.voting_timeout,
                "minimum_votes": team.termination.approval.minimum_votes,
            },
        },
    }


def _team_from_dict(data: dict) -> TeamConfig:
    """_team_to_dict의 역변환 — 저장된 dict에서 TeamConfig를 복원한다."""
    agents = tuple(
        AgentSpec(
            name=a["name"],
            role=a["role"],
            system_prompt=a["system_prompt"],
            model=a["model"],
            max_turns=a["max_turns"],
            capabilities=frozenset(
                AgentCapability(value) for value in a["capabilities"]
            ),
        )
        for a in data["agents"]
    )
    approval_data = data["termination"]["approval"]
    approval = ApprovalConfig(
        mode=ApprovalPolicy(approval_data["mode"]),
        voting_timeout=approval_data["voting_timeout"],
        minimum_votes=approval_data["minimum_votes"],
    )
    termination = TerminationPolicy(
        max_messages=data["termination"]["max_messages"],
        token_budget=data["termination"]["token_budget"],
        processed_token_limit=data["termination"].get("processed_token_limit"),
        synthesis_at=data["termination"].get("synthesis_at"),
        proposal_by=data["termination"].get("proposal_by"),
        call_reserve_tokens=data["termination"].get("call_reserve_tokens"),
        max_proposals=data["termination"].get("max_proposals"),
        idle_timeout=data["termination"]["idle_timeout"],
        approval=approval,
    )
    return TeamConfig(
        name=data["name"],
        agents=agents,
        description=data["description"],
        default_model=data["default_model"],
        termination=termination,
    )


def _event_from_dict(data: dict) -> Event:
    """Event 복원 — contracts.Event에는 from_dict가 없어 여기서 역직렬화한다."""
    return Event(
        event_id=data["event_id"],
        session_id=data["session_id"],
        type=EventType(data["type"]),
        sequence=data["sequence"],
        created_at=data["created_at"],
        payload=data["payload"],
    )


def _dumps(obj: dict) -> str:
    """도메인 dict를 JSON 문자열로 — 결정적(sort_keys) 저장."""
    return json.dumps(obj, sort_keys=True)


class SQLiteStore:
    """Store Protocol의 SQLite 구현 (파일 영속). ":memory:"는 지원하지 않는다 —
    to_thread의 워커 스레드 간 인메모리 연결 공유 문제 때문에 파일 경로만 받는다.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        # 부모 디렉터리 자동 생성 (경로가 상대적이면 현재 디렉터리 기준).
        parent = self._path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        # 여러 워커 스레드에서 접근하므로 check_same_thread=False, Lock으로 직렬화.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 내부 동기 헬퍼 (Lock 보유 상태에서 호출) ----

    def _require_open(self) -> sqlite3.Connection:
        """닫힌 스토어 사용을 명확한 오류로 거부한다."""
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")
        return self._conn

    # ---- 세션 ----

    async def save_session(self, session: Session) -> None:
        await asyncio.to_thread(self._save_session, session)

    def _save_session(self, session: Session) -> None:
        with self._lock:
            conn = self._require_open()
            conn.execute(
                "INSERT OR REPLACE INTO sessions(id, status, created_at, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    session.status.value,
                    session.created_at,
                    _dumps(session.to_dict()),
                ),
            )
            conn.commit()

    async def get_session(self, session_id: str) -> Session | None:
        return await asyncio.to_thread(self._get_session, session_id)

    def _get_session(self, session_id: str) -> Session | None:
        with self._lock:
            conn = self._require_open()
            row = conn.execute(
                "SELECT data FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return Session.from_dict(json.loads(row[0]))

    async def list_sessions(self, *, limit: int = 50) -> list[Session]:
        return await asyncio.to_thread(self._list_sessions, limit)

    def _list_sessions(self, limit: int) -> list[Session]:
        with self._lock:
            conn = self._require_open()
            # 최근 생성 순 — created_at DESC, 동률은 rowid DESC(나중 삽입이 먼저).
            rows = conn.execute(
                "SELECT data FROM sessions "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Session.from_dict(json.loads(r[0])) for r in rows]

    async def list_sessions_by_status(
        self, status: SessionStatus
    ) -> list[Session]:
        return await asyncio.to_thread(self._list_sessions_by_status, status)

    def _list_sessions_by_status(self, status: SessionStatus) -> list[Session]:
        with self._lock:
            conn = self._require_open()
            rows = conn.execute(
                "SELECT data FROM sessions WHERE status = ? ORDER BY rowid ASC",
                (status.value,),
            ).fetchall()
        return [Session.from_dict(json.loads(r[0])) for r in rows]

    # ---- 팀 스냅샷 ----

    async def save_team_snapshot(self, session_id: str, team: TeamConfig) -> None:
        await asyncio.to_thread(self._save_team_snapshot, session_id, team)

    def _save_team_snapshot(self, session_id: str, team: TeamConfig) -> None:
        with self._lock:
            conn = self._require_open()
            conn.execute(
                "INSERT OR REPLACE INTO team_snapshots(session_id, data) "
                "VALUES (?, ?)",
                (session_id, _dumps(_team_to_dict(team))),
            )
            conn.commit()

    async def get_team_snapshot(self, session_id: str) -> TeamConfig | None:
        return await asyncio.to_thread(self._get_team_snapshot, session_id)

    def _get_team_snapshot(self, session_id: str) -> TeamConfig | None:
        with self._lock:
            conn = self._require_open()
            row = conn.execute(
                "SELECT data FROM team_snapshots WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _team_from_dict(json.loads(row[0]))

    # ---- 메시지 ----

    async def append_message(self, message: Message) -> None:
        await asyncio.to_thread(self._append_message, message)

    def _append_message(self, message: Message) -> None:
        with self._lock:
            conn = self._require_open()
            # 같은 id 중복 append는 무시 (중복 배달 방지, D-023).
            conn.execute(
                "INSERT OR IGNORE INTO messages(id, session_id, sequence, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    message.id,
                    message.session_id,
                    message.sequence,
                    _dumps(message.to_dict()),
                ),
            )
            conn.commit()

    async def list_messages(self, session_id: str) -> list[Message]:
        return await asyncio.to_thread(self._list_messages, session_id)

    def _list_messages(self, session_id: str) -> list[Message]:
        with self._lock:
            conn = self._require_open()
            rows = conn.execute(
                "SELECT data FROM messages WHERE session_id = ? "
                "ORDER BY sequence ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        return [Message.from_dict(json.loads(r[0])) for r in rows]

    # ---- 제안 / 투표 ----

    async def save_proposal(self, proposal: ResultProposal) -> None:
        await asyncio.to_thread(self._save_proposal, proposal)

    def _save_proposal(self, proposal: ResultProposal) -> None:
        with self._lock:
            conn = self._require_open()
            # id 기준 upsert — status 전이(pending→…→superseded)를 반영.
            conn.execute(
                "INSERT OR REPLACE INTO proposals(id, session_id, version, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    proposal.id,
                    proposal.session_id,
                    proposal.version,
                    _dumps(proposal.to_dict()),
                ),
            )
            conn.commit()

    async def list_proposals(self, session_id: str) -> list[ResultProposal]:
        return await asyncio.to_thread(self._list_proposals, session_id)

    def _list_proposals(self, session_id: str) -> list[ResultProposal]:
        with self._lock:
            conn = self._require_open()
            rows = conn.execute(
                "SELECT data FROM proposals WHERE session_id = ? "
                "ORDER BY version ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        return [ResultProposal.from_dict(json.loads(r[0])) for r in rows]

    async def append_vote(self, vote: Vote) -> None:
        await asyncio.to_thread(self._append_vote, vote)

    def _append_vote(self, vote: Vote) -> None:
        with self._lock:
            conn = self._require_open()
            # 같은 id 중복 append는 무시.
            conn.execute(
                "INSERT OR IGNORE INTO votes(id, session_id, proposal_id, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    vote.id,
                    vote.session_id,
                    vote.proposal_id,
                    _dumps(vote.to_dict()),
                ),
            )
            conn.commit()

    async def list_votes(
        self, session_id: str, proposal_id: str | None = None
    ) -> list[Vote]:
        return await asyncio.to_thread(self._list_votes, session_id, proposal_id)

    def _list_votes(
        self, session_id: str, proposal_id: str | None
    ) -> list[Vote]:
        with self._lock:
            conn = self._require_open()
            if proposal_id is not None:
                rows = conn.execute(
                    "SELECT data FROM votes WHERE session_id = ? "
                    "AND proposal_id = ? ORDER BY rowid ASC",
                    (session_id, proposal_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM votes WHERE session_id = ? ORDER BY rowid ASC",
                    (session_id,),
                ).fetchall()
        return [Vote.from_dict(json.loads(r[0])) for r in rows]

    # ---- 이벤트 ----

    async def append_event(self, event: Event) -> None:
        await asyncio.to_thread(self._append_event, event)

    def _append_event(self, event: Event) -> None:
        with self._lock:
            conn = self._require_open()
            # 같은 event_id 중복 append는 무시.
            conn.execute(
                "INSERT OR IGNORE INTO events(event_id, session_id, sequence, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.session_id,
                    event.sequence,
                    _dumps(event.to_dict()),
                ),
            )
            conn.commit()

    async def list_events(
        self, session_id: str, *, after_sequence: int = -1
    ) -> list[Event]:
        return await asyncio.to_thread(
            self._list_events, session_id, after_sequence
        )

    def _list_events(self, session_id: str, after_sequence: int) -> list[Event]:
        with self._lock:
            conn = self._require_open()
            # sequence 초과분만 오름차순 — Last-Event-ID 재개 (EventContract §5).
            rows = conn.execute(
                "SELECT data FROM events WHERE session_id = ? "
                "AND sequence > ? ORDER BY sequence ASC, rowid ASC",
                (session_id, after_sequence),
            ).fetchall()
        return [_event_from_dict(json.loads(r[0])) for r in rows]

    # ---- 수명주기 ----

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
