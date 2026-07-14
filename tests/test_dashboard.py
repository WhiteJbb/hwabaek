"""M4 정적 대시보드와 서버 라우팅 통합 테스트.

밀폐 원칙: 실키·실네트워크·SQLite 없이 TestClient로만 검증한다.
"""
from __future__ import annotations

import unittest
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from hwabaek.server import create_app


class _DashboardHTMLParser(HTMLParser):
    """대시보드의 구조 id와 내비게이션 텍스트를 수집한다."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self._nav_depth = 0
        self.nav_text: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag == "nav":
            self._nav_depth += 1

    def handle_endtag(self, tag) -> None:
        if tag == "nav" and self._nav_depth:
            self._nav_depth -= 1

    def handle_data(self, data) -> None:
        if self._nav_depth and data.strip():
            self.nav_text.append(data.strip())


def _unused_llm_provider(team, task):
    """정적 화면 테스트에서 호출되면 안 되는 LLM 팩토리 대역."""
    raise AssertionError("dashboard asset requests must not create an LLM client")


class DashboardTest(unittest.TestCase):
    def _app(self):
        return create_app(
            store=None,
            llm_factory_provider=_unused_llm_provider,
            interrupt_on_startup=False,
        )

    def test_root_redirects_to_dashboard(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/", follow_redirects=False)

        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"], "/app/")

    def test_dashboard_html_exposes_shell_and_korean_navigation(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/app/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))

        parser = _DashboardHTMLParser()
        parser.feed(response.text)
        self.assertTrue(
            {"route-view", "primary-nav", "connection-status", "toast-region", "live-region"}
            <= parser.ids
        )
        self.assertIn("화백", response.text)
        navigation = " ".join(parser.nav_text)
        for label in ("홈", "세션", "팀"):
            with self.subTest(label=label):
                self.assertIn(label, navigation)

    def test_dashboard_stylesheet_is_served_as_css(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/app/styles.css")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/css"))
        self.assertTrue(response.content)

    def test_dashboard_script_contains_api_and_hash_route_contracts(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/app/app.js")

        self.assertEqual(response.status_code, 200)
        content_type = response.headers["content-type"]
        self.assertTrue(
            content_type.startswith("text/javascript")
            or content_type.startswith("application/javascript")
        )
        script = response.text
        for token in (
            "EventSource",
            "POST",
            "/sessions",
            "cancel",
            "/teams",
            "#/",
            "#/sessions",
            "#/teams",
            "mergeRecords",
            "canAdvance",
            "activeStream !== source",
        ):
            with self.subTest(token=token):
                self.assertIn(token, script)

    def test_dashboard_separates_work_cache_and_processed_token_usage(self) -> None:
        with TestClient(self._app()) as client:
            script = client.get("/app/app.js").text

        for token in (
            '["input_tokens", "output_tokens", "cache_write_tokens"]',
            "usage.cache_read_tokens",
            "작업 예산",
            "캐시 재사용",
            "전체 처리",
            'aria-label="작업 토큰 예산 사용량"',
            "processed_token_limit",
            "payload.phase",
            "synthesis_at",
            "proposal_by",
            "teamData.default_team",
            "defaultTeamExists",
            "서버 기본 팀을 찾을 수 없습니다.",
            "Quick은 짧고 가역적인 선택",
            "일반 채팅",
            "토론 최대",
            "failureExplanation",
        ):
            with self.subTest(token=token):
                self.assertIn(token, script)

        self.assertNotIn(
            '["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]',
            script,
        )

    def test_static_mount_does_not_shadow_session_api(self) -> None:
        with TestClient(self._app()) as client:
            response = client.get("/sessions/nope")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        body = response.json()
        self.assertEqual(set(body), {"detail"})
        self.assertIn("not found", body["detail"])


if __name__ == "__main__":
    unittest.main()
