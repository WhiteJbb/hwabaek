"""FakeLLMClient 및 응답 빌더 검증 — 밀폐 테스트 기반의 자기 검증.

밀폐 원칙: 외부 의존성·네트워크 없이 unittest만으로 구동한다. 여기서는 대역이
LLM 계약(base.py)을 정확히 따르는지, 그리고 계약 자체의 검증 규칙(LLMRequest/Turn)이
기대대로 동작하는지 함께 확인한다.
"""
from __future__ import annotations

import unittest

from hwabaek.contracts import ContractError, Usage
from hwabaek.llm.base import (
    Blame,
    LLMAuthError,
    LLMBadRequestError,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)
from hwabaek.llm.fake import FakeLLMClient, text_response, tool_response


def make_request(text: str = "hello") -> LLMRequest:
    """검증에 쓰는 최소 유효 요청."""
    return LLMRequest(
        model="fake-model",
        system_prompt="You are a test agent.",
        turns=(Turn(role=Role.USER, content=text),),
    )


class FakeLLMClientTest(unittest.IsolatedAsyncioTestCase):
    """대역의 스크립트 소비/기록/소진 동작 검증."""

    async def test_returns_responses_in_script_order(self) -> None:
        # 스크립트 순서대로 응답이 반환되어야 한다.
        r1 = text_response("first")
        r2 = tool_response("do_it", {"x": 1})
        r3 = text_response("third")
        fake = FakeLLMClient(script=[r1, r2, r3])

        self.assertIs(await fake.complete(make_request("a")), r1)
        self.assertIs(await fake.complete(make_request("b")), r2)
        self.assertIs(await fake.complete(make_request("c")), r3)

    async def test_records_requests_in_calls(self) -> None:
        # 받은 요청이 순서대로 calls에 기록되어야 한다.
        fake = FakeLLMClient(script=[text_response("ok"), text_response("ok2")])
        req_a = make_request("alpha")
        req_b = make_request("beta")

        await fake.complete(req_a)
        await fake.complete(req_b)

        self.assertEqual(fake.calls, [req_a, req_b])
        self.assertIs(fake.calls[0], req_a)
        self.assertIs(fake.calls[1], req_b)

    async def test_error_instances_are_raised_with_contract_blame(self) -> None:
        # 주입한 오류는 해당 타입 그대로 raise되고 blame/retryable이 계약과 일치해야 한다.
        cases = [
            (LLMBadRequestError, Blame.CLIENT, False),
            (LLMAuthError, Blame.CLIENT, False),
            (LLMRateLimitError, Blame.PROVIDER, True),
            (LLMServerError, Blame.PROVIDER, True),
            (LLMConnectionError, Blame.PROVIDER, True),
        ]
        for error_cls, expected_blame, expected_retryable in cases:
            with self.subTest(error=error_cls.__name__):
                injected = error_cls("boom")
                fake = FakeLLMClient(script=[injected])
                with self.assertRaises(error_cls) as ctx:
                    await fake.complete(make_request())
                # 같은 인스턴스가 그대로 전파되어야 한다.
                self.assertIs(ctx.exception, injected)
                # 계약이 정한 귀책/재시도 속성.
                self.assertIsInstance(ctx.exception, LLMError)
                self.assertEqual(ctx.exception.blame, expected_blame)
                self.assertEqual(ctx.exception.retryable, expected_retryable)
                # 실패한 호출도 기록되어야 한다.
                self.assertEqual(len(fake.calls), 1)

    async def test_script_exhaustion_raises_assertion_and_still_records(self) -> None:
        # 스크립트 소진 후 호출은 AssertionError로 실패하되, calls에는 기록되어야 한다.
        fake = FakeLLMClient(script=[text_response("only")])
        await fake.complete(make_request("one"))

        with self.assertRaises(AssertionError) as ctx:
            await fake.complete(make_request("two"))
        self.assertIn("script exhausted", str(ctx.exception))
        # 소진으로 실패한 호출도 순서 검증을 위해 기록된다.
        self.assertEqual(len(fake.calls), 2)

    async def test_empty_script_exhausted_on_first_call(self) -> None:
        # 빈 스크립트는 첫 호출부터 소진 상태다.
        fake = FakeLLMClient(script=[])
        with self.assertRaises(AssertionError):
            await fake.complete(make_request())
        self.assertEqual(len(fake.calls), 1)

    def test_is_instance_of_llm_client_protocol(self) -> None:
        # runtime_checkable Protocol 준수를 확인한다.
        fake = FakeLLMClient(script=[])
        self.assertIsInstance(fake, LLMClient)


