"""ChatGPT 구독(Codex OAuth) 토큰 프로바이더 — chatgpt_oauth 인증 모드 (D-026).

ChatGPT Plus/Pro 구독으로 Responses API를 호출하는 **비공식** 경로. Codex의
device code flow로 사용자가 자기 계정에 로그인하고, 발급된 토큰을 로컬에 저장했다가
구독 백엔드(https://chatgpt.com/backend-api/codex)에 Bearer로 실어 보낸다. 사용자
소유 계정에 대한 정당한 인증 통합이며, 약관 변경 시 즉시 제거될 수 있다(D-026).

엔드포인트·client_id·헤더·거부 필드는 litellm 공개 소스에서 확정했다
(litellm/llms/chatgpt, branch litellm_internal_staging):
- device code:  POST auth.openai.com/api/accounts/deviceauth/usercode {client_id}
- device poll:  POST auth.openai.com/api/accounts/deviceauth/token {device_auth_id, user_code}
                200=완료 / 403·404=대기 / 그 외=거부·만료
- verify URL:   auth.openai.com/codex/device (사용자가 코드 입력)
- token 교환:   POST auth.openai.com/oauth/token (form: authorization_code grant)
- token 갱신:   POST auth.openai.com/oauth/token (json: refresh_token grant)
- account id:   JWT claim "https://api.openai.com/auth"."chatgpt_account_id"

보안 원칙: 토큰 값(access/refresh/id)을 로그·예외 메시지·repr에 절대 싣지 않는다.
네트워크는 openai SDK가 이미 의존하는 httpx로 처리한다(신규 의존성 아님). 이 모듈의
네트워크는 요청 핫패스가 아닌 인증 설정 시점에서만 동작하므로 동기 httpx를 쓴다.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hwabaek.llm.base import LLMAuthError

# --- litellm 소스에서 확정한 상수 (litellm/llms/chatgpt/common_utils.py) --------
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_AUTH_BASE = "https://auth.openai.com"
CHATGPT_DEVICE_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CHATGPT_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CHATGPT_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
CHATGPT_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
# 토큰 교환의 redirect_uri (authenticator.py: {AUTH_BASE}/deviceauth/callback).
CHATGPT_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CHATGPT_OAUTH_SCOPE = "openid profile email"
# 구독 백엔드 base_url — {api_base}/responses 로 호출한다.
CHATGPT_API_BASE = "https://chatgpt.com/backend-api/codex"
# 요청 헤더(get_chatgpt_default_headers): Authorization(Bearer)은 SDK가 붙이고,
# 아래 3종은 default_headers로 openai_client가 주입한다.
CHATGPT_ACCOUNT_ID_HEADER = "ChatGPT-Account-Id"
DEFAULT_ORIGINATOR = "codex_cli_rs"
DEFAULT_USER_AGENT = "codex_cli_rs/0.0.0 (Unknown 0; unknown) unknown"

# account id가 실려 있는 JWT claim 경로.
_ACCOUNT_CLAIM_NAMESPACE = "https://api.openai.com/auth"
_ACCOUNT_CLAIM_KEY = "chatgpt_account_id"

# 토큰 파일 경로: 기본값 + 환경변수 재정의.
_ENV_AUTH_FILE = "HWABAEK_CHATGPT_AUTH_FILE"
_DEFAULT_AUTH_FILE = "~/.hwabaek/chatgpt_token.json"

# 로그인 유도 메시지(영어 ASCII, 토큰 미포함).
_LOGIN_REQUIRED_MSG = (
    "chatgpt login required: run python -m hwabaek.llm.chatgpt_auth login"
)

# device flow 폴링 상한(초) 및 HTTP 타임아웃.
_DEFAULT_LOGIN_TIMEOUT = 900.0
_HTTP_TIMEOUT = 30.0
# access token 만료 판정 skew(초) — 만료 직전이면 미리 갱신한다.
_EXPIRY_SKEW = 60.0


@dataclass(frozen=True)
class DeviceLogin:
    """device code flow 시작 결과 — 사용자에게 보여줄 URL·코드와 폴링용 핸들.

    user_code/device_auth_id는 토큰이 아니라 일회성 로그인 핸들이므로 노출해도 무방.
    """

    device_auth_id: str
    user_code: str
    verification_uri: str
    interval: int = 5


class ChatGPTTokenProvider:
    """Codex OAuth device flow 토큰 프로바이더.

    책임: 로그인 시작/완료, 토큰 파일 저장·로드, 만료 시 refresh, 유효 토큰 제공.
    토큰 값은 인스턴스 어디에도 공개 속성으로 두지 않으며 repr에도 싣지 않는다.
    테스트는 transport로 httpx.MockTransport를 주입해 실 네트워크 없이 밀폐 검증한다.
    """

    def __init__(
        self,
        *,
        auth_file: str | os.PathLike[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # transport는 밀폐 테스트 주입점 — 프로덕션에서는 None(실 네트워크).
        self._auth_file = _resolve_auth_file(auth_file)
        self._transport = transport

    def __repr__(self) -> str:  # 토큰 노출 방지 — 경로만 노출.
        return f"ChatGPTTokenProvider(auth_file={str(self._auth_file)!r})"

    @property
    def auth_file(self) -> str:
        """토큰 파일 경로(문자열)."""
        return str(self._auth_file)

    # -- 로그인 (device code flow) -----------------------------------------
    def start_login(self) -> DeviceLogin:
        """device code를 요청하고, 사용자에게 보여줄 URL·코드를 반환한다."""
        data = self._post_json(
            CHATGPT_DEVICE_CODE_URL,
            {"client_id": CHATGPT_CLIENT_ID},
            fail="chatgpt device code request failed",
        )
        user_code = data.get("user_code") or data.get("usercode")
        device_auth_id = data.get("device_auth_id")
        if not user_code or not device_auth_id:
            raise LLMAuthError("chatgpt device code response was incomplete")
        return DeviceLogin(
            device_auth_id=str(device_auth_id),
            user_code=str(user_code),
            verification_uri=CHATGPT_DEVICE_VERIFY_URL,
            interval=_coerce_interval(data.get("interval")),
        )

    def finish_login(
        self, login: DeviceLogin, *, timeout: float = _DEFAULT_LOGIN_TIMEOUT
    ) -> str | None:
        """사용자 승인을 폴링하고, 완료되면 토큰을 교환·저장한다.

        200=완료 / 403·404=대기(interval 후 재시도) / 그 외=거부·만료. 승인 완료 후
        authorization_code + code_verifier로 토큰을 교환한다. 반환값은 account_id.
        """
        deadline = time.time() + timeout
        while True:
            resp = self._raw_post_json(
                CHATGPT_DEVICE_TOKEN_URL,
                {
                    "device_auth_id": login.device_auth_id,
                    "user_code": login.user_code,
                },
            )
            if resp.status_code == 200:
                body = _json(resp)
                authorization_code = body.get("authorization_code")
                code_verifier = body.get("code_verifier")
                if not authorization_code or not code_verifier:
                    raise LLMAuthError("chatgpt login response was incomplete")
                break
            if resp.status_code in (403, 404):
                if time.time() >= deadline:
                    raise LLMAuthError("chatgpt login timed out; run login again")
                time.sleep(login.interval)
                continue
            raise LLMAuthError("chatgpt login was denied or failed")

        tokens = self._exchange_code(str(authorization_code), str(code_verifier))
        return self._persist(tokens)

    # -- 유효 토큰 제공 -----------------------------------------------------
    def get_auth(self) -> tuple[str, str | None]:
        """유효한 (access_token, account_id)를 반환한다(필요 시 refresh).

        토큰 없음/만료+refresh 실패 시 LLMAuthError. openai_client가 클라이언트 구성
        시 1회 호출한다.
        """
        data = self._ensure_valid()
        return data["access_token"], data.get("account_id")

    def get_access_token(self) -> str:
        """유효한 access_token만 반환한다(필요 시 refresh)."""
        return self._ensure_valid()["access_token"]

    def get_account_id(self) -> str | None:
        """저장된 account_id를 반환한다(필요 시 refresh)."""
        return self._ensure_valid().get("account_id")

    def _ensure_valid(self) -> dict[str, Any]:
        """토큰 파일을 로드하고, 만료됐으면 refresh해 유효 토큰 dict를 반환한다."""
        data = self._load()
        if not data or not data.get("access_token"):
            raise LLMAuthError(_LOGIN_REQUIRED_MSG)
        if _jwt_expired(data["access_token"]):
            data = self._refresh(data)
        return data

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        """refresh_token으로 access/id token을 갱신하고 저장한다."""
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise LLMAuthError(
                "chatgpt token expired and no refresh token; run login again"
            )
        resp = self._raw_post_json(
            CHATGPT_OAUTH_TOKEN_URL,
            {
                "client_id": CHATGPT_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": CHATGPT_OAUTH_SCOPE,
            },
        )
        if resp.status_code != 200:
            # 상태 코드만 노출 — 본문에 토큰이 섞일 수 있어 싣지 않는다.
            raise LLMAuthError(
                f"chatgpt token refresh failed (status={resp.status_code}); "
                "run login again"
            )
        body = _json(resp)
        access_token = body.get("access_token")
        if not access_token:
            raise LLMAuthError("chatgpt token refresh returned no access token")
        id_token = body.get("id_token") or data.get("id_token")
        refreshed = {
            "access_token": access_token,
            "id_token": id_token,
            # 새 refresh_token이 없으면 기존 것을 재사용.
            "refresh_token": body.get("refresh_token") or refresh_token,
        }
        self._persist(refreshed)
        return refreshed

    def _exchange_code(self, authorization_code: str, code_verifier: str) -> dict[str, Any]:
        """authorization_code를 access/refresh/id token으로 교환한다(form-encoded)."""
        try:
            with self._http_client() as client:
                resp = client.post(
                    CHATGPT_OAUTH_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": authorization_code,
                        "redirect_uri": CHATGPT_REDIRECT_URI,
                        "client_id": CHATGPT_CLIENT_ID,
                        "code_verifier": code_verifier,
                    },
                )
        except httpx.HTTPError:
            raise LLMAuthError("chatgpt token exchange failed (network)") from None
        if resp.status_code != 200:
            raise LLMAuthError(
                f"chatgpt token exchange failed (status={resp.status_code})"
            )
        body = _json(resp)
        access_token = body.get("access_token")
        if not access_token:
            raise LLMAuthError("chatgpt token exchange returned no access token")
        return {
            "access_token": access_token,
            "refresh_token": body.get("refresh_token"),
            "id_token": body.get("id_token"),
        }

    # -- 토큰 파일 I/O ------------------------------------------------------
    def _persist(self, tokens: dict[str, Any]) -> str | None:
        """토큰 dict에 account_id를 채워 파일에 저장한다. account_id를 반환한다.

        파일 권한: POSIX는 0o600(소유자 전용) 최선 노력. Windows는 chmod가 큰 효과가
        없어(문서화) 사용자 프로필 디렉터리 ACL에 의존한다.
        """
        account_id = _account_id_from(tokens.get("id_token"), tokens.get("access_token"))
        record = {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens.get("id_token"),
            "account_id": account_id,
        }
        path = self._auth_file
        path.parent.mkdir(parents=True, exist_ok=True)
        # 새 파일을 소유자 전용 권한으로 먼저 만든 뒤 기록(경합 창 최소화).
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
        except OSError:
            # 플랫폼(Windows)에서 모드 지정이 무시될 수 있음 — 최선 노력.
            pass
        path.write_text(json.dumps(record), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return account_id

    def _load(self) -> dict[str, Any] | None:
        """토큰 파일을 로드한다. 없거나 손상 시 None."""
        path = self._auth_file
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    # -- HTTP 헬퍼 ----------------------------------------------------------
    def _http_client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=_HTTP_TIMEOUT)
        return httpx.Client(timeout=_HTTP_TIMEOUT)

    def _post_json(self, url: str, payload: dict[str, Any], *, fail: str) -> dict[str, Any]:
        """JSON POST 후 200이면 본문 dict를 반환, 아니면 LLMAuthError(fail)."""
        resp = self._raw_post_json(url, payload)
        if resp.status_code != 200:
            raise LLMAuthError(f"{fail} (status={resp.status_code})")
        return _json(resp)

    def _raw_post_json(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """JSON POST 원시 응답을 반환한다(상태 코드 분기는 호출부 책임)."""
        try:
            with self._http_client() as client:
                return client.post(url, json=payload)
        except httpx.HTTPError:
            raise LLMAuthError("chatgpt auth request failed (network)") from None


# ---------------------------------------------------------------------------
# 순수 헬퍼 (네트워크·상태 없음)
# ---------------------------------------------------------------------------

def _resolve_auth_file(explicit: str | os.PathLike[str] | None) -> Path:
    """토큰 파일 경로 결정: 인자 > 환경변수 > 기본값(~ 확장)."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(_ENV_AUTH_FILE)
    if env:
        return Path(env)
    return Path(_DEFAULT_AUTH_FILE).expanduser()


