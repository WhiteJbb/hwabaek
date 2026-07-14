"""hwabaek.config 로더 단위 테스트.

네트워크/실키 없이 tempfile로 임시 YAML을 만들어 검증한다 (테스트 밀폐 원칙).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hwabaek.config import ConfigError, list_team_configs, load_team_config
from hwabaek.contracts import DEFAULT_MODEL, ApprovalPolicy, ContractError

# 저장소 루트 (tests/ 의 부모).
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEAM_YAML = REPO_ROOT / "configs" / "team.default.yaml"


def _write(directory: Path, filename: str, content: str) -> Path:
    """directory 아래에 UTF-8로 YAML 파일을 쓰고 경로를 반환한다."""
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


MINIMAL_AGENT_BLOCK = """
agents:
  - name: solo
    role: does everything
    system_prompt: You are a helpful agent. respond in the language of the task.
"""


class LoadTeamConfigValidCasesTest(unittest.TestCase):
    """정상 로드 경로 — 전 필드 명시 / 선택 필드 생략."""

    def test_loads_with_all_fields_explicit(self) -> None:
        content = """
name: full-team
description: a fully specified team
default_model: gpt-custom-model
termination:
  max_messages: 42
  token_budget: 12345
  idle_timeout: 5.5
  approval: majority
agents:
  - name: alpha
    role: first agent
    system_prompt: You are alpha. respond in the language of the task.
    model: alpha-model
    max_turns: 10
  - name: beta
    role: second agent
    system_prompt: You are beta. respond in the language of the task.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            team = load_team_config(path)

        self.assertEqual(team.name, "full-team")
        self.assertEqual(team.description, "a fully specified team")
        self.assertEqual(team.default_model, "gpt-custom-model")
        self.assertEqual(team.termination.max_messages, 42)
        self.assertEqual(team.termination.token_budget, 12345)
        self.assertEqual(team.termination.idle_timeout, 5.5)
        self.assertEqual(team.termination.approval, ApprovalPolicy.MAJORITY)
        self.assertEqual(len(team.agents), 2)
        alpha, beta = team.agents
        self.assertEqual(alpha.name, "alpha")
        self.assertEqual(alpha.model, "alpha-model")
        self.assertEqual(alpha.max_turns, 10)
        self.assertEqual(beta.name, "beta")
        self.assertIsNone(beta.model)
        self.assertEqual(beta.max_turns, 50)

    def test_loads_with_optional_fields_omitted_uses_contract_defaults(self) -> None:
        content = "name: minimal-team\n" + MINIMAL_AGENT_BLOCK
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            team = load_team_config(path)

        self.assertEqual(team.name, "minimal-team")
        self.assertEqual(team.description, "")
        self.assertEqual(team.default_model, DEFAULT_MODEL)
        self.assertEqual(team.termination.max_messages, 100)
        self.assertEqual(team.termination.token_budget, 200_000)
        self.assertEqual(team.termination.idle_timeout, 30.0)
        self.assertEqual(team.termination.approval, ApprovalPolicy.UNANIMOUS)
        self.assertEqual(len(team.agents), 1)
        agent = team.agents[0]
        self.assertEqual(agent.name, "solo")
        self.assertIsNone(agent.model)
        self.assertEqual(agent.max_turns, 50)


class DefaultTeamYamlTest(unittest.TestCase):
    """configs/team.default.yaml 실제 파일 검증."""

    def test_default_team_yaml_has_three_agents_and_unanimous_approval(self) -> None:
        self.assertTrue(
            DEFAULT_TEAM_YAML.exists(), f"missing default team file: {DEFAULT_TEAM_YAML}"
        )
        team = load_team_config(DEFAULT_TEAM_YAML)

        self.assertEqual(team.name, "default")
        self.assertEqual(len(team.agents), 3)
        agent_names = {agent.name for agent in team.agents}
        self.assertEqual(agent_names, {"researcher", "analyst", "writer"})
        self.assertEqual(team.termination.approval, ApprovalPolicy.UNANIMOUS)
        self.assertEqual(team.termination.max_messages, 100)
        self.assertEqual(team.termination.token_budget, 200_000)
        self.assertEqual(team.termination.idle_timeout, 30.0)


