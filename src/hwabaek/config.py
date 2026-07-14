"""hwabaek 팀 설정 YAML 로더.

`configs/*.yaml` 형태의 팀 설정 파일을 읽어 contracts.py의 TeamConfig로
변환한다. 스키마는 엄격하게 검증한다(오타 방어) — 허용되지 않은 키가
있으면 즉시 실패하고, 필드 경로를 포함한 영어 ASCII 오류 메시지를 낸다.

termination.approval 마이그레이션 (D-019): 구형은 `approval: <mode>` 처럼
mode 문자열 하나만 쓰는 축약형이며 하위 호환으로 계속 지원한다. 신형은
`approval: {mode, timeout_seconds, minimum_votes}` 매핑으로, voting 전용
타이머(timeout_seconds → ApprovalConfig.voting_timeout)와 participating_unanimous
전용 하한(minimum_votes)을 함께 지정할 수 있다.

이 모듈은 파일 I/O(open)와 yaml.safe_load만 표준 의존성 위에 추가한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hwabaek.contracts import (
    AgentCapability,
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    ContractError,
    TeamConfig,
    TerminationPolicy,
)

# 스키마에서 허용하는 키 집합 — 여기 없는 키는 오타로 간주해 거부한다.
_TOP_LEVEL_KEYS = {"name", "description", "default_model", "termination", "agents"}
_TERMINATION_KEYS = {
    "max_messages", "token_budget", "processed_token_limit", "synthesis_at",
    "proposal_by", "call_reserve_tokens", "max_proposals", "idle_timeout", "approval",
}
_AGENT_KEYS = {"name", "role", "system_prompt", "model", "max_turns", "capabilities"}
_APPROVAL_KEYS = {"mode", "timeout_seconds", "minimum_votes"}


class ConfigError(ValueError):
    """팀 설정 로딩/검증 실패. 메시지는 항상 파일 경로와 필드 경로를 포함한다."""


def _reject_unknown_keys(
    data: dict[str, Any], allowed: set[str], *, file_path: Path, prefix: str
) -> None:
    """매핑에 허용되지 않은 키가 있으면 ConfigError를 낸다."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ConfigError(
            f"team config {file_path}: {prefix}: unknown key(s): {joined}"
        )


def _require_str(
    data: dict[str, Any], key: str, *, file_path: Path, prefix: str
) -> str:
    """필수 문자열 필드를 꺼낸다. 없거나 빈 문자열/타입 불일치면 ConfigError."""
    if key not in data:
        raise ConfigError(f"team config {file_path}: {prefix}.{key}: missing required key")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ConfigError(
            f"team config {file_path}: {prefix}.{key}: expected a non-empty string, "
            f"got {value!r}"
        )
    return value


def _optional_str(
    data: dict[str, Any], key: str, *, file_path: Path, prefix: str, default: str
) -> str:
    """선택 문자열 필드를 꺼낸다. 생략 시 default. 있으면 문자열 타입이어야 한다."""
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(
            f"team config {file_path}: {prefix}.{key}: expected a string, got {value!r}"
        )
    return value


def _optional_int(
    data: dict[str, Any], key: str, *, file_path: Path, prefix: str, default: int | None
) -> int | None:
    """선택 정수 필드를 꺼낸다. bool은 int의 서브클래스이지만 의도치 않은 타입이므로 거부."""
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"team config {file_path}: {prefix}.{key}: expected an int, got {value!r}"
        )
    return value


def _optional_number(
    data: dict[str, Any], key: str, *, file_path: Path, prefix: str, default: float | None
) -> float | None:
    """선택 수치(int/float) 필드를 꺼낸다."""
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"team config {file_path}: {prefix}.{key}: expected a number, got {value!r}"
        )
    return value


def _invalid_approval_mode_error(
    value: Any, *, file_path: Path, prefix: str
) -> ConfigError:
    """approval mode 값이 ApprovalPolicy에 없을 때의 오류. 4종 유효값을 모두 나열한다."""
    valid = ", ".join(p.value for p in ApprovalPolicy)
    return ConfigError(
        f"team config {file_path}: {prefix}: invalid value {value!r}, "
        f"expected one of: {valid}"
    )


