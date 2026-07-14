"""HTTP 서버 진입점 — `python -m hwabaek.serve` (M3).

로컬 전용으로 127.0.0.1에 바인딩한다 (D-012 — localhost 전용, 인증 계층 없음).

사용:
    python -m hwabaek.serve                     # 실 LLM(api_key), configs/ 팀, data/hwabaek.db
    python -m hwabaek.serve --fake --no-db      # 밀폐 데모 (실키/네트워크/DB 없음)
    python -m hwabaek.serve --port 9000 --team default

--fake는 run.py의 Fake LLM 패턴(내장 1인 first-mode 팀 + 스크립트 Fake)을 재사용해
네트워크·API 키 없이 세션 1건이 결정적으로 completed까지 관통하는 데모를 제공한다.

콘솔 출력은 영어 ASCII만 사용한다 (Windows cp949 콘솔 제약).
"""
from __future__ import annotations

import argparse

import uvicorn

from hwabaek.server.app import create_app

# 로컬 전용 바인딩 주소 (D-012). 노출 인터페이스로 바꾸지 않는다.
HOST = "127.0.0.1"


def build_app(args: argparse.Namespace):
    """CLI 인자로부터 FastAPI 앱을 조립한다 (uvicorn 기동과 분리해 테스트 가능)."""
    store = None
    if not args.no_db:
        from hwabaek.store.sqlite import SQLiteStore

        store = SQLiteStore(args.db)

    if args.fake:
        # run.py의 밀폐 Fake 패턴 재사용 — 내장 1인 first-mode 팀 + 스크립트 Fake.
        from hwabaek.run import _fake_llm_factory, _fake_team

        team_override = _fake_team()

        def llm_factory_provider(team, task):
            return _fake_llm_factory(task)

    else:
        # 실 LLM — auth 모드 검증은 여기서(기동 시) 실패하게 한다.
        from hwabaek.run import _real_llm_factory

        real_factory = _real_llm_factory(args.auth)
        team_override = None

        def llm_factory_provider(team, task):
            return real_factory

    return create_app(
        store=store,
        teams_dir="configs",
        llm_factory_provider=llm_factory_provider,
        team_override=team_override,
        default_team=args.team,
    )


def create_parser() -> argparse.ArgumentParser:
    """서버 CLI 파서 — 옵션 충돌을 uvicorn 기동 전에 검증한다."""
    parser = argparse.ArgumentParser(
        prog="python -m hwabaek.serve",
        description="Run the hwabaek local HTTP server (127.0.0.1 only).",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="port to bind (default: 8000)"
    )
    parser.add_argument(
        "--team", default="default",
        help="default team name (built-ins: quick|default|deep) for real runs "
             "when a request omits it; ignored with --fake",
    )
    parser.add_argument(
        "--auth", choices=["api_key", "chatgpt_oauth"], default="api_key",
        help="llm auth mode for real runs (D-026); ignored with --fake",
    )
    parser.add_argument(
        "--fake", action="store_true",
        help="hermetic demo with a scripted fake LLM (no network, no key)",
    )
    persistence = parser.add_mutually_exclusive_group()
    persistence.add_argument(
        "--db", default="data/hwabaek.db",
        help="sqlite path for session records (default: data/hwabaek.db)",
    )
    persistence.add_argument(
        "--no-db", action="store_true",
        help="disable persistence (in-memory active session only)",
    )
    return parser


def main() -> None:
    args = create_parser().parse_args()

    app = build_app(args)
    print(f"hwabaek server on http://{HOST}:{args.port}"
          + (" (fake LLM)" if args.fake else "")
          + (" (no db)" if args.no_db else f" (db: {args.db})"))
    uvicorn.run(app, host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
