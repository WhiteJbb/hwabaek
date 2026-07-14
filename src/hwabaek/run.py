"""CLI 실행 스크립트 — 콘솔에서 세션 1건을 돌려보는 스모크 진입점 (M2a).

사용:
    python -m hwabaek.run "your task here" [--team configs/team.default.yaml]
    python -m hwabaek.run "your task here" --fake   # 밀폐 스모크 (실키/네트워크 없음)

--fake는 내장 1인 팀(first 모드) + 스크립트 Fake LLM으로 전체 스택
(bus → agent → consensus → session)을 결정적으로 관통한다. 실 API 경로는
OPENAI_API_KEY 환경변수가 필요하다 (D-026 api_key 모드).

콘솔 출력은 영어 ASCII만 사용한다 (Windows cp949 콘솔 제약).
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

from hwabaek.config import load_team_config
from hwabaek.contracts import (
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    Event,
    EventType,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
)
from hwabaek.llm.fake import FakeLLMClient, text_response, tool_response
from hwabaek.session import SessionManager


def _clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id_factory():
    counter = itertools.count()
    prefix = uuid4().hex[:8]
    def factory() -> str:
        return f"{prefix}-{next(counter):06d}"
    return factory


def _print_event(event: Event) -> None:
    """이벤트를 한 줄 요약으로 출력한다 (영어 ASCII)."""
    p = event.payload
    if event.type is EventType.SESSION_STATUS:
        line = f"session -> {p['status']}"
        if p.get("fail_reason"):
            line += f" ({p['fail_reason']})"
    elif event.type is EventType.MESSAGE:
        recipients = ",".join(p["recipients"])
        line = f"message [{p['type']}] {p['sender']} -> {recipients}: {p['content'][:80]}"
    elif event.type is EventType.AGENT_STATE:
        line = f"agent {p['agent']} -> {p['state']}"
        if p.get("detail"):
            line += f" ({p['detail']})"
    elif event.type is EventType.USAGE:
        u = p["usage"]
        total = sum(u.values())
        line = f"usage total={total} budget={p['token_budget']}"
    elif event.type is EventType.VOTE_STATUS:
        line = (
            f"vote proposal v{p['proposal_version']}: "
            f"approve={len(p['approvals'])} reject={len(p['rejections'])} "
            f"pending={len(p['pending'])} abstain={len(p['abstained'])}"
        )
    else:  # RESULT
        line = f"result by {p['submitted_by']}"
    print(f"[{event.sequence:04d}] {line}")


def _fake_team() -> TeamConfig:
    """밀폐 스모크용 내장 1인 팀 — first 모드라 1인이 허용된다 (D-018)."""
    return TeamConfig(
        name="smoke",
        agents=(
            AgentSpec(
                name="solo",
                role="Smoke test agent",
                system_prompt="You are a smoke test agent.",
            ),
        ),
        termination=TerminationPolicy(
            max_messages=10,
            token_budget=10_000,
            idle_timeout=5.0,
            approval=ApprovalConfig(mode=ApprovalPolicy.FIRST, voting_timeout=5.0),
        ),
    )


def _fake_llm_factory(task: str):
    def factory(spec: AgentSpec) -> FakeLLMClient:
        return FakeLLMClient([
            tool_response(
                "submit_result",
                {"content": f"Smoke result for task: {task}"},
            ),
            text_response("Submitted the smoke result."),
        ])
    return factory


def _real_llm_factory():
    # OPENAI_API_KEY는 SDK가 읽는다 — 존재만 확인하고 값은 다루지 않는다(마스킹 원칙).
    if not os.environ.get("OPENAI_API_KEY"):
        print("error: OPENAI_API_KEY is not set (required for real runs; use --fake for a hermetic smoke)")
        raise SystemExit(2)
    from hwabaek.llm.openai_client import OpenAIClient

    client = OpenAIClient()

    def factory(spec: AgentSpec) -> OpenAIClient:
        return client  # 어댑터는 상태 없는 호출 래퍼 — 에이전트 간 공유 가능

    return factory


async def _run(args: argparse.Namespace) -> int:
    if args.fake:
        team = _fake_team()
        llm_factory = _fake_llm_factory(args.task)
    else:
        team = load_team_config(args.team)
        llm_factory = _real_llm_factory()

    store = None
    if not args.no_db:
        from hwabaek.store.sqlite import SQLiteStore

        store = SQLiteStore(args.db)

    manager = SessionManager(
        team,
        args.task,
        llm_factory=llm_factory,
        clock=_clock,
        id_factory=_make_id_factory(),
        on_event=_print_event,
        store=store,
    )
    try:
        session = await manager.run()
    finally:
        if store is not None:
            await store.close()

    print("-" * 60)
    print(f"status: {session.status.value}"
          + (f" ({session.fail_reason.value})" if session.fail_reason else ""))
    if session.fail_detail:
        print(f"detail: {session.fail_detail}")
    if session.result is not None:
        print(f"result (by {session.submitted_by}):")
        print(session.result)
    elif session.draft_result is not None:
        print(f"unratified draft (by {session.draft_proposer}):")
        print(session.draft_result)
    print(f"tokens: {session.usage.total_tokens}")
    if store is not None:
        print(f"session {session.id} stored in {args.db}")
    return 0 if session.status is SessionStatus.COMPLETED else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m hwabaek.run",
        description="Run one hwabaek session from the console.",
    )
    parser.add_argument("task", help="task for the team")
    parser.add_argument(
        "--team", default="configs/team.default.yaml",
        help="team config yaml (default: configs/team.default.yaml)",
    )
    parser.add_argument(
        "--fake", action="store_true",
        help="hermetic smoke run with a scripted fake LLM (no network, no key)",
    )
    parser.add_argument(
        "--db", default="data/hwabaek.db",
        help="sqlite path for session records (default: data/hwabaek.db)",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="disable persistence for this run",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