def _parse_approval(
    approval_raw: Any, *, file_path: Path, prefix: str, default: ApprovalConfig
) -> ApprovalConfig:
    """termination.approval을 파싱한다.

    구형(문자열, mode 축약형)과 신형(매핑, mode/timeout_seconds/minimum_votes)을
    모두 허용한다 (D-019 마이그레이션).
    """
    if approval_raw is None:
        return default

    if isinstance(approval_raw, str):
        try:
            mode = ApprovalPolicy(approval_raw)
        except ValueError:
            raise _invalid_approval_mode_error(
                approval_raw, file_path=file_path, prefix=f"{prefix}.approval"
            ) from None
        try:
            return ApprovalConfig(mode=mode)
        except ContractError as e:
            raise ConfigError(f"team config {file_path}: {prefix}.approval: {e}") from e

    if isinstance(approval_raw, dict):
        approval_prefix = f"{prefix}.approval"
        _reject_unknown_keys(
            approval_raw, _APPROVAL_KEYS, file_path=file_path, prefix=approval_prefix
        )

        mode_raw = approval_raw.get("mode")
        if mode_raw is None:
            raise ConfigError(
                f"team config {file_path}: {approval_prefix}.mode: missing required key"
            )
        if not isinstance(mode_raw, str):
            raise ConfigError(
                f"team config {file_path}: {approval_prefix}.mode: expected a string, "
                f"got {mode_raw!r}"
            )
        try:
            mode = ApprovalPolicy(mode_raw)
        except ValueError:
            raise _invalid_approval_mode_error(
                mode_raw, file_path=file_path, prefix=f"{approval_prefix}.mode"
            ) from None

        kwargs: dict[str, Any] = {"mode": mode}

        if "timeout_seconds" in approval_raw:
            timeout_raw = approval_raw["timeout_seconds"]
            if (
                isinstance(timeout_raw, bool)
                or not isinstance(timeout_raw, (int, float))
                or timeout_raw <= 0
            ):
                raise ConfigError(
                    f"team config {file_path}: {approval_prefix}.timeout_seconds: "
                    f"expected a positive number, got {timeout_raw!r}"
                )
            kwargs["voting_timeout"] = timeout_raw

        if "minimum_votes" in approval_raw:
            min_votes_raw = approval_raw["minimum_votes"]
            if min_votes_raw is not None and (
                isinstance(min_votes_raw, bool)
                or not isinstance(min_votes_raw, int)
                or min_votes_raw < 1
            ):
                raise ConfigError(
                    f"team config {file_path}: {approval_prefix}.minimum_votes: "
                    f"expected null or a positive int, got {min_votes_raw!r}"
                )
            kwargs["minimum_votes"] = min_votes_raw

        try:
            return ApprovalConfig(**kwargs)
        except ContractError as e:
            raise ConfigError(f"team config {file_path}: {approval_prefix}: {e}") from e

    raise ConfigError(
        f"team config {file_path}: {prefix}.approval: expected a string or mapping, "
        f"got {approval_raw!r}"
    )


def _parse_termination(
    data: dict[str, Any] | None, *, file_path: Path
) -> TerminationPolicy:
    """termination 섹션을 파싱한다. 섹션 자체가 생략되면 계약 기본값을 그대로 쓴다."""
    if data is None:
        return TerminationPolicy()
    if not isinstance(data, dict):
        raise ConfigError(
            f"team config {file_path}: termination: expected a mapping, got {data!r}"
        )
    prefix = "termination"
    _reject_unknown_keys(data, _TERMINATION_KEYS, file_path=file_path, prefix=prefix)

    defaults = TerminationPolicy()
    max_messages = _optional_int(
        data, "max_messages", file_path=file_path, prefix=prefix,
        default=defaults.max_messages,
    )
    token_budget = _optional_int(
        data, "token_budget", file_path=file_path, prefix=prefix,
        default=defaults.token_budget,
    )
    processed_token_limit = _optional_int(
        data, "processed_token_limit", file_path=file_path, prefix=prefix, default=None,
    )
    synthesis_at = _optional_int(
        data, "synthesis_at", file_path=file_path, prefix=prefix, default=None,
    )
    proposal_by = _optional_int(
        data, "proposal_by", file_path=file_path, prefix=prefix, default=None,
    )
    call_reserve_tokens = _optional_int(
        data, "call_reserve_tokens", file_path=file_path, prefix=prefix, default=None,
    )
    max_proposals = _optional_int(
        data, "max_proposals", file_path=file_path, prefix=prefix, default=None,
    )
    idle_timeout = _optional_number(
        data, "idle_timeout", file_path=file_path, prefix=prefix,
        default=defaults.idle_timeout,
    )
    approval = _parse_approval(
        data.get("approval"), file_path=file_path, prefix=prefix, default=defaults.approval
    )

    try:
        return TerminationPolicy(
            max_messages=max_messages,
            token_budget=token_budget,
            processed_token_limit=processed_token_limit,
            synthesis_at=synthesis_at,
            proposal_by=proposal_by,
            call_reserve_tokens=call_reserve_tokens,
            max_proposals=max_proposals,
            idle_timeout=idle_timeout,
            approval=approval,
        )
    except ContractError as e:
        raise ConfigError(f"team config {file_path}: {prefix}: {e}") from e


