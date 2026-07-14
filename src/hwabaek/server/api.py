"""REST + SSE 라우트와 대시보드용 응답 (M3/M4).

엔드포인트:
- POST /sessions              태스크 제출 (동시 세션 1개 검사+생성 원자화)
- GET  /sessions              저장된 세션 목록
- GET  /sessions/{id}         세션 상세 (상태·팀 요약·결과·사용량 + 메시지·제안·투표)
- POST /sessions/{id}/cancel  실행 중 세션 취소
- GET  /teams                 팀 설정 목록 (이름·에이전트 요약)
- GET  /sessions/{id}/events  SSE 이벤트 스트림 (Last-Event-ID 재전송)

모든 store 접근은 async(요청 경로 비블로킹), 사용자 노출 문자열은 영어 ASCII.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from hwabaek.config import ConfigError
from hwabaek.server.events import (
    ConcurrentSessionError,
    SessionNotActiveError,
    SessionNotFoundError,
    SessionRegistry,
    UnknownTeamError,
    team_summary,
)

router = APIRouter()
MAX_EVENT_SEQUENCE = (1 << 63) - 1


class CreateSessionRequest(BaseModel):
    """POST /sessions 요청 본문. task는 비어 있을 수 없다(빈 문자열은 422)."""

    task: str = Field(min_length=1, description="task for the team")
    team: str | None = Field(default=None, description="team name (defaults to server default)")

    @field_validator("task")
    @classmethod
    def task_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("task must not be blank")
        return normalized


def _registry(request: Request) -> SessionRegistry:
    return request.app.state.registry


def _error(response: Response, code: int, message: str) -> dict:
    """상태 코드를 설정하고 표준 에러 본문을 만든다 (detail 키 — ASCII 메시지)."""
    response.status_code = code
    return {"detail": message}


@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest, request: Request, response: Response
) -> dict:
    """태스크를 제출해 세션을 생성한다. 동시 세션이 있으면 409."""
    registry = _registry(request)
    try:
        session = await registry.create_session(body.task, body.team)
    except ConcurrentSessionError as exc:
        return _error(response, status.HTTP_409_CONFLICT, str(exc))
    except UnknownTeamError as exc:
        return _error(response, status.HTTP_400_BAD_REQUEST, str(exc))
    except ConfigError:
        # YAML 파서 오류에는 원문 줄과 절대 경로가 포함될 수 있으므로 외부에 노출하지 않는다.
        return _error(
            response,
            status.HTTP_400_BAD_REQUEST,
            "team configuration is invalid",
        )
    response.status_code = status.HTTP_201_CREATED
    return session.to_dict()


@router.get("/sessions")
async def list_sessions(request: Request, limit: int = 50) -> dict:
    """저장된 세션 목록 (최근 생성 순)."""
    registry = _registry(request)
    capped = max(1, min(limit, 200))
    sessions = await registry.list_sessions(limit=capped)
    return {"sessions": [s.to_dict() for s in sessions]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request, response: Response) -> dict:
    """세션 상세 — 상태·nullable 팀 요약·사용량과 메시지·제안·투표."""
    registry = _registry(request)
    try:
        return await registry.get_session_detail(session_id)
    except SessionNotFoundError as exc:
        return _error(response, status.HTTP_404_NOT_FOUND, str(exc))


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str, request: Request, response: Response
) -> dict:
    """실행 중 세션을 취소한다. 종료된 세션이면 409, 없으면 404."""
    registry = _registry(request)
    try:
        session = await registry.cancel_session(session_id)
    except SessionNotFoundError as exc:
        return _error(response, status.HTTP_404_NOT_FOUND, str(exc))
    except SessionNotActiveError as exc:
        return _error(response, status.HTTP_409_CONFLICT, str(exc))
    return session.to_dict()


@router.get("/teams")
async def list_teams(request: Request, response: Response) -> dict:
    """팀 설정 목록 (이름·설명·에이전트 요약·종료 정책)."""
    registry = _registry(request)
    try:
        teams = await registry.list_teams()
    except UnknownTeamError as exc:
        return _error(response, status.HTTP_400_BAD_REQUEST, str(exc))
    except ConfigError:
        # 설정 원문에는 API 키 같은 비밀값이 있을 수 있다.
        return _error(
            response,
            status.HTTP_400_BAD_REQUEST,
            "team configuration is invalid",
        )
    return {
        "default_team": registry.default_team_name,
        "teams": [team_summary(t) for t in teams],
    }


def _parse_last_event_id(raw: str | None) -> int:
    """Last-Event-ID를 0 이상 sequence로 파싱한다. 없으면 -1(처음부터)."""
    if raw is None:
        return -1
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID must be a non-negative integer",
        ) from None
    if value < 0 or value > MAX_EVENT_SEQUENCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID must be a non-negative integer",
        )
    return value


@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> Response:
    """SSE 스트림 — 저장/메모리 이벤트 재전송(Last-Event-ID sequence 초과분) 후
    라이브 스트림. 종료 세션이면 재전송 후 종료한다 (EventContract §5)."""
    registry = _registry(request)
    # 존재하지 않는 세션은 스트림을 열기 전에 404로 거부한다.
    if registry.get_runner(session_id) is None:
        session = await registry.current_session(session_id)
        if session is None:
            return Response(
                content='{"detail":"session not found"}',
                media_type="application/json",
                status_code=status.HTTP_404_NOT_FOUND,
            )
    after = _parse_last_event_id(last_event_id)
    return StreamingResponse(
        registry.event_stream(session_id, after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
