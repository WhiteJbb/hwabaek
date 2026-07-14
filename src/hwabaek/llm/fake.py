"""테스트용 Fake LLM 클라이언트 — M2 이후 밀폐 테스트의 기반 (D-009).

실키·실네트워크에 의존하지 않고 LLMClient 계약을 만족하는 결정적 대역이다.
호출마다 미리 정의한 스크립트를 순서대로 소비하며, 응답 반환 또는 오류 raise를
결정한다. 받은 요청은 모두 기록해 호출 순서·인자를 검증할 수 있게 한다.

주의: 모든 값은 고정(datetime/random 금지) — 결정적 재현이 목적이다.
"""
from __future__ import annotations

from collections.abc import Sequence

from hwabaek.contracts import Usage
from hwabaek.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    StopReason,
    ToolCall,
)

# usage 기본값 — 사용량 집계 테스트가 0이 아닌 값을 기대하므로 작은 고정값을 쓴다.
_DEFAULT_USAGE = Usage(input_tokens=10, output_tokens=5)


class FakeLLMClient:
    """스크립트 기반 결정적 LLMClient 구현 (LLMClient Protocol 만족).

    - script: complete() 호출마다 앞에서부터 하나씩 소비한다. LLMResponse면
      반환하고, LLMError 인스턴스면 그대로 raise한다.
    - calls: 받은 요청을 순서대로 기록한다. 스크립트 소진으로 실패한 호출도
      기록한다(요청 자체는 유효하게 도착했기 때문).
    - 스크립트 소진 후 호출되면 AssertionError로 즉시 실패시켜 테스트 누락을 드러낸다.
    """

    def __init__(self, script: Sequence[LLMResponse | LLMError]) -> None:
        # 방어적 복사 — 호출자가 넘긴 시퀀스 변경이 대역 상태에 새지 않게 한다.
        self._script: list[LLMResponse | LLMError] = list(script)
        self._index = 0
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """요청 1건을 스크립트에 따라 처리한다.

        요청은 처리 성패와 무관하게 먼저 calls에 기록한다 — 소진으로 실패한
        호출도 기록되어야 순서 검증이 정확해진다.
        """
        self.calls.append(request)
        if self._index >= len(self._script):
            raise AssertionError(
                f"FakeLLMClient script exhausted after {len(self._script)} calls"
            )
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, LLMError):
            raise item
        return item


def text_response(
    text: str,
    *,
    usage: Usage | None = None,
    model: str = "fake-model",
) -> LLMResponse:
    """텍스트만 담은 정상 종료 응답(stop=END)을 만든다."""
    return LLMResponse(
        text=text,
        tool_calls=(),
        stop=StopReason.END,
        usage=usage if usage is not None else _DEFAULT_USAGE,
        model=model,
    )


def tool_response(
    name: str,
    arguments: dict,
    *,
    call_id: str = "call-1",
    text: str = "",
    usage: Usage | None = None,
    model: str = "fake-model",
) -> LLMResponse:
    """도구 호출 1건을 요청하는 응답(stop=TOOL_USE)을 만든다."""
    return LLMResponse(
        text=text,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        stop=StopReason.TOOL_USE,
        usage=usage if usage is not None else _DEFAULT_USAGE,
        model=model,
    )