class ResponseBuilderTest(unittest.IsolatedAsyncioTestCase):
    """응답 빌더 헬퍼 검증."""

    def test_text_response_shape(self) -> None:
        # 텍스트 응답은 stop=END, tool_calls 비어 있음, usage 기본값 비영(非零).
        resp = text_response("hi there")
        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.text, "hi there")
        self.assertEqual(resp.stop, StopReason.END)
        self.assertEqual(resp.tool_calls, ())
        self.assertEqual(resp.model, "fake-model")
        self.assertGreater(resp.usage.input_tokens, 0)
        self.assertGreater(resp.usage.output_tokens, 0)

    def test_text_response_custom_usage_and_model(self) -> None:
        # 사용자 지정 usage/model이 그대로 반영되어야 한다.
        usage = Usage(input_tokens=42, output_tokens=7)
        resp = text_response("hi", usage=usage, model="custom-model")
        self.assertIs(resp.usage, usage)
        self.assertEqual(resp.model, "custom-model")

    def test_tool_response_shape(self) -> None:
        # 도구 응답은 stop=TOOL_USE이고 tool_calls 1건을 담아야 한다.
        resp = tool_response("search", {"q": "cats"}, call_id="call-9", text="thinking")
        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.stop, StopReason.TOOL_USE)
        self.assertEqual(resp.text, "thinking")
        self.assertEqual(len(resp.tool_calls), 1)
        call = resp.tool_calls[0]
        self.assertIsInstance(call, ToolCall)
        self.assertEqual(call.id, "call-9")
        self.assertEqual(call.name, "search")
        self.assertEqual(call.arguments, {"q": "cats"})
        self.assertGreater(resp.usage.total_tokens, 0)

    def test_tool_response_defaults(self) -> None:
        # 기본 call_id와 빈 text 기본값 확인.
        resp = tool_response("noop", {})
        self.assertEqual(resp.tool_calls[0].id, "call-1")
        self.assertEqual(resp.text, "")

    async def test_builders_flow_through_fake(self) -> None:
        # 빌더가 만든 응답이 대역을 통해 그대로 흘러가야 한다.
        fake = FakeLLMClient(script=[tool_response("act", {"n": 3})])
        resp = await fake.complete(make_request())
        self.assertEqual(resp.stop, StopReason.TOOL_USE)
        self.assertEqual(resp.tool_calls[0].name, "act")


class LLMRequestContractTest(unittest.TestCase):
    """LLMRequest 자체 검증 규칙 — 대역이 다루는 요청 계약을 밀폐 검증."""

    def test_valid_request_is_accepted(self) -> None:
        req = make_request()
        self.assertEqual(req.model, "fake-model")
        # 기본값 확인.
        self.assertEqual(req.max_output_tokens, 4096)
        self.assertTrue(req.cache_system_prefix)

    def test_empty_model_rejected(self) -> None:
        with self.assertRaises(ContractError):
            LLMRequest(
                model="",
                system_prompt="sys",
                turns=(Turn(role=Role.USER, content="hi"),),
            )

    def test_empty_system_prompt_rejected(self) -> None:
        with self.assertRaises(ContractError):
            LLMRequest(
                model="m",
                system_prompt="",
                turns=(Turn(role=Role.USER, content="hi"),),
            )

    def test_empty_turns_rejected(self) -> None:
        with self.assertRaises(ContractError):
            LLMRequest(model="m", system_prompt="sys", turns=())

    def test_non_positive_max_output_tokens_rejected(self) -> None:
        with self.assertRaises(ContractError):
            LLMRequest(
                model="m",
                system_prompt="sys",
                turns=(Turn(role=Role.USER, content="hi"),),
                max_output_tokens=0,
            )

    def test_duplicate_tool_names_rejected(self) -> None:
        dup = ToolSpec(name="tool", description="d", input_schema={})
        with self.assertRaises(ContractError):
            LLMRequest(
                model="m",
                system_prompt="sys",
                turns=(Turn(role=Role.USER, content="hi"),),
                tools=(dup, ToolSpec(name="tool", description="d2", input_schema={})),
            )

    def test_distinct_tool_names_accepted(self) -> None:
        req = LLMRequest(
            model="m",
            system_prompt="sys",
            turns=(Turn(role=Role.USER, content="hi"),),
            tools=(
                ToolSpec(name="a", description="d", input_schema={}),
                ToolSpec(name="b", description="d", input_schema={}),
            ),
        )
        self.assertEqual(len(req.tools), 2)


class TurnContractTest(unittest.TestCase):
    """Turn 역할 규칙 검증 — 계약이 정한 불변식을 밀폐 확인."""

    def test_user_turn_with_tool_calls_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Turn(
                role=Role.USER,
                content="hi",
                tool_calls=(ToolCall(id="c1", name="t", arguments={}),),
            )

    def test_assistant_turn_with_tool_results_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Turn(
                role=Role.ASSISTANT,
                content="hi",
                tool_results=(ToolResult(tool_call_id="c1", content="r"),),
            )

    def test_empty_turn_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Turn(role=Role.USER)

    def test_assistant_turn_with_tool_calls_accepted(self) -> None:
        turn = Turn(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id="c1", name="t", arguments={}),),
        )
        self.assertEqual(turn.role, Role.ASSISTANT)
        self.assertEqual(len(turn.tool_calls), 1)

    def test_user_turn_with_tool_results_accepted(self) -> None:
        turn = Turn(
            role=Role.USER,
            tool_results=(ToolResult(tool_call_id="c1", content="r"),),
        )
        self.assertEqual(turn.role, Role.USER)
        self.assertEqual(len(turn.tool_results), 1)


if __name__ == "__main__":
    unittest.main()
