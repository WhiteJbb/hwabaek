"""ChatGPTTokenProvider + openai_client chatgpt_oauth 모드 밀폐 검증 (M2b, D-026).

밀폐 원칙: 실 네트워크·실 계정에 절대 의존하지 않는다. device flow·토큰 교환·refresh
는 httpx.MockTransport로 응답을 위조해 검증한다. 토큰 값이 예외·repr에 새지 않는지
가짜 토큰 문자열로 단언한다.

- device flow 시작/폴링(pending·denied)/토큰 저장 왕복
- 만료 토큰 refresh 성공/실패(-> LLMAuthError)
- 토큰 미노출(repr·예외 메시지)
- chatgpt_oauth payload에서 금지 필드 제거 / api_key payload 무변경
- chatgpt_oauth 클라이언트 구성(base_url·계정 헤더·Bearer)
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from hwabaek.llm.base import (
    LLMAuthError,
    LLMRequest,
    Role,
    ToolSpec,
    Turn,
)
from hwabaek.llm.chatgpt_auth import (
    CHATGPT_API_BASE,
    CHATGPT_DEVICE_VERIFY_URL,
    ChatGPTTokenProvider,
)
from hwabaek.llm.openai_client import OpenAIClient, build_request_payload

MODEL = "gpt-5.6-terra"

# 절대 예외·repr에 새면 안 되는 가짜 토큰 — 마스킹 검증용.
FAKE_ACCESS = "fake-access-token-DO-NOT-LEAK-aaa"
FAKE_REFRESH = "fake-refresh-token-DO-NOT-LEAK-bbb"


# ---------------------------------------------------------------------------
# JWT 픽스처 (서명 검증 없는 base64url payload — 프로바이더가 읽는 claim만 채운다)
# ---------------------------------------------------------------------------

def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


def _access_jwt(account_id: str = "acct_test_123", *, exp_offset: float = 3600.0) -> str:
    """exp claim과 account claim을 담은 access/id 토큰용 JWT."""
    return _jwt(
        {
            "exp": time.time() + exp_offset,
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }
    )


# ---------------------------------------------------------------------------
# device flow / refresh 라운드트립
# ---------------------------------------------------------------------------

class ChatGPTTokenProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_file = os.path.join(self._tmp.name, "token.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _provider(self, handler) -> ChatGPTTokenProvider:
        return ChatGPTTokenProvider(
            auth_file=self.auth_file, transport=httpx.MockTransport(handler)
        )

    def _write_token(
        self, *, access: str, refresh: str | None, account_id: str, id_token=None
    ) -> None:
        Path(self.auth_file).write_text(
            json.dumps(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                    "account_id": account_id,
                }
            ),
            encoding="utf-8",
        )

    def test_device_flow_start_poll_persist_round_trip(self) -> None:
        state = {"polls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/deviceauth/usercode"):
                return httpx.Response(
                    200,
                    json={
                        "device_auth_id": "dev_abc",
                        "user_code": "WXYZ-1234",
                        "interval": 0,
                    },
                )
            if path.endswith("/deviceauth/token"):
                state["polls"] += 1
                if state["polls"] == 1:
                    return httpx.Response(403, json={})  # 아직 승인 전(pending)
                return httpx.Response(
                    200,
                    json={
                        "authorization_code": "authcode_1",
                        "code_challenge": "chal",
                        "code_verifier": "verifier_1",
                    },
                )
            if path.endswith("/oauth/token"):  # authorization_code 교환
                return httpx.Response(
                    200,
                    json={
                        "access_token": FAKE_ACCESS,
                        "refresh_token": FAKE_REFRESH,
                        "id_token": _access_jwt("acct_test_123"),
                    },
                )
            return httpx.Response(404, json={})

        provider = self._provider(handler)
        login = provider.start_login()
        self.assertEqual(login.user_code, "WXYZ-1234")
        self.assertEqual(login.verification_uri, CHATGPT_DEVICE_VERIFY_URL)
        self.assertEqual(login.interval, 0)

        account_id = provider.finish_login(login)
        self.assertEqual(account_id, "acct_test_123")
        self.assertGreaterEqual(state["polls"], 2)  # pending 후 success

        saved = json.loads(Path(self.auth_file).read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], FAKE_ACCESS)
        self.assertEqual(saved["refresh_token"], FAKE_REFRESH)
        self.assertEqual(saved["account_id"], "acct_test_123")

    def test_login_denied_raises_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/deviceauth/usercode"):
                return httpx.Response(
                    200,
                    json={"device_auth_id": "d", "user_code": "C-1", "interval": 0},
                )
            if path.endswith("/deviceauth/token"):
                return httpx.Response(400, json={"error": "access_denied"})
            return httpx.Response(404, json={})

        provider = self._provider(handler)
        login = provider.start_login()
        with self.assertRaises(LLMAuthError):
            provider.finish_login(login)

    def test_expired_token_refreshes_successfully(self) -> None:
        self._write_token(
            access=_access_jwt(exp_offset=-100.0),  # 이미 만료
            refresh=FAKE_REFRESH,
            account_id="acct_test_123",
        )
        new_access = _access_jwt("acct_test_123", exp_offset=3600.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth/token"):
                return httpx.Response(
                    200,
                    json={
                        "access_token": new_access,
                        "id_token": _access_jwt("acct_test_123"),
                    },
                )
            raise AssertionError("only the refresh endpoint should be called")

        provider = self._provider(handler)
        token = provider.get_access_token()
        self.assertEqual(token, new_access)

        saved = json.loads(Path(self.auth_file).read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], new_access)
        # 새 refresh_token이 없으면 기존 것을 재사용한다.
        self.assertEqual(saved["refresh_token"], FAKE_REFRESH)

    def test_expired_token_refresh_failure_raises_auth_error(self) -> None:
        self._write_token(
            access=_access_jwt(exp_offset=-100.0),
            refresh=FAKE_REFRESH,
            account_id="a",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            # 본문에 토큰을 심어도 오류 메시지에는 새지 않아야 한다(상태 코드만 노출).
            return httpx.Response(400, json={"error": "invalid_grant", "blob": FAKE_ACCESS})

        provider = self._provider(handler)
        with self.assertRaises(LLMAuthError) as ctx:
            provider.get_access_token()
        self.assertNotIn(FAKE_REFRESH, str(ctx.exception))
        self.assertNotIn(FAKE_ACCESS, str(ctx.exception))

    def test_missing_token_file_raises_login_required(self) -> None:
        provider = self._provider(lambda r: httpx.Response(404, json={}))
        with self.assertRaises(LLMAuthError) as ctx:
            provider.get_access_token()
        self.assertIn("login required", str(ctx.exception))

    def test_valid_token_skips_refresh(self) -> None:
        access = _access_jwt("acct_v", exp_offset=3600.0)
        self._write_token(access=access, refresh=FAKE_REFRESH, account_id="acct_v")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be called for a valid token")

        provider = self._provider(handler)
        token, account_id = provider.get_auth()
        self.assertEqual(token, access)
        self.assertEqual(account_id, "acct_v")

    def test_tokens_not_exposed_in_repr_or_errors(self) -> None:
        self._write_token(
            access=_access_jwt(exp_offset=-100.0),
            refresh=FAKE_REFRESH,
            account_id="a",
        )
        provider = self._provider(
            lambda r: httpx.Response(400, json={"blob": FAKE_ACCESS})
        )
        # repr에는 경로만, 토큰 값은 없다.
        self.assertNotIn(FAKE_ACCESS, repr(provider))
        self.assertNotIn(FAKE_REFRESH, repr(provider))
        with self.assertRaises(LLMAuthError) as ctx:
            provider.get_access_token()
        msg = str(ctx.exception)
        self.assertNotIn(FAKE_ACCESS, msg)
        self.assertNotIn(FAKE_REFRESH, msg)

    def test_auth_file_env_override(self) -> None:
        custom = os.path.join(self._tmp.name, "custom_token.json")
        old = os.environ.get("HWABAEK_CHATGPT_AUTH_FILE")
        os.environ["HWABAEK_CHATGPT_AUTH_FILE"] = custom
        try:
            provider = ChatGPTTokenProvider()
            self.assertEqual(Path(provider.auth_file), Path(custom))
        finally:
            if old is None:
                os.environ.pop("HWABAEK_CHATGPT_AUTH_FILE", None)
            else:
                os.environ["HWABAEK_CHATGPT_AUTH_FILE"] = old


# ---------------------------------------------------------------------------
# build_request_payload — chatgpt_oauth 금지 필드 제거 / api_key 무변경
# ---------------------------------------------------------------------------

class ChatGPTOAuthPayloadTest(unittest.TestCase):
    def _req(self, **kwargs) -> LLMRequest:
        base = dict(
            model=MODEL,
            system_prompt="You are a test agent.",
            turns=(Turn(role=Role.USER, content="hi"),),
        )
        base.update(kwargs)
        return LLMRequest(**base)

    def test_oauth_mode_strips_forbidden_fields(self) -> None:
        payload = build_request_payload(
            self._req(max_output_tokens=512), auth_mode="chatgpt_oauth"
        )
        # 구독 백엔드가 거부하는 top-level 필드는 제거된다.
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("prompt_cache_options", payload)
        self.assertNotIn("metadata", payload)
        # 허용 필드는 유지된다.
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["instructions"], "You are a test agent.")
        self.assertIn("input", payload)

    def test_oauth_mode_keeps_tools(self) -> None:
        schema = {"type": "object", "properties": {}}
        payload = build_request_payload(
            self._req(
                tools=(ToolSpec(name="t", description="d", input_schema=schema),)
            ),
            auth_mode="chatgpt_oauth",
        )
        self.assertIn("tools", payload)
        self.assertEqual(payload["tools"][0]["name"], "t")

    def test_api_key_mode_payload_unchanged(self) -> None:
        # 기본(api_key) 모드는 기존 동작 그대로 — 금지 필드가 그대로 남는다.
        payload = build_request_payload(self._req(max_output_tokens=512))
        self.assertEqual(payload["max_output_tokens"], 512)
        self.assertIn("prompt_cache_options", payload)  # cache_system_prefix 기본 True

    def test_default_auth_mode_is_api_key(self) -> None:
        # auth_mode 미지정 시 api_key 모드(금지 필드 유지)와 동일해야 한다.
        default_payload = build_request_payload(self._req(max_output_tokens=256))
        explicit_payload = build_request_payload(
            self._req(max_output_tokens=256), auth_mode="api_key"
        )
        self.assertEqual(default_payload, explicit_payload)


# ---------------------------------------------------------------------------
# OpenAIClient(auth_mode="chatgpt_oauth") 구성
# ---------------------------------------------------------------------------

class _StubProvider:
    """get_auth만 노출하는 최소 토큰 프로바이더 스텁(밀폐 — 파일·네트워크 없음)."""

    def __init__(self, token: str, account_id: str | None) -> None:
        self._token = token
        self._account_id = account_id

    def get_auth(self) -> tuple[str, str | None]:
        return self._token, self._account_id


class ChatGPTOAuthClientConstructionTest(unittest.TestCase):
    def test_construction_sets_backend_base_url_and_account_header(self) -> None:
        client = OpenAIClient(
            auth_mode="chatgpt_oauth",
            token_provider=_StubProvider(FAKE_ACCESS, "acct_9"),
        )
        inner = client._client
        self.assertEqual(str(inner.base_url).rstrip("/"), CHATGPT_API_BASE)
        headers = inner.default_headers
        self.assertEqual(headers.get("ChatGPT-Account-Id"), "acct_9")
        self.assertIn("originator", headers)
        self.assertIn("user-agent", headers)
        # access_token은 Bearer 인증으로만 쓰이고 repr에는 노출되지 않는다.
        self.assertEqual(inner.api_key, FAKE_ACCESS)
        self.assertNotIn(FAKE_ACCESS, repr(client))

    def test_construction_without_account_id_omits_header(self) -> None:
        client = OpenAIClient(
            auth_mode="chatgpt_oauth",
            token_provider=_StubProvider(FAKE_ACCESS, None),
        )
        self.assertNotIn("ChatGPT-Account-Id", client._client.default_headers)

    def test_unknown_auth_mode_raises(self) -> None:
        with self.assertRaises(LLMAuthError):
            OpenAIClient(auth_mode="nope")

    def test_login_required_propagates_on_construction(self) -> None:
        # 토큰 파일이 없으면 구성 시점에 LLMAuthError로 실패한다.
        with tempfile.TemporaryDirectory() as d:
            provider = ChatGPTTokenProvider(
                auth_file=os.path.join(d, "none.json"),
                transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})),
            )
            with self.assertRaises(LLMAuthError):
                OpenAIClient(auth_mode="chatgpt_oauth", token_provider=provider)


if __name__ == "__main__":
    unittest.main()
