import base64
from typing import Dict
from urllib.parse import urlencode, urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class NobitexAuth(AuthBase):
    """
    Nobitex API-Key auth (Ed25519).

    Docs: https://apidocs.nobitex.ir/api_key/api-key-guide
    message = timestamp + METHOD + full_path + raw_body
    """

    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider
        self._private_key = self._load_private_key(secret_key)

    @staticmethod
    def _load_private_key(secret_key: str) -> Ed25519PrivateKey:
        padded = secret_key + "=" * (-len(secret_key) % 4)
        seed = base64.urlsafe_b64decode(padded)
        return Ed25519PrivateKey.from_private_bytes(seed)

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = dict(request.headers or {})
        headers.update(self.header_for_authentication(request))
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """Private WS uses /auth/ws/token/; connect auth is handled in the user stream."""
        return request

    def header_for_authentication(self, request: RESTRequest) -> Dict[str, str]:
        timestamp = str(int(self.time_provider.time()))
        path = self._path_for_signature(request)
        body = self._raw_body(request)
        return {
            "Nobitex-Key": self.api_key,
            "Nobitex-Signature": self._generate_signature(
                timestamp, request.method.value, path, body
            ),
            "Nobitex-Timestamp": timestamp,
            "User-Agent": CONSTANTS.USER_AGENT,
        }

    def _generate_signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        payload = f"{timestamp}{method}{path}{body}".encode("utf-8")
        signature = self._private_key.sign(payload)
        return base64.urlsafe_b64encode(signature).decode("ascii")

    @staticmethod
    def _raw_body(request: RESTRequest) -> str:
        if request.data is None:
            return ""
        if isinstance(request.data, bytes):
            return request.data.decode("utf-8")
        return str(request.data)

    @staticmethod
    def _path_for_signature(request: RESTRequest) -> str:
        # full_path = path + query (no host). Query may already be on url and/or in params.
        parsed = urlparse(request.url or "")
        path = parsed.path or ""
        query = parsed.query
        if request.params:
            extra = urlencode(request.params)
            query = f"{query}&{extra}" if query else extra
        return f"{path}?{query}" if query else path
