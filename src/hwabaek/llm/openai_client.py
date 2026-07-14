"""OpenAI Responses API 어댑터 — LLMClient 계약의 OpenAI 구현 (D-009, D-026).

M2a 범위: **api_key 모드만** 구현한다. ChatGPT 구독(chatgpt_oauth) 모드는 M2b에서
추가하되(D-026), 인증 분기가 나중에 끼워질 수 있도록 클라이언트 구성은 `_build_client`
한 곳에 모은다.

설계 원칙(밀폐 테스트 우선):
- 순수 매핑 함수(`build_request_payload` / `parse_response` / `map_error`)와 얇은 호출
  래퍼(`OpenAIClient.complete`)를 분리한다 — 매핑은 네트워크 없이 단위 검증한다.
- Responses API 시그니처는 openai SDK 2.45.0 타입(openai/types/responses/)에서 직접
  확인했다. 웹 문서는 403이라 SDK 소스가 진실이다.
- 오류는 LLMError 계층으로 정규화하며 귀책(blame)을 구분한다. 오류 메시지에는 원본
  SDK 메시지(본문에 API 키가 섞일 수 있음)를 싣지 않고 상태·타입 요약만 남긴다.
- API 키는 어떤 로그·repr·오류에도 노출하지 않는다.

SDK 사실 요약(2.45.0):
- `responses.create(instructions=..., input=[...], tools=[...], max_output_tokens=...)`.
  system_prompt는 `instructions`(top-level str), 대화는 `input` 아이템 배열.
- 도구 정의: `{"type": "function", "name", "description", "parameters"(JSON Schema),
  "strict"}`.
- 어시스턴트 도구 호출: input 아이템 `{"type": "function_call", "call_id", "name",
  "arguments"(JSON 문자열)}`. 도구 결과: `{"type": "function_call_output", "call_id",
  "output"(문자열)}`.
- 캐싱: gpt-5.6+는 `prompt_cache_options={"ttl": "30m", "mode": ...}`(top-level)와
  입력 텍스트 블록의 `prompt_cache_breakpoint={"mode": "explicit"}`(명시적 breakpoint)를
  지원한다. 최소 캐시 수명 30분.
- usage: `input_tokens`(캐시 포함 총 입력) / `input_tokens_details.cached_tokens`(캐시
  읽기) / `input_tokens_details.cache_write_tokens`(캐시 쓰기) / `output_tokens`.
"""
from __future__ import annotations

import json
from typing import Any

import openai

from hwabaek.contracts import Usage
from hwabaek.llm.base import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
    Role,
    StopReason,
    ToolCall,
)
from hwabaek.llm.chatgpt_auth import (
    CHATGPT_ACCOUNT_ID_HEADER,
    CHATGPT_API_BASE,
    DEFAULT_ORIGINATOR,
    DEFAULT_USER_AGENT,
    ChatGPTTokenProvider,
)

# 캐시 최소 수명 — SDK가 현재 허용하는 유일한 값("30m", Research §6).
_CACHE_TTL = "30m"

# 인증 모드(D-026) — api_key(기본·공식) | chatgpt_oauth(구독 device flow).
AUTH_API_KEY = "api_key"
AUTH_CHATGPT_OAUTH = "chatgpt_oauth"

# chatgpt_oauth(구독) 백엔드가 허용하는 top-level 필드 화이트리스트.
# litellm chatgpt/responses/transformation.py의 allowed_keys와 동일하게 확정했다 —
# 이 집합 밖의 필드(max_output_tokens, metadata, prompt_cache_options 등)는 구독
# 백엔드가 거부하므로 제거한다. 토큰 예산은 사후 집계로 강제한다(D-026: 사전 상한 불가).
_CHATGPT_ALLOWED_KEYS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "stream",
        "store",
        "include",
        "tools",
        "tool_choice",
        "reasoning",
        "previous_response_id",
        "truncation",
    }
)


# ---------------------------------------------------------------------------
# 요청 매핑 — LLMRequest -> responses.create kwargs
# ---------------------------------------------------------------------------

