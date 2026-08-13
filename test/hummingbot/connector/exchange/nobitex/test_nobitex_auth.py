import base64
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS
from hummingbot.connector.exchange.nobitex.nobitex_auth import NobitexAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSJSONRequest


class NobitexAuthTests(IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        seed_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        self._secret = base64.urlsafe_b64encode(seed_bytes).decode("utf-8")
        self._api_key = base64.urlsafe_b64encode(public_key_bytes).decode("utf-8")
        self._private_key = private_key

        self.now = 1234567890.0
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = self.now

        self._auth = NobitexAuth(
            api_key=self._api_key,
            secret_key=self._secret,
            time_provider=mock_time_provider,
        )

    def _expected_signature(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        payload = f"{timestamp}{method}{path}{body}".encode("utf-8")
        return base64.urlsafe_b64encode(self._private_key.sign(payload)).decode("ascii")

    async def test_rest_authenticate(self):
        params = {"status": "all", "details": "2"}
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://apiv2.nobitex.ir/market/orders/list",
            params=params,
            is_auth_required=True,
        )
        configured = await self._auth.rest_authenticate(request)

        timestamp = str(int(self.now))
        path = "/market/orders/list?status=all&details=2"
        expected_sig = self._expected_signature(timestamp, "GET", path)

        self.assertEqual(self._api_key, configured.headers["Nobitex-Key"])
        self.assertEqual(timestamp, configured.headers["Nobitex-Timestamp"])
        self.assertEqual(expected_sig, configured.headers["Nobitex-Signature"])
        self.assertEqual(CONSTANTS.USER_AGENT, configured.headers["User-Agent"])
        self.assertEqual(params, configured.params)

    async def test_rest_authenticate_post_with_body(self):
        body = json.dumps({"currency": "usdt"}, separators=(",", ":"))
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://apiv2.nobitex.ir/users/wallets/balance",
            data=body,
            is_auth_required=True,
        )
        configured = await self._auth.rest_authenticate(request)

        timestamp = str(int(self.now))
        expected_sig = self._expected_signature(
            timestamp, "POST", "/users/wallets/balance", body
        )

        self.assertEqual(expected_sig, configured.headers["Nobitex-Signature"])
        self.assertEqual(body, configured.data)

    async def test_ws_authenticate_passthrough(self):
        request = WSJSONRequest(payload={"op": "subscribe"})
        configured = await self._auth.ws_authenticate(request)
        self.assertIs(request, configured)