def _coerce_interval(value: Any, *, default: int = 5) -> int:
    """폴링 간격을 정수 초로 정규화한다(문자열 "5" 등 허용, 음수 방지)."""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return default
    return interval if interval >= 0 else default


def _json(resp: httpx.Response) -> dict[str, Any]:
    """응답 본문을 dict로 파싱한다(비 dict/파싱 실패는 빈 dict)."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """JWT payload(가운데 세그먼트)를 서명 검증 없이 디코드한다.

    account_id·exp claim만 읽는 용도다(우리 OAuth 서버 응답을 신뢰). 손상 토큰은 {}.
    """
    try:
        payload_b64 = token.split(".")[1]
    except (AttributeError, IndexError):
        return {}
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _account_id_from(*tokens: Any) -> str | None:
    """id_token/access_token claim에서 chatgpt_account_id를 추출한다."""
    for token in tokens:
        if not token:
            continue
        claims = _decode_jwt_claims(token)
        auth = claims.get(_ACCOUNT_CLAIM_NAMESPACE)
        if isinstance(auth, dict):
            account_id = auth.get(_ACCOUNT_CLAIM_KEY)
            if account_id:
                return str(account_id)
    return None


def _jwt_expired(access_token: str, *, skew: float = _EXPIRY_SKEW) -> bool:
    """access_token의 exp claim으로 만료 여부를 판정한다.

    exp를 읽을 수 없으면 만료로 단정하지 않는다(백엔드 401에 위임) — 불필요한 refresh
    를 피한다.
    """
    exp = _decode_jwt_claims(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return time.time() >= float(exp) - skew


# ---------------------------------------------------------------------------
# CLI 로그인 진입점 — python -m hwabaek.llm.chatgpt_auth login
# ---------------------------------------------------------------------------

def _login_cli() -> int:
    """device flow를 대화형으로 수행한다(콘솔 출력 영어 ASCII)."""
    provider = ChatGPTTokenProvider()
    login = provider.start_login()
    print("To authorize hwabaek with your ChatGPT subscription account:")
    print(f"  1. Open this URL in a browser: {login.verification_uri}")
    print(f"  2. Enter this code: {login.user_code}")
    print("Waiting for authorization (this window will update when done)...")
    provider.finish_login(login)
    print("Login complete. Token saved to:")
    print(f"  {provider.auth_file}")
    print("Note: on Windows the token file relies on your user profile ACL.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점: `login` 서브커맨드만 지원한다."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "login":
        print("usage: python -m hwabaek.llm.chatgpt_auth login")
        return 2
    try:
        return _login_cli()
    except LLMAuthError as exc:
        # 예외 메시지에는 토큰이 없다(마스킹 원칙) — 그대로 출력해도 안전.
        print(f"Login failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
