"""LLM 클라이언트 계약 — 프로바이더 중립 인터페이스 (D-009).

에이전트 런타임(agent.py)은 이 모듈의 타입만 사용한다. 프로바이더 특이사항
(파라미터 제약, stop 사유 표현, usage 필드 구조)은 각 어댑터(openai_client /
anthropic_client) 내부에 격리한다.

규칙:
- 표준 라이브러리 + contracts 외 의존성 금지 (SDK import는 어댑터에서만).
- 오류는 LLMError 계층으로 정규화하고, 귀책(blame)을 반드시 구분한다 —
  클라이언트의 잘못된 요청을 프로바이더 혼잡으로 기록하면 자가 치유가 오작동한다.
- 오류 메시지는 영어 ASCII. API 키를 메시지에 포함하지 않는다(마스킹).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from hwabaek.contracts import ContractError, ErrorCategory, Usage


class StopReason(str, enum.Enum):
    """응답 종료 사유 — 프로바이더별 표현을 어댑터가 이 값으로 정규화한다."""

    END = "end"                # 정상 완료
    TOOL_USE = "tool_use"      # 도구 호출 요청 — tool_results로 후속 호출 필요
    MAX_TOKENS = "max_tokens"  # 출력 상한 도달 (응답 절단)
    REFUSAL = "refusal"        # 모델 거부


class Role(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ToolSpec:
    """에이전트에 부여하는 도구 정의. input_schema는 JSON Schema(dict)."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ContractError("ToolSpec.name and description must be non-empty")
        if not isinstance(self.input_schema, dict):
            raise ContractError("ToolSpec.input_schema must be a dict (JSON Schema)")


@dataclass(frozen=True)
class ToolCall:
    """모델이 요청한 도구 호출 1건."""

    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ContractError("ToolCall.id and name must be non-empty")
        if not isinstance(self.arguments, dict):
            raise ContractError("ToolCall.arguments must be a dict")


@dataclass(frozen=True)
class ToolResult:
    """도구 실행 결과. 실패도 누락 없이 is_error=True로 반환한다."""

    tool_call_id: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not self.tool_call_id:
            raise ContractError("ToolResult.tool_call_id must be non-empty")


@dataclass(frozen=True)
class Turn:
    """대화의 한 턴 — 프로바이더 중립 표현.

    - USER 턴: content(발신자 태깅된 메시지 병합 — Plan 코어 의미론 §2) 그리고/또는
      직전 assistant 턴의 tool_calls에 대한 tool_results.
    - ASSISTANT 턴: content 그리고/또는 tool_calls.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    def __post_init__(self) -> None:
        if self.role is Role.USER and self.tool_calls:
            raise ContractError("user turn must not carry tool_calls")
        if self.role is Role.ASSISTANT and self.tool_results:
            raise ContractError("assistant turn must not carry tool_results")
        if not self.content and not self.tool_calls and not self.tool_results:
            raise ContractError("turn must carry content, tool_calls, or tool_results")


@dataclass(frozen=True)
class LLMRequest:
    """LLM 호출 1건의 요청.

    cache_system_prefix=True면 어댑터는 (tools + system_prompt) 접두사를 캐싱한다 —
    시스템 프롬프트는 고정하고 대화는 turns 뒤에만 추가하는 전략과 한 쌍 (Research §2/§6).
    """

    model: str
    system_prompt: str
    turns: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 4096
    cache_system_prefix: bool = True

    def __post_init__(self) -> None:
        if not self.model:
            raise ContractError("LLMRequest.model must be non-empty")
        if not self.system_prompt:
            raise ContractError("LLMRequest.system_prompt must be non-empty")
        if not self.turns:
            raise ContractError("LLMRequest.turns must be non-empty")
        if not isinstance(self.max_output_tokens, int) or self.max_output_tokens < 1:
            raise ContractError("LLMRequest.max_output_tokens must be a positive int")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ContractError("duplicate tool names in LLMRequest.tools")


@dataclass(frozen=True)
class LLMResponse:
    """정규화된 응답. usage는 contracts.Usage로 정규화된 값."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: StopReason
    usage: Usage
    model: str

    def __post_init__(self) -> None:
        if not self.model:
            raise ContractError("LLMResponse.model must be non-empty")
        if self.stop is StopReason.TOOL_USE and not self.tool_calls:
            raise ContractError("tool_use stop requires at least one tool call")
        if self.stop is not StopReason.TOOL_USE and self.tool_calls:
            raise ContractError(
                f"stop reason {self.stop.value!r} must not carry tool calls"
            )


# ---------------------------------------------------------------------------
# 오류 계층 — 귀책 구분 (CLAUDE.md 검증 원칙)
# ---------------------------------------------------------------------------

class Blame(str, enum.Enum):
    CLIENT = "client"      # 우리 요청이 잘못됨 — 재시도 무의미, 버그로 취급
    PROVIDER = "provider"  # 프로바이더 혼잡/장애 — 재시도 대상


class LLMError(Exception):
    """LLM 호출 실패의 공통 부모. 어댑터가 SDK 예외를 이 계층으로 정규화한다.

    category(세분 범주)와 retryable(재시도 가능 여부)은 분리해 기록한다 —
    오류 기록·이벤트에는 category를 쓰고, blame은 집계용 상위 구분이다.
    """

    blame: Blame = Blame.PROVIDER
    category: ErrorCategory = ErrorCategory.PROVIDER_ERROR
    retryable: bool = False


class LLMBadRequestError(LLMError):
    """4xx 계열 — 요청 구성이 잘못됨 (파라미터/스키마 오류)."""

    blame = Blame.CLIENT
    category = ErrorCategory.CLIENT_ERROR
    retryable = False


class LLMAuthError(LLMError):
    """인증/권한 실패 — 키 문제. 메시지에 키를 포함하지 않는다."""

    blame = Blame.CLIENT
    category = ErrorCategory.CLIENT_ERROR
    retryable = False


class LLMRateLimitError(LLMError):
    """429 — 재시도 대상이지만 세션 health 판정에서 모델 탓으로 돌리지 않는다."""

    blame = Blame.PROVIDER
    category = ErrorCategory.RATE_LIMIT
    retryable = True


class LLMServerError(LLMError):
    """5xx 계열."""

    blame = Blame.PROVIDER
    category = ErrorCategory.PROVIDER_ERROR
    retryable = True


class LLMTimeoutError(LLMError):
    """요청 시간 초과."""

    blame = Blame.PROVIDER
    category = ErrorCategory.TIMEOUT
    retryable = True


class LLMConnectionError(LLMError):
    """네트워크 연결 실패."""

    blame = Blame.PROVIDER
    category = ErrorCategory.PROVIDER_ERROR
    retryable = True


# ---------------------------------------------------------------------------
# 클라이언트 프로토콜
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    """프로바이더 중립 비동기 클라이언트.

    스트리밍은 어댑터 내부 구현 세부다(긴 응답의 타임아웃 회피용) — 계약은 완성된
    응답 1건을 반환한다. 대시보드 실시간성은 메시지 단위 SSE로 충분 (IA SC-03).
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """요청 1건을 수행하고 정규화된 응답을 반환한다. 실패는 LLMError 계층으로."""
        ...
