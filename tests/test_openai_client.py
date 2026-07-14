"""OpenAIClient 어댑터 밀폐 검증 (M2a, D-009/D-026).

밀폐 원칙: 실 네트워크·실 API 키에 절대 의존하지 않는다. 순수 매핑 함수는 직접
호출로, complete()는 mock 클라이언트(AsyncMock)로 검증한다. 응답 매핑은 실제 openai
SDK pydantic 타입을 구성해 검증하여 계약 표류를 막는다.

- build_request_payload: 텍스트 턴 / 도구 정의 / 도구 왕복 / max_output_tokens / 캐싱
- parse_response: 텍스트 / function_call / incomplete / refusal / usage(캐시) 매핑
- map_error: 6종 매핑 + blame/category/retryable + API 키 마스킹
- complete(): mock 클라이언트로 성공·예외 경로
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai

from openai.types.responses.response import IncompleteDetails, Response
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

from hwabaek.contracts import ErrorCategory, Usage
from hwabaek.llm.base import (
    Blame,
    LLMAuthError,
    LLMBadRequestError,
    LLMClient,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)
from hwabaek.llm.openai_client import (
    OpenAIClient,
    build_request_payload,
    map_error,
    parse_response,
)

# 절대 오류 메시지에 새어나오면 안 되는 가짜 키 — 마스킹 검증용.
SECRET_KEY = "sk-test-DO-NOT-LEAK-abcdef123456"

MODEL = "gpt-5.6-terra"


# ---------------------------------------------------------------------------
# 응답/오류 픽스처 헬퍼 (실 SDK 타입 구성)
# ---------------------------------------------------------------------------

def _text_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_1",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


def _refusal_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_1",
        content=[ResponseOutputRefusal(refusal=text, type="refusal")],
        role="assistant",
        status="completed",
        type="message",
    )


def _function_call(name: str, arguments: str, call_id: str = "call_1") -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        arguments=arguments,
        call_id=call_id,
        name=name,
        type="function_call",
    )


def _usage(
    *, input_tokens: int, output_tokens: int, cached: int = 0, cache_write: int = 0
) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(
            cached_tokens=cached, cache_write_tokens=cache_write
        ),
        output_tokens=output_tokens,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=input_tokens + output_tokens,
    )


def _response(
    output: list,
    *,
    status: str = "completed",
    incomplete_details: IncompleteDetails | None = None,
    usage: ResponseUsage | None = None,
    model: str = MODEL,
) -> Response:
    # model_construct: parse_response가 읽는 필드만 채운다(밀폐, 검증 우회).
    return Response.model_construct(
        output=output,
        status=status,
        incomplete_details=incomplete_details,
        usage=usage,
        model=model,
    )


def _http_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls, status: int):
    """상태 오류를 만든다 — 메시지와 응답 본문 모두에 가짜 키를 심어 마스킹을 검증한다.

    body의 type/code는 키를 담지 않는 짧은 식별자다(요약에 실려도 안전).
    """
    resp = httpx.Response(status, request=_http_request())
    body = {
        "message": f"Invalid key {SECRET_KEY} rejected",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
    return cls(f"Error with {SECRET_KEY} in message", response=resp, body=body)


# ---------------------------------------------------------------------------
# build_request_payload
# ---------------------------------------------------------------------------

class BuildRequestPayloadTest(unittest.TestCase):
    def _req(self, **kwargs) -> LLMRequest:
        base = dict(
            model=MODEL,
            system_prompt="You are a test agent.",
            turns=(Turn(role=Role.USER, content="hello"),),
        )
        base.update(kwargs)
        return LLMRequest(**base)

    def test_text_turn_and_top_level_fields(self) -> None:
        payload = build_request_payload(self._req(max_output_tokens=256))
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["instructions"], "You are a test agent.")
        self.assertEqual(payload["max_output_tokens"], 256)
        self.assertNotIn("tools", payload)  # 도구 없으면 키 생략
        # 첫 텍스트 블록에 명시적 cache breakpoint가 붙는다(cache_system_prefix 기본 True).
        self.assertEqual(len(payload["input"]), 1)
        item = payload["input"][0]
        self.assertEqual(item["role"], "user")
        self.assertEqual(
            item["content"],
            [
                {
                    "type": "input_text",
                    "text": "hello",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        )
        self.assertEqual(payload["prompt_cache_options"], {"ttl": "30m"})

    def test_cache_disabled_uses_plain_string_and_no_options(self) -> None:
        payload = build_request_payload(self._req(cache_system_prefix=False))
        item = payload["input"][0]
        # breakpoint 없이 단순 문자열 content.
        self.assertEqual(item, {"role": "user", "content": "hello"})
        self.assertNotIn("prompt_cache_options", payload)

    def test_tool_definitions_mapping(self) -> None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        req = self._req(
            tools=(ToolSpec(name="search", description="Search the web", input_schema=schema),)
        )
        payload = build_request_payload(req)
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "name": "search",
                    "description": "Search the web",
                    "parameters": schema,
                    "strict": False,
                }
            ],
        )

    def test_tool_call_and_result_round_trip(self) -> None:
        turns = (
            Turn(role=Role.USER, content="find cats"),
            Turn(
                role=Role.ASSISTANT,
                content="on it",
                tool_calls=(ToolCall(id="call_1", name="search", arguments={"q": "cats"}),),
            ),
            Turn(
                role=Role.USER,
                tool_results=(ToolResult(tool_call_id="call_1", content="found cats"),),
            ),
        )
        payload = build_request_payload(self._req(turns=turns))
        items = payload["input"]
        self.assertEqual(len(items), 4)

        # 1) 첫 사용자 텍스트 — breakpoint 부여.
        self.assertEqual(items[0]["role"], "user")
        self.assertEqual(items[0]["content"][0]["text"], "find cats")
        self.assertIn("prompt_cache_breakpoint", items[0]["content"][0])

        # 2) 어시스턴트 텍스트 — breakpoint는 이미 배치되어 단순 문자열.
        self.assertEqual(items[1], {"role": "assistant", "content": "on it"})

        # 3) 어시스턴트 도구 호출 — arguments는 JSON 문자열.
        self.assertEqual(items[2]["type"], "function_call")
        self.assertEqual(items[2]["call_id"], "call_1")
        self.assertEqual(items[2]["name"], "search")
        self.assertEqual(json.loads(items[2]["arguments"]), {"q": "cats"})

        # 4) 도구 결과 — function_call_output.
        self.assertEqual(
            items[3],
            {"type": "function_call_output", "call_id": "call_1", "output": "found cats"},
        )

    def test_tool_result_error_is_flagged_in_output(self) -> None:
        turns = (
            Turn(role=Role.USER, content="go"),
            Turn(
                role=Role.ASSISTANT,
                tool_calls=(ToolCall(id="c1", name="t", arguments={}),),
            ),
            Turn(
                role=Role.USER,
                tool_results=(ToolResult(tool_call_id="c1", content="boom", is_error=True),),
            ),
        )
        payload = build_request_payload(self._req(turns=turns))
        outputs = [i for i in payload["input"] if i.get("type") == "function_call_output"]
        self.assertEqual(outputs[0]["output"], "[tool error] boom")


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------

class ParseResponseTest(unittest.TestCase):
    def test_text_response(self) -> None:
        raw = _response(
            [_text_message("Hello world")],
            usage=_usage(input_tokens=10, output_tokens=5),
        )
        resp = parse_response(raw)
        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.text, "Hello world")
        self.assertEqual(resp.tool_calls, ())
        self.assertEqual(resp.stop, StopReason.END)
        self.assertEqual(resp.model, MODEL)

    def test_function_call_response(self) -> None:
        raw = _response(
            [
                _text_message("let me check"),
                _function_call("get_weather", '{"city": "seoul"}', call_id="call_9"),
            ],
            usage=_usage(input_tokens=20, output_tokens=8),
        )
        resp = parse_response(raw)
        self.assertEqual(resp.stop, StopReason.TOOL_USE)
        self.assertEqual(resp.text, "let me check")
        self.assertEqual(len(resp.tool_calls), 1)
        call = resp.tool_calls[0]
        self.assertEqual(call.id, "call_9")
        self.assertEqual(call.name, "get_weather")
        self.assertEqual(call.arguments, {"city": "seoul"})

    def test_incomplete_max_output_tokens(self) -> None:
        raw = _response(
            [_text_message("partial answer that got cut")],
            status="incomplete",
            incomplete_details=IncompleteDetails(reason="max_output_tokens"),
            usage=_usage(input_tokens=10, output_tokens=100),
        )
        resp = parse_response(raw)
        self.assertEqual(resp.stop, StopReason.MAX_TOKENS)
        self.assertEqual(resp.tool_calls, ())
        self.assertEqual(resp.text, "partial answer that got cut")

    def test_truncated_partial_tool_call_is_dropped(self) -> None:
        # 절단 시 부분 function_call은 신뢰 불가 -> MAX_TOKENS, 도구 호출 버림(계약 준수).
        raw = _response(
            [_function_call("search", '{"q": "ca')],  # arguments가 잘렸다고 가정
            status="incomplete",
            incomplete_details=IncompleteDetails(reason="max_output_tokens"),
        )
        resp = parse_response(raw)
        self.assertEqual(resp.stop, StopReason.MAX_TOKENS)
        self.assertEqual(resp.tool_calls, ())

    def test_refusal_response(self) -> None:
        raw = _response([_refusal_message("I can't help with that")])
        resp = parse_response(raw)
        self.assertEqual(resp.stop, StopReason.REFUSAL)
        self.assertEqual(resp.text, "")
        self.assertEqual(resp.tool_calls, ())

    def test_content_filter_maps_to_refusal(self) -> None:
        raw = _response(
            [_text_message("")],
            status="incomplete",
            incomplete_details=IncompleteDetails(reason="content_filter"),
        )
        resp = parse_response(raw)
        self.assertEqual(resp.stop, StopReason.REFUSAL)

    def test_usage_with_cache_tokens_maps_to_non_overlapping_buckets(self) -> None:
        raw = _response(
            [_text_message("ok")],
            usage=_usage(
                input_tokens=1000, output_tokens=200, cached=400, cache_write=100
            ),
        )
        resp = parse_response(raw)
        # input(신규) = 1000 - 400(read) - 100(write) = 500.
        self.assertEqual(
            resp.usage,
            Usage(
                input_tokens=500,
                output_tokens=200,
                cache_read_tokens=400,
                cache_write_tokens=100,
            ),
        )
        # 예산 총계는 OpenAI total(input+output)과 일치해야 한다.
        self.assertEqual(resp.usage.total_tokens, 1200)

    def test_usage_none_yields_zero_usage(self) -> None:
        raw = _response([_text_message("ok")], usage=None)
        resp = parse_response(raw)
        self.assertEqual(resp.usage, Usage())


# ---------------------------------------------------------------------------
# map_error
# ---------------------------------------------------------------------------

class MapErrorTest(unittest.TestCase):
    def test_six_mappings_with_blame_category_retryable(self) -> None:
        cases = [
            (
                _status_error(openai.BadRequestError, 400),
                LLMBadRequestError,
                Blame.CLIENT,
                ErrorCategory.CLIENT_ERROR,
                False,
            ),
            (
                _status_error(openai.AuthenticationError, 401),
                LLMAuthError,
                Blame.CLIENT,
                ErrorCategory.CLIENT_ERROR,
                False,
            ),
            (
                _status_error(openai.PermissionDeniedError, 403),
                LLMAuthError,
                Blame.CLIENT,
                ErrorCategory.CLIENT_ERROR,
                False,
            ),
            (
                _status_error(openai.RateLimitError, 429),
                LLMRateLimitError,
                Blame.PROVIDER,
                ErrorCategory.RATE_LIMIT,
                True,
            ),
            (
                _status_error(openai.InternalServerError, 500),
                LLMServerError,
                Blame.PROVIDER,
                ErrorCategory.PROVIDER_ERROR,
                True,
            ),
            (
                openai.APITimeoutError(request=_http_request()),
                LLMTimeoutError,
                Blame.PROVIDER,
                ErrorCategory.TIMEOUT,
                True,
            ),
            (
                openai.APIConnectionError(
                    message=f"connect failed near {SECRET_KEY}", request=_http_request()
                ),
                LLMConnectionError,
                Blame.PROVIDER,
                ErrorCategory.PROVIDER_ERROR,
                True,
            ),
        ]
        for exc, expected_cls, blame, category, retryable in cases:
            with self.subTest(exc=type(exc).__name__):
                mapped = map_error(exc)
                self.assertIsInstance(mapped, expected_cls)
                self.assertEqual(mapped.blame, blame)
                self.assertEqual(mapped.category, category)
                self.assertEqual(mapped.retryable, retryable)
                # 키 마스킹: 요약 메시지에 가짜 키가 절대 없어야 한다.
                self.assertNotIn(SECRET_KEY, str(mapped))
                self.assertNotIn(SECRET_KEY, mapped.args[0])

    def test_timeout_checked_before_connection(self) -> None:
        # APITimeoutError는 APIConnectionError의 서브클래스 — 타임아웃으로 매핑돼야 한다.
        mapped = map_error(openai.APITimeoutError(request=_http_request()))
        self.assertIsInstance(mapped, LLMTimeoutError)

    def test_generic_5xx_status_maps_to_server(self) -> None:
        exc = _status_error(openai.APIStatusError, 503)
        mapped = map_error(exc)
        self.assertIsInstance(mapped, LLMServerError)
        self.assertNotIn(SECRET_KEY, str(mapped))

    def test_generic_4xx_status_maps_to_bad_request(self) -> None:
        exc = _status_error(openai.NotFoundError, 404)
        mapped = map_error(exc)
        self.assertIsInstance(mapped, LLMBadRequestError)
        self.assertNotIn(SECRET_KEY, str(mapped))

    def test_summary_includes_safe_status_and_type_but_not_message(self) -> None:
        mapped = map_error(_status_error(openai.BadRequestError, 400))
        summary = str(mapped)
        # 안전한 식별자는 실린다.
        self.assertIn("status=400", summary)
        self.assertIn("type=invalid_request_error", summary)
        self.assertIn("code=invalid_api_key", summary)
        # 원본 메시지(키 포함)는 실리지 않는다.
        self.assertNotIn(SECRET_KEY, summary)


# ---------------------------------------------------------------------------
# OpenAIClient.complete()
# ---------------------------------------------------------------------------

def _client_with_mock(create_mock: AsyncMock) -> OpenAIClient:
    # api_key를 명시 주입해 OPENAI_API_KEY 환경변수 없이도 구성되게 한다(밀폐).
    client = OpenAIClient(api_key="test-key")
    client._client = SimpleNamespace(responses=SimpleNamespace(create=create_mock))
    return client


def _sample_request() -> LLMRequest:
    return LLMRequest(
        model=MODEL,
        system_prompt="You are a test agent.",
        turns=(Turn(role=Role.USER, content="hi"),),
    )


class OpenAIClientCompleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_success_path(self) -> None:
        raw = _response(
            [_text_message("done")], usage=_usage(input_tokens=12, output_tokens=3)
        )
        create = AsyncMock(return_value=raw)
        client = _client_with_mock(create)

        resp = await client.complete(_sample_request())

        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.text, "done")
        self.assertEqual(resp.stop, StopReason.END)
        # payload가 올바른 kwargs로 전달됐는지 확인.
        create.assert_awaited_once()
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["model"], MODEL)
        self.assertEqual(kwargs["instructions"], "You are a test agent.")
        self.assertIn("input", kwargs)

    async def test_complete_maps_sdk_exception(self) -> None:
        create = AsyncMock(
            side_effect=_status_error(openai.RateLimitError, 429)
        )
        client = _client_with_mock(create)

        with self.assertRaises(LLMRateLimitError) as ctx:
            await client.complete(_sample_request())
        # 매핑된 오류에도 키가 노출되지 않는다.
        self.assertNotIn(SECRET_KEY, str(ctx.exception))

    async def test_complete_timeout_maps_to_llm_timeout(self) -> None:
        create = AsyncMock(side_effect=openai.APITimeoutError(request=_http_request()))
        client = _client_with_mock(create)
        with self.assertRaises(LLMTimeoutError):
            await client.complete(_sample_request())


# ---------------------------------------------------------------------------
# OpenAIClient.complete() — chatgpt_oauth 스트리밍 집계 (구독 백엔드가 stream 강제)
# ---------------------------------------------------------------------------

class _FakeStream:
    """responses.create(stream=True)가 돌려주는 AsyncStream의 최소 대역."""

    def __init__(self, events: list) -> None:
        self._events = list(events)
        self.closed = False

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        self.closed = True
        return False

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _StubTokenProvider:
    def get_auth(self) -> tuple[str, str | None]:
        return "fake-access-token", "acct_1"


def _oauth_client_with_stream(stream: _FakeStream) -> tuple[OpenAIClient, AsyncMock]:
    client = OpenAIClient(
        auth_mode="chatgpt_oauth", token_provider=_StubTokenProvider()
    )
    create = AsyncMock(return_value=stream)
    client._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client, create


def _stream_event(etype: str, response=None, item=None) -> SimpleNamespace:
    return SimpleNamespace(type=etype, response=response, item=item)


class OpenAIClientStreamingCompleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_oauth_complete_aggregates_completed_event(self) -> None:
        """chatgpt_oauth는 stream=True/store=False로 호출하고, 종결 이벤트의
        Response 스냅샷(usage 포함)을 완성 응답으로 되돌린다."""
        final = _response(
            [_text_message("done")], usage=_usage(input_tokens=12, output_tokens=3)
        )
        stream = _FakeStream([
            _stream_event("response.created"),
            _stream_event("response.in_progress"),
            _stream_event("response.completed", final),
        ])
        client, create = _oauth_client_with_stream(stream)

        resp = await client.complete(_sample_request())

        self.assertEqual(resp.text, "done")
        self.assertEqual(resp.stop, StopReason.END)
        self.assertEqual(resp.usage.output_tokens, 3)
        kwargs = create.await_args.kwargs
        self.assertIs(kwargs["stream"], True)
        self.assertIs(kwargs["store"], False)
        self.assertNotIn("max_output_tokens", kwargs)
        self.assertTrue(stream.closed)  # 종결 이벤트 후 스트림을 닫는다

    async def test_oauth_complete_backfills_empty_snapshot_from_done_items(self) -> None:
        """구독 백엔드는 종결 스냅샷에 output을 싣지 않는다(실측) —
        response.output_item.done의 완성 아이템으로 텍스트를 복원한다."""
        empty_final = _response(
            [], usage=_usage(input_tokens=28, output_tokens=7)
        )
        stream = _FakeStream([
            _stream_event("response.created"),
            _stream_event(
                "response.output_item.done", item=_text_message("annyeong!")
            ),
            _stream_event("response.completed", empty_final),
        ])
        client, _ = _oauth_client_with_stream(stream)

        resp = await client.complete(_sample_request())

        self.assertEqual(resp.text, "annyeong!")
        self.assertEqual(resp.stop, StopReason.END)
        # usage는 종결 스냅샷의 것을 그대로 쓴다.
        self.assertEqual(resp.usage.output_tokens, 7)

    async def test_oauth_complete_backfills_tool_calls_from_done_items(self) -> None:
        empty_final = _response([], usage=_usage(input_tokens=5, output_tokens=5))
        stream = _FakeStream([
            _stream_event(
                "response.output_item.done",
                item=_function_call("send_message", '{"recipients": ["*"]}'),
            ),
            _stream_event("response.completed", empty_final),
        ])
        client, _ = _oauth_client_with_stream(stream)

        resp = await client.complete(_sample_request())

        self.assertEqual(resp.stop, StopReason.TOOL_USE)
        self.assertEqual(resp.tool_calls[0].name, "send_message")
        self.assertEqual(resp.tool_calls[0].arguments, {"recipients": ["*"]})

    async def test_oauth_complete_prefers_snapshot_output_when_present(self) -> None:
        # 스냅샷에 output이 있으면(표준 API 동작) done 아이템 보강 없이 그대로 쓴다.
        final = _response([_text_message("from snapshot")])
        stream = _FakeStream([
            _stream_event(
                "response.output_item.done", item=_text_message("from delta")
            ),
            _stream_event("response.completed", final),
        ])
        client, _ = _oauth_client_with_stream(stream)

        resp = await client.complete(_sample_request())
        self.assertEqual(resp.text, "from snapshot")

    async def test_oauth_complete_incomplete_maps_to_max_tokens(self) -> None:
        final = _response(
            [_text_message("partial")],
            status="incomplete",
            incomplete_details=IncompleteDetails(reason="max_output_tokens"),
        )
        stream = _FakeStream([_stream_event("response.incomplete", final)])
        client, _ = _oauth_client_with_stream(stream)

        resp = await client.complete(_sample_request())
        self.assertEqual(resp.stop, StopReason.MAX_TOKENS)

    async def test_oauth_complete_failed_raises_server_error(self) -> None:
        """response.failed는 LLMServerError — error.message(본문)는 싣지 않는다."""
        final = _response([], status="failed")
        final.error = SimpleNamespace(code="server_error", message=SECRET_KEY)
        stream = _FakeStream([_stream_event("response.failed", final)])
        client, _ = _oauth_client_with_stream(stream)

        with self.assertRaises(LLMServerError) as ctx:
            await client.complete(_sample_request())
        self.assertIn("server_error", str(ctx.exception))
        self.assertNotIn(SECRET_KEY, str(ctx.exception))

    async def test_oauth_complete_without_terminal_event_raises(self) -> None:
        stream = _FakeStream([_stream_event("response.created")])
        client, _ = _oauth_client_with_stream(stream)
        with self.assertRaises(LLMServerError):
            await client.complete(_sample_request())

    async def test_api_key_mode_stays_non_streaming(self) -> None:
        raw = _response([_text_message("ok")])
        create = AsyncMock(return_value=raw)
        client = _client_with_mock(create)

        await client.complete(_sample_request())
        kwargs = create.await_args.kwargs
        self.assertNotIn("stream", kwargs)
        self.assertNotIn("store", kwargs)


class OpenAIClientConstructionTest(unittest.TestCase):
    def test_satisfies_llm_client_protocol(self) -> None:
        client = OpenAIClient(api_key="test-key")
        self.assertIsInstance(client, LLMClient)

    def test_api_key_not_exposed_in_repr(self) -> None:
        client = OpenAIClient(api_key=SECRET_KEY)
        self.assertNotIn(SECRET_KEY, repr(client))


if __name__ == "__main__":
    unittest.main()
