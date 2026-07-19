import asyncio
import base64
import json
import time
from typing import Dict, Optional, Tuple

from hummingbot.connector.exchange.bitpin import bitpin_constants as CONSTANTS, bitpin_web_utils as web_utils
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest
from hummingbot.core.web_assistant.rest_assistant import RESTAssistant


class BitpinAuth(AuthBase):
    # Refresh slightly before JWT exp to avoid first-request 401s (~15m access TTL)
    _TOKEN_REFRESH_SKEW_SECONDS = 60.0

    def __init__(
            self,
            api_key: str,
            secret_key: str,
            time_provider: TimeSynchronizer,
            domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider
        self._domain = domain

        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._access_token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        if self._access_token is None:
            raise IOError("REST auth requires a RESTAssistant. Call ensure_authenticated() first.")

        headers = dict(request.headers or {})
        headers.update(self.header_for_authentication())
        request.headers = headers
        return request

    async def ensure_authenticated(self, rest_assistant: RESTAssistant) -> None:
        async with self._token_lock:
            if self._token_needs_refresh():
                if self._refresh_token is None:
                    await self._authenticate(rest_assistant)
                else:
                    await self._refresh_access_token(rest_assistant)

    async def authenticate(self, rest_assistant: RESTAssistant) -> None:
        await self.ensure_authenticated(rest_assistant)

    async def refresh_authenticate(self, rest_assistant: RESTAssistant, stale_token: Optional[str] = None) -> None:
        async with self._token_lock:
            # Another coroutine already replaced the token that got 401
            if stale_token is not None and self._access_token != stale_token:
                return
            await self._refresh_access_token(rest_assistant)

    async def get_ws_credentials(self, rest_assistant: RESTAssistant) -> Tuple[str, str]:
        """
        Fetch Centrifugo ws_token + user_identifier.
        NOT the same as REST access token.
        POST /usr/ws-info/ — credentials in body, no Bearer header.
        """
        data = await rest_assistant.execute_request(
            url=web_utils.private_rest_url(CONSTANTS.WS_INFO_PATH_URL, domain=self._domain),
            method=RESTMethod.POST,
            data={"api_key": self.api_key, "secret_key": self.secret_key},
            is_auth_required=False,
            throttler_limit_id=CONSTANTS.WS_INFO_PATH_URL,
        )
        ws_token = data["ws_token"]
        user_identifier = data["user_identifier"]
        return ws_token, user_identifier

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request  # Centrifugo connect token is handled in data source, not here

    def header_for_authentication(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }

    def _token_needs_refresh(self) -> bool:
        if self._access_token is None:
            return True
        # JWT exp is real Unix time — do NOT use TimeSynchronizer (Bitpin ticker units break it)
        return time.time() >= (self._access_token_expires_at - self._TOKEN_REFRESH_SKEW_SECONDS)

    @staticmethod
    def _parse_jwt_exp(token: str) -> float:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return float(claims["exp"])

    def _store_access_token(self, access_token: str) -> None:
        self._access_token = access_token
        self._access_token_expires_at = self._parse_jwt_exp(access_token)

    async def _authenticate(self, rest_assistant: RESTAssistant) -> None:
        data = await rest_assistant.execute_request(
            url=web_utils.private_rest_url(CONSTANTS.BITPIN_USER_STREAM_PATH_URL, domain=self._domain),
            method=RESTMethod.POST,
            data={"api_key": self.api_key, "secret_key": self.secret_key},
            is_auth_required=False,
            throttler_limit_id=CONSTANTS.BITPIN_USER_STREAM_PATH_URL,
        )
        self._store_access_token(data["access"])
        self._refresh_token = data["refresh"]

    async def _refresh_access_token(self, rest_assistant: RESTAssistant) -> None:
        if self._refresh_token is None:
            await self._authenticate(rest_assistant)
            return

        try:
            # Docs: POST /usr/refresh_token/ → {"access": "..."} only
            # https://docs.bitpin.ir/v1/docs/authentication/refresh_token
            data = await rest_assistant.execute_request(
                url=web_utils.private_rest_url(CONSTANTS.BITPIN_USER_STREAM_PATH_URL2, domain=self._domain),
                method=RESTMethod.POST,
                data={"refresh": self._refresh_token},
                is_auth_required=False,
                throttler_limit_id=CONSTANTS.BITPIN_USER_STREAM_PATH_URL2,
            )
            self._store_access_token(data["access"])
        except Exception:
            # Refresh token expired/invalid → full re-auth with api_key/secret
            await self._authenticate(rest_assistant)
