"""콘솔 진입점의 프로필 해석과 M5 사용량 표시를 밀폐 검증한다."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from hwabaek.contracts import Usage, make_usage_event
from hwabaek.config import ConfigError
from hwabaek.run import _load_team, _print_event


class RunCliTest(unittest.TestCase):
    def test_builtin_profile_name_loads_yaml(self) -> None:
        self.assertEqual(_load_team("quick").name, "quick")
        self.assertEqual(_load_team("default").name, "default")
        self.assertEqual(_load_team("deep").name, "deep")

    def test_unknown_profile_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "unknown team profile 'quik'.*quick, default, deep"
        ):
            _load_team("quik")

    def test_usage_output_separates_work_cache_and_processed(self) -> None:
        event = make_usage_event(
            "e1",
            3,
            "s1",
            Usage(input_tokens=10, output_tokens=5, cache_read_tokens=30),
            60_000,
            "2026-07-14T00:00:00Z",
            processed_token_limit=150_000,
            phase="synthesis",
            reserved_tokens=6_000,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            _print_event(event)

        rendered = output.getvalue()
        self.assertIn("work=15/60000", rendered)
        self.assertIn("cache_read=30", rendered)
        self.assertIn("processed=45/150000", rendered)
        self.assertIn("phase=synthesis", rendered)


if __name__ == "__main__":
    unittest.main()
