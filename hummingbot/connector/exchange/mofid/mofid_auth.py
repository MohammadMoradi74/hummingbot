from typing import Dict

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class MofidAuth(AuthBase):
    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        # api_key = Bearer JWT; secret_key = Lightstreamer LS_user (customer_isin).
        self.api_key = api_key
        self.ls_user = secret_key
        self.time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        # RESTAssistant stamps GET with application/x-www-form-urlencoded; WAF returns HTML 200.
        if request.method == RESTMethod.GET:
            headers.pop("Content-Type", None)
        headers.update(self.header_for_authentication())
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request

    def header_for_authentication(self) -> Dict[str, str]:
        # Browser-like headers required; bare aiohttp UA is blocked by WAF (HTML).
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-language": "fa",
            "Referer": "https://d.easytrader.ir/",
            "Origin": "https://d.easytrader.ir",
        }

    def ws_connect_headers(self) -> Dict[str, str]:
        """Lightstreamer WS handshake headers (no Bearer; protocol required or 403)."""
        from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS
        return {
            "Origin": "https://d.easytrader.ir",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            "Sec-WebSocket-Protocol": CONSTANTS.LS_WS_PROTOCOL,
        }
