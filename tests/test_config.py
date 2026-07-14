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


# 최소 에이전트 블록 — 2인 팀. approval mode가 first가 아닌 한 최소 2인이
# 필요하다(D-018: 제출자는 자기 제안에 투표할 수 없음)는 계약 요건 때문에,
# 여러 케이스에서 재사용하는 이 최소 블록도 2인으로 구성한다.
MINIMAL_AGENT_BLOCK = """
agents:
  - name: solo
    role: does everything
    system_prompt: You are a helpful agent. respond in the language of the task.
  - name: helper
    role: assists solo
    system_prompt: You are a helpful assistant. respond in the language of the task.
"""

# 1인 팀 전용 블록 — approval mode가 first일 때만 허용됨을 검증하는 케이스에서 쓴다.
SOLO_AGENT_BLOCK = """
agents:
  - name: solo
    role: does everything
    system_prompt: You are a helpful agent. respond in the language of the task.
"""


class LoadTeamConfigValidCasesTest(unittest.TestCase):
    """정상 로드 경로 — 전 필드 명시 / 선택 필드 생략 / approval 신구 형식."""

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
        self.assertEqual(team.termination.approval.mode, ApprovalPolicy.MAJORITY)
        self.assertEqual(team.termination.approval.voting_timeout, 120.0)
        self.assertIsNone(team.termination.approval.minimum_votes)
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
        self.assertEqual(team.termination.approval.mode, ApprovalPolicy.UNANIMOUS)
        self.assertEqual(team.termination.approval.voting_timeout, 120.0)
        self.assertIsNone(team.termination.approval.minimum_votes)
        self.assertEqual(len(team.agents), 2)
        agent = team.agents[0]
        self.assertEqual(agent.name, "solo")
        self.assertIsNone(agent.model)
        self.assertEqual(agent.max_turns, 50)

    def test_string_approval_is_backward_compatible_mode_shorthand(self) -> None:
        # 구형: approval에 mode 문자열 하나만 지정 — timeout/minimum은 계약 기본값.
        content = (
            "name: t\n"
            "termination:\n"
            "  approval: participating_unanimous\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            team = load_team_config(path)

        approval = team.termination.approval
        self.assertEqual(approval.mode, ApprovalPolicy.PARTICIPATING_UNANIMOUS)
        self.assertEqual(approval.voting_timeout, 120.0)
        self.assertIsNone(approval.minimum_votes)

    def test_mapping_approval_loads_all_fields(self) -> None:
        # 신형: mode/timeout_seconds/minimum_votes 전체 지정.
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: participating_unanimous\n"
            "    timeout_seconds: 45\n"
            "    minimum_votes: 2\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            team = load_team_config(path)

        approval = team.termination.approval
        self.assertEqual(approval.mode, ApprovalPolicy.PARTICIPATING_UNANIMOUS)
        self.assertEqual(approval.voting_timeout, 45)
        self.assertEqual(approval.minimum_votes, 2)

    def test_solo_team_with_first_mode_is_allowed(self) -> None:
        # 1인 팀은 approval mode가 first일 때만 허용된다 (D-018).
        content = (
            "name: t\n"
            "termination:\n"
            "  approval: first\n"
            + SOLO_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            team = load_team_config(path)

        self.assertEqual(len(team.agents), 1)
        self.assertEqual(team.termination.approval.mode, ApprovalPolicy.FIRST)


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
        self.assertEqual(team.termination.approval.mode, ApprovalPolicy.UNANIMOUS)
        self.assertEqual(team.termination.approval.voting_timeout, 120.0)
        self.assertIsNone(team.termination.approval.minimum_votes)
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

    def test_invalid_approval_string_lists_valid_options(self) -> None:
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
            for valid in ("unanimous", "majority", "participating_unanimous", "first"):
                self.assertIn(valid, message)

    def test_invalid_approval_mapping_mode_lists_valid_options(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: sometimes\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            message = str(ctx.exception)
            self.assertIn("sometimes", message)
            for valid in ("unanimous", "majority", "participating_unanimous", "first"):
                self.assertIn(valid, message)

    def test_approval_mapping_unknown_key_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: unanimous\n"
            "    bogus_key: 1\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            message = str(ctx.exception)
            self.assertIn("bogus_key", message)
            self.assertIn("approval", message)

    def test_approval_mapping_missing_mode_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    timeout_seconds: 10\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            message = str(ctx.exception)
            self.assertIn("mode", message)
            self.assertIn("missing required key", message)

    def test_approval_timeout_seconds_negative_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: unanimous\n"
            "    timeout_seconds: -5\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("timeout_seconds", str(ctx.exception))

    def test_approval_timeout_seconds_string_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: unanimous\n"
            '    timeout_seconds: "soon"\n'
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("timeout_seconds", str(ctx.exception))

    def test_approval_list_type_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval: [unanimous]\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn("approval", str(ctx.exception))

    def test_minimum_votes_with_unanimous_wraps_contract_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval:\n"
            "    mode: unanimous\n"
            "    minimum_votes: 2\n"
            + MINIMAL_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, ContractError)

    def test_solo_team_with_unanimous_mode_raises_config_error(self) -> None:
        content = (
            "name: t\n"
            "termination:\n"
            "  approval: unanimous\n"
            + SOLO_AGENT_BLOCK
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "team.yaml", content)
            with self.assertRaises(ConfigError) as ctx:
                load_team_config(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, ContractError)

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