class LoadTeamConfigErrorCasesTest(unittest.TestCase):
    """오류 경로 — 파일 없음/문법 오류/스키마 위반/계약 위반."""

    def test_missing_file_raises_config_error_with_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.yaml"
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(missing)
            self.assertIn(str(missing), str(ctx.exception))

    def test_yaml_syntax_error_raises_config_error(self) -> None:
        content = "name: [unterminated\nagents: ["
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_root_is_list_raises_config_error(self) -> None:
        content = "- name: not-a-mapping\n- agents: []\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("root", str(ctx.exception))

    def test_unknown_top_level_key_raises_config_error(self) -> None:
        content = "name: t\nbogus_key: true\n" + MINIMAL_AGENT_BLOCK
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("bogus_key", str(ctx.exception))

    def test_unknown_termination_key_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  max_messages: 10\n"
            "  bogus_key: 1\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("bogus_key", str(ctx.exception))
            self.assertIn("termination", str(ctx.exception))

    def test_unknown_agent_key_raises_config_error(self) -> None:
        content = """
name: t
agents:
  - name: solo
    role: does everything
    system_prompt: You are a helpful agent.
    bogus_key: 1
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("bogus_key", str(ctx.exception))
            self.assertIn("agents[0]", str(ctx.exception))

    def test_missing_team_name_raises_config_error(self) -> None:
        content = MINIMAL_AGENT_BLOCK
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("name", str(ctx.exception))

    def test_missing_agents_raises_config_error(self) -> None:
        content = "name: t\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("agents", str(ctx.exception))

    def test_missing_agent_role_raises_config_error(self) -> None:
        content = """
name: t
agents:
  - name: solo
    system_prompt: You are a helpful agent.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("role", str(ctx.exception))
            self.assertIn("agents[0]", str(ctx.exception))

    def test_invalid_approval_value_lists_valid_options(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval: sometimes\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            message = str(ctx.exception)
            self.assertIn("sometimes", message)
            for valid in ("unanimous", "majority", "first"):
                self.assertIn(valid, message)

    def test_max_messages_type_error_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  max_messages: \"100\"\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("max_messages", str(ctx.exception))

    def test_invalid_agent_name_wraps_contract_error(self) -> None:
        # 회귀 테스트: 에이전트 이름 규칙 위반이 ContractError로 새지 않고
        # 파일 경로를 포함한 ConfigError로 감싸져야 한다 (통합 리뷰에서 발견된 누출).
        content = """
name: t
agents:
  - name: Bad Name
    role: broken agent
    system_prompt: You are misnamed.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("agents[0]", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, ContractError)

    def test_duplicate_agent_names_wraps_contract_error(self) -> None:
        content = """
name: t
agents:
  - name: dup
    role: first
    system_prompt: You are the first dup.
  - name: dup
    role: second
    system_prompt: You are the second dup.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, ContractError)


class ListTeamConfigsTest(unittest.TestCase):
    """list_team_configs — 디렉터리 일괄 로드."""

    def test_loads_all_yaml_files_sorted_by_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write(tmp_path, "b.yaml", "name: team-b\n" + MINIMAL_AGENT_BLOCK)
            _write(tmp_path, "a.yaml", "name: team-a\n" + MINIMAL_AGENT_BLOCK)
            _write(tmp_path, "c.yaml", "name: team-c\n" + MINIMAL_AGENT_BLOCK)

            teams = list_team_configs(tmp_path)

        self.assertEqual([t.name for t in teams], ["team-a", "team-b", "team-c"])

    def test_empty_directory_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            teams = list_team_configs(tmp)
        self.assertEqual(teams, [])

    def test_missing_directory_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-dir"
            with self.assertRaises(ConfigError):
                list_team_configs(missing)


if __name__ == "__main__":
    unittest.main()