def _parse_capabilities(
    data: dict[str, Any], *, file_path: Path, prefix: str
) -> frozenset[AgentCapability] | None:
    """agent capabilities 필드를 파싱한다.

    생략 시 None을 반환해(kwargs에서 제외) 계약 기본값(전체 권한)을 쓰게 한다.
    지정 시 문자열 리스트여야 한다 — 빈 리스트는 허용(어떤 도구도 못 쓰는
    관찰자 에이전트). 각 값은 AgentCapability로 변환하며 중복은 집합화로
    무시한다. 유효하지 않은 값은 3종 유효 값을 나열해 거부한다.
    """
    if "capabilities" not in data:
        return None
    value = data["capabilities"]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"team config {file_path}: {prefix}.capabilities: expected a list of "
            f"strings, got {value!r}"
        )
    capabilities: set[AgentCapability] = set()
    for raw in value:
        try:
            capabilities.add(AgentCapability(raw))
        except ValueError:
            valid = ", ".join(c.value for c in AgentCapability)
            raise ConfigError(
                f"team config {file_path}: {prefix}.capabilities: invalid value "
                f"{raw!r}, expected one of: {valid}"
            ) from None
    return frozenset(capabilities)


def _parse_agent(data: Any, *, file_path: Path, index: int) -> AgentSpec:
    """agents 리스트의 항목 하나(에이전트 명세)를 파싱한다."""
    prefix = f"agents[{index}]"
    if not isinstance(data, dict):
        raise ConfigError(
            f"team config {file_path}: {prefix}: expected a mapping, got {data!r}"
        )
    _reject_unknown_keys(data, _AGENT_KEYS, file_path=file_path, prefix=prefix)

    name = _require_str(data, "name", file_path=file_path, prefix=prefix)
    role = _require_str(data, "role", file_path=file_path, prefix=prefix)
    system_prompt = _require_str(data, "system_prompt", file_path=file_path, prefix=prefix)

    model_raw = data.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise ConfigError(
            f"team config {file_path}: {prefix}.model: expected a string, got {model_raw!r}"
        )
    model = model_raw

    # 주의: 여기서 임시 AgentSpec을 만들어 기본값을 읽으면 이름 규칙 위반 등이
    # try 밖에서 ContractError로 새어 나간다 — 생략 시 kwargs에서 빼서 계약 기본값을 쓴다.
    max_turns = _optional_int(
        data, "max_turns", file_path=file_path, prefix=prefix, default=None
    )
    capabilities = _parse_capabilities(data, file_path=file_path, prefix=prefix)
    extra_kwargs: dict[str, Any] = {}
    if max_turns is not None:
        extra_kwargs["max_turns"] = max_turns
    if capabilities is not None:
        extra_kwargs["capabilities"] = capabilities

    try:
        return AgentSpec(
            name=name,
            role=role,
            system_prompt=system_prompt,
            model=model,
            **extra_kwargs,
        )
    except ContractError as e:
        raise ConfigError(f"team config {file_path}: {prefix}: {e}") from e


def load_team_config(path: str | Path) -> TeamConfig:
    """팀 설정 YAML 1개를 읽어 TeamConfig로 변환한다.

    실패 시 항상 ConfigError를 낸다 (파일 없음/문법 오류/스키마 위반/계약 위반 포함).
    """
    file_path = Path(path)

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"team config {file_path}: cannot read file: {e}") from e

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"team config {file_path}: invalid YAML syntax: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(
            f"team config {file_path}: root: expected a mapping, got {type(raw).__name__}"
        )

    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, file_path=file_path, prefix="root")

    name = _require_str(raw, "name", file_path=file_path, prefix="root")
    description = _optional_str(
        raw, "description", file_path=file_path, prefix="root", default=""
    )

    default_model_raw = raw.get("default_model")
    default_model_kwargs: dict[str, Any] = {}
    if default_model_raw is not None:
        if not isinstance(default_model_raw, str) or not default_model_raw:
            raise ConfigError(
                f"team config {file_path}: root.default_model: expected a non-empty "
                f"string, got {default_model_raw!r}"
            )
        default_model_kwargs["default_model"] = default_model_raw

    termination = _parse_termination(raw.get("termination"), file_path=file_path)

    agents_raw = raw.get("agents")
    if agents_raw is None:
        raise ConfigError(f"team config {file_path}: agents: missing required key")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ConfigError(
            f"team config {file_path}: agents: expected a non-empty list, got {agents_raw!r}"
        )

    agents = tuple(
        _parse_agent(item, file_path=file_path, index=i)
        for i, item in enumerate(agents_raw)
    )

    try:
        return TeamConfig(
            name=name,
            agents=agents,
            description=description,
            termination=termination,
            **default_model_kwargs,
        )
    except ContractError as e:
        raise ConfigError(f"team config {file_path}: {e}") from e


def list_team_configs(dir_path: str | Path) -> list[TeamConfig]:
    """디렉터리 안의 모든 *.yaml 팀 설정을 파일명 순으로 로드한다."""
    directory = Path(dir_path)
    if not directory.is_dir():
        raise ConfigError(f"team config directory {directory}: not a directory or missing")

    paths = sorted(directory.glob("*.yaml"), key=lambda p: p.name)
    return [load_team_config(p) for p in paths]