def build_request_payload(
    request: LLMRequest, *, auth_mode: str = AUTH_API_KEY
) -> dict[str, Any]:
    """LLMRequest를 responses.create 호출 kwargs(dict)로 변환한다.

    - system_prompt -> instructions
    - turns -> input 아이템 배열(function_call / function_call_output 왕복 포함)
    - tools -> function 도구 정의
    - cache_system_prefix -> prompt_cache_options + 접두사 끝 명시적 breakpoint

    auth_mode가 chatgpt_oauth면 구독 백엔드가 거부하는 top-level 필드
    (max_output_tokens, prompt_cache_options 등)를 화이트리스트로 제거한다 — 나머지
    매핑은 api_key 모드와 동일하다(순수 함수, 네트워크·시계 없음).
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system_prompt,
        "max_output_tokens": request.max_output_tokens,
        "input": _build_input(request.turns, cache_prefix=request.cache_system_prefix),
    }
    if request.tools:
        payload["tools"] = [_tool_param(tool) for tool in request.tools]
    if request.cache_system_prefix:
        # 캐싱을 명시적으로 opt-in한다(30분 최소 수명). 명시적 breakpoint는 접두사
        # 끝(첫 텍스트 블록)에 함께 부여했다 — _build_input 참조.
        payload["prompt_cache_options"] = {"ttl": _CACHE_TTL}
    if auth_mode == AUTH_CHATGPT_OAUTH:
        # 구독 백엔드는 화이트리스트 외 필드를 거부한다(litellm 확정) — 제거한다.
        payload = {k: v for k, v in payload.items() if k in _CHATGPT_ALLOWED_KEYS}
    return payload


def _tool_param(tool: Any) -> dict[str, Any]:
    """ToolSpec -> Responses API function 도구 정의.

    strict는 False로 둔다 — 우리 JSON Schema가 strict 모드 제약
    (additionalProperties:false 등)을 보장하지 않으므로 활성화하지 않는다.
    """
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": False,
    }


def _build_input(turns: Any, *, cache_prefix: bool) -> list[dict[str, Any]]:
    """Turn 시퀀스를 Responses API input 아이템 배열로 변환한다.

    - USER 턴: 직전 어시스턴트 도구 호출에 대한 tool_results(function_call_output)를
      먼저 싣고, 이어 사용자 텍스트 메시지를 싣는다.
    - ASSISTANT 턴: 텍스트 메시지를 먼저, 이어 tool_calls(function_call)를 싣는다.

    cache_prefix면 첫 텍스트 메시지 블록에 명시적 cache breakpoint를 부여해
    (instructions + tools + 고정 접두사)를 재사용 캐시 경계로 표시한다 — 시스템
    프롬프트/도구는 세션 내 고정이므로 이 경계까지가 안정 접두사다(Research §6).
    """
    items: list[dict[str, Any]] = []
    # 명시적 breakpoint를 아직 배치하지 않았는지 추적(첫 텍스트 블록에 1회만).
    breakpoint_pending = cache_prefix
    for turn in turns:
        if turn.role is Role.USER:
            for result in turn.tool_results:
                items.append(_function_call_output(result))
            if turn.content:
                item, breakpoint_pending = _message(
                    "user", turn.content, breakpoint_pending
                )
                items.append(item)
        else:  # Role.ASSISTANT
            if turn.content:
                item, breakpoint_pending = _message(
                    "assistant", turn.content, breakpoint_pending
                )
                items.append(item)
            for call in turn.tool_calls:
                items.append(_function_call(call))
    return items


def _message(
    role: str, text: str, place_breakpoint: bool
) -> tuple[dict[str, Any], bool]:
    """역할 메시지 input 아이템을 만든다.

    place_breakpoint면 구조화된 content 블록(input_text)에 명시적 cache breakpoint를
    부여하고 (아이템, False)를 반환한다 — 이후 호출은 breakpoint를 재배치하지 않는다.
    아니면 단순 문자열 content로 만들고 place_breakpoint를 그대로 흘려보낸다.
    """
    if place_breakpoint:
        content: Any = [
            {
                "type": "input_text",
                "text": text,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ]
        return {"role": role, "content": content}, False
    return {"role": role, "content": text}, place_breakpoint


def _function_call(call: ToolCall) -> dict[str, Any]:
    """어시스턴트의 도구 호출 -> function_call input 아이템(arguments는 JSON 문자열)."""
    return {
        "type": "function_call",
        "call_id": call.id,
        "name": call.name,
        "arguments": json.dumps(call.arguments),
    }


def _function_call_output(result: Any) -> dict[str, Any]:
    """도구 실행 결과 -> function_call_output input 아이템.

    Responses API에는 별도 오류 플래그가 없으므로, is_error인 결과는 output 앞에
    ASCII 마커를 붙여 모델이 실패를 인지하게 한다(정보 손실 방지).
    """
    output = result.content
    if result.is_error:
        output = f"[tool error] {output}"
    return {
        "type": "function_call_output",
        "call_id": result.tool_call_id,
        "output": output,
    }


# ---------------------------------------------------------------------------
# 응답 매핑 — Response -> LLMResponse
# ---------------------------------------------------------------------------

def parse_response(raw: Any) -> LLMResponse:
    """Responses API 응답을 정규화된 LLMResponse로 변환한다.

    output 아이템을 훑어 텍스트/도구 호출/거부를 수집하고, status·incomplete_details로
    stop 사유를 정규화한 뒤 usage를 contracts.Usage로 매핑한다.
    """
    text_parts: list[str] = []
    raw_calls: list[Any] = []
    refusal = False

    for item in getattr(raw, "output", None) or ():
        itype = getattr(item, "type", None)
        if itype == "message":
            for block in getattr(item, "content", None) or ():
                btype = getattr(block, "type", None)
                if btype == "output_text":
                    text_parts.append(block.text)
                elif btype == "refusal":
                    refusal = True
        elif itype == "function_call":
            raw_calls.append(item)

    stop = _normalize_stop(raw, has_tool_calls=bool(raw_calls), refusal=refusal)

    # 계약: TOOL_USE만 tool_calls를 실을 수 있다. 절단(MAX_TOKENS)·거부 시 부분 도구
    # 호출은 사용 불가하다 — arguments 파싱 자체를 생략해 잘린 JSON에서도 안전하게
    # 버린다(실패 경로 강건성).
    if stop is StopReason.TOOL_USE:
        tool_calls = tuple(
            ToolCall(
                id=item.call_id,
                name=item.name,
                arguments=_parse_arguments(item.arguments),
            )
            for item in raw_calls
        )
    else:
        tool_calls = ()

    return LLMResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        stop=stop,
        usage=_map_usage(getattr(raw, "usage", None)),
        model=str(getattr(raw, "model", "") or ""),
    )


def _parse_arguments(raw_arguments: str) -> dict[str, Any]:
    """function_call.arguments(JSON 문자열)를 dict로 파싱한다(빈 문자열은 빈 인자)."""
    if not raw_arguments or not raw_arguments.strip():
        return {}
    return json.loads(raw_arguments)


def _normalize_stop(raw: Any, *, has_tool_calls: bool, refusal: bool) -> StopReason:
    """stop 사유 정규화.

    우선순위:
    1. 절단(incomplete: max_output_tokens 또는 사유 미상) -> MAX_TOKENS
    2. 콘텐츠 필터(incomplete: content_filter) -> REFUSAL
    3. 모델 거부(refusal 블록) -> REFUSAL
    4. 도구 호출 존재 -> TOOL_USE
    5. 그 외(정상 완료) -> END

    절단을 도구 호출보다 우선한다 — 정상적인 도구 호출 응답은 status=completed이며,
    절단 시의 부분 function_call은 arguments가 불완전해 신뢰할 수 없다.
    """
    incomplete = getattr(raw, "incomplete_details", None)
    reason = getattr(incomplete, "reason", None) if incomplete is not None else None
    status = getattr(raw, "status", None)

    if reason == "max_output_tokens":
        return StopReason.MAX_TOKENS
    if reason == "content_filter":
        return StopReason.REFUSAL
    if status == "incomplete":
        # 사유가 명시되지 않은 incomplete도 절단으로 취급한다.
        return StopReason.MAX_TOKENS
    if refusal:
        return StopReason.REFUSAL
    if has_tool_calls:
        return StopReason.TOOL_USE
    return StopReason.END


def _map_usage(usage: Any) -> Usage:
    """ResponseUsage -> contracts.Usage(비중첩 4버킷).

    OpenAI의 `input_tokens`는 캐시 읽기/쓰기를 포함한 총 입력이다. hwabaek Usage는
    네 필드를 서로 겹치지 않게 합산(total_tokens)하므로, 캐시분을 제외한 신규 입력만
    input_tokens에 싣는다:
        input(신규) = input_tokens - cached_tokens - cache_write_tokens
        cache_read  = cached_tokens
        cache_write = cache_write_tokens
    이 분해로 Usage.total_tokens == OpenAI usage.total_tokens(=input+output)가 되어
    예산 집계가 프로바이더 총계와 일치한다(Research §5의 프로바이더별 집계 차이 대응).
    """
    if usage is None:
        return Usage()
    input_total = getattr(usage, "input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    details = getattr(usage, "input_tokens_details", None)
    cache_read = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    cache_write = (
        getattr(details, "cache_write_tokens", 0) or 0 if details is not None else 0
    )
    fresh_input = max(0, input_total - cache_read - cache_write)
    return Usage(
        input_tokens=fresh_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


# ---------------------------------------------------------------------------
# 오류 매핑 — openai 예외 -> LLMError 계층 (귀책 구분 + 키 마스킹)
# ---------------------------------------------------------------------------

def map_error(exc: BaseException) -> LLMError:
    """openai SDK 예외를 LLMError 계층으로 정규화한다.

    원본 메시지(본문에 API 키가 섞일 수 있음)는 싣지 않고, 상태 코드·오류 타입/코드·
    예외 클래스명만 요약한다(_summary). 하위 타입 우선순위에 유의:
    - APITimeoutError는 APIConnectionError의 서브클래스 -> 먼저 검사.
    - BadRequest/Auth/Permission/RateLimit/InternalServer는 APIStatusError 서브클래스
      -> 일반 APIStatusError 폴백보다 먼저 검사.
    """
    if isinstance(exc, openai.BadRequestError):
        return LLMBadRequestError(_summary("bad request", exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthError(_summary("authentication or permission denied", exc))
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError(_summary("rate limit exceeded", exc))
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeoutError(_summary("request timed out", exc))
    if isinstance(exc, openai.APIConnectionError):
        return LLMConnectionError(_summary("connection error", exc))
    if isinstance(exc, openai.InternalServerError):
        return LLMServerError(_summary("server error", exc))
    if isinstance(exc, openai.APIStatusError):
        # 열거되지 않은 상태 코드: 5xx는 프로바이더, 그 외 4xx는 클라이언트 귀책.
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 500 <= status < 600:
            return LLMServerError(_summary("server error", exc))
        return LLMBadRequestError(_summary("client error", exc))
    # 알 수 없는 SDK 오류: 보수적으로 프로바이더 귀책(재시도 대상)으로 둔다.
    return LLMServerError(_summary("unexpected provider error", exc))


def _summary(label: str, exc: BaseException) -> str:
    """키 노출 없이 오류를 요약한다 — 상태 코드/오류 타입·코드/예외 클래스명만 사용.

    원본 메시지(str(exc), exc.message)나 응답 본문은 절대 포함하지 않는다. exc.type/
    exc.code는 "invalid_request_error"/"invalid_api_key" 같은 짧은 식별자로 키 값을
    담지 않는다.
    """
    parts = [f"OpenAI {label}"]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        parts.append(f"status={status}")
    etype = getattr(exc, "type", None)
    if isinstance(etype, str) and etype:
        parts.append(f"type={etype}")
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    parts.append(f"exc={type(exc).__name__}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------

class OpenAIClient:
    """OpenAI Responses API 기반 LLMClient 구현 — 인증 모드 2종(D-026).

    - api_key(기본·공식): OPENAI_API_KEY 또는 주입 키로 표준 API 호출.
    - chatgpt_oauth(선택·비공식): ChatGPT 구독 device flow 토큰으로 구독 백엔드 호출.
      구독 백엔드가 거부하는 필드는 payload에서 제거하고(build_request_payload),
      토큰 예산은 사후 집계로만 강제한다.

    재시도는 SDK 기본(max_retries=2)에 맡기고 자체 재시도 루프는 두지 않는다.
    스트리밍은 쓰지 않고 완성된 응답 1건을 반환한다(계약 준수).
    """

    def __init__(
        self,
        auth_mode: str = AUTH_API_KEY,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        token_provider: ChatGPTTokenProvider | None = None,
    ) -> None:
        # 인증 분기(D-026: api_key | chatgpt_oauth). 클라이언트 구성은 모드별 빌더
        # 한 곳에 모으고, complete/매핑 경로는 auth_mode만 참조한다(핫패스 무분기).
        self._auth_mode = auth_mode
        if auth_mode == AUTH_CHATGPT_OAUTH:
            self._client = self._build_chatgpt_client(
                token_provider=token_provider, base_url=base_url
            )
        elif auth_mode == AUTH_API_KEY:
            self._client = self._build_client(api_key=api_key, base_url=base_url)
        else:
            raise LLMAuthError(f"unknown auth_mode: {auth_mode!r}")

    @staticmethod
    def _build_client(*, api_key: str | None, base_url: str | None) -> Any:
        """AsyncOpenAI 클라이언트를 구성한다(api_key 모드).

        api_key 미지정 시 SDK가 OPENAI_API_KEY 환경변수를 사용한다. base_url은 지정
        시에만 넘긴다. 키는 클라이언트 내부에만 두고 어디에도 노출하지 않는다.
        """
        kwargs: dict[str, Any] = {}
        if api_key is not None:
            kwargs["api_key"] = api_key
        if base_url is not None:
            kwargs["base_url"] = base_url
        return openai.AsyncOpenAI(**kwargs)

    @staticmethod
    def _build_chatgpt_client(
        *, token_provider: ChatGPTTokenProvider | None, base_url: str | None
    ) -> Any:
        """AsyncOpenAI 클라이언트를 구독(chatgpt_oauth) 백엔드용으로 구성한다.

        토큰 프로바이더에서 유효 토큰·account_id를 받아(필요 시 refresh) Bearer 인증과
        계정 헤더를 구성한다. base_url·헤더 구성은 litellm chatgpt 프로바이더 소스에서
        확정했다. access_token은 SDK의 api_key로 전달돼 `Authorization: Bearer`로
        나가며(로그·repr 미노출), account_id 등 부가 헤더는 default_headers로 싣는다.

        주의(실측 필요): 토큰은 구성 시점에 1회 확보한다 — 세션 도중 만료 시 자동
        재발급은 하지 않는다(다음 세션에서 refresh). 구독 백엔드의 stream 강제·
        accept 헤더 요구는 실 로그인으로 검증해야 한다(README 고지).
        """
        provider = token_provider or ChatGPTTokenProvider()
        access_token, account_id = provider.get_auth()
        headers = {
            "originator": DEFAULT_ORIGINATOR,
            "user-agent": DEFAULT_USER_AGENT,
        }
        if account_id:
            headers[CHATGPT_ACCOUNT_ID_HEADER] = account_id
        return openai.AsyncOpenAI(
            api_key=access_token,
            base_url=base_url or CHATGPT_API_BASE,
            default_headers=headers,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """요청 1건을 수행하고 정규화된 응답을 반환한다.

        payload 빌드 -> responses.create -> parse. SDK 예외는 map_error로 변환해
        raise하며, 원본 예외를 체이닝하지 않는다(`from None`) — 원본 메시지에 섞일 수
        있는 API 키가 트레이스백에 노출되지 않게 한다.
        """
        payload = build_request_payload(request, auth_mode=self._auth_mode)
        try:
            raw = await self._client.responses.create(**payload)
        except openai.OpenAIError as exc:
            raise map_error(exc) from None
        return parse_response(raw)
