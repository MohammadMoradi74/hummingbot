from typing import Dict

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class MofidAuth(AuthBase):
    # Manual paste from browser/curl (test account). expires_in ~12h.
    # Lightstreamer create_session LS_user (= JWT customer_isin claim).
    ls_user: str = "11293241406857"
    token: str = (
        "eyJhbGciOiJSUzI1NiIsImtpZCI6ImI3MmYyMjczZTE4YTQ0YjQ5OTFmMDg3ODIzNzQyYm"
        "I1IiwidHlwIjoiYXQrand0In0.eyJpc3MiOiJodHRwczovL2xvZ2luLmVtb2ZpZC5jb20i"
        "LCJuYmYiOjE3ODg2MDE5MTQsImlhdCI6MTc4ODYwMTkxNCwiZXhwIjoxNzg4NjQ1MTE0LC"
        "JhdWQiOlsiZWFzeTJfYXBpIiwibXRzX2FwaSIsImh0dHBzOi8vbG9naW4uZW1vZmlkLmNv"
        "bS9yZXNvdXJjZXMiXSwic2NvcGUiOlsiZWFzeTJfYXBpIiwibXRzX2FwaSIsIm9wZW5pZC"
        "IsInByb2ZpbGUiLCJsb2dpbl9kZWxlZ2F0aW9uLWFwaSJdLCJhbXIiOlsicHdkIl0sImNs"
        "aWVudF9pZCI6ImVhc3lfcGtjZSIsInN1YiI6IjYwMTU2ZjQzLTg3ZTktNDAwYS04ZTJjLT"
        "YxZmFhMDRmNmJkNSIsImF1dGhfdGltZSI6MTc4ODYwMTkxMiwiaWRwIjoibG9jYWwiLCJw"
        "ayI6IjYwMTU2ZjQzLTg3ZTktNDAwYS04ZTJjLTYxZmFhMDRmNmJkNSIsInR3b19mYWN0b3"
        "JfZW5hYmxlZCI6ImZhbHNlIiwidXNlclR5cGUiOiJIYWdoaWdoaSIsImRpc3BsYXlfbmFt"
        "ZSI6Ilx1MDY0NVx1MDYyRFx1MDY0NVx1MDYyRiBcdTA2NDVcdTA2MzFcdTA2MjdcdTA2Mk"
        "ZcdTA2Q0MiLCJmaXJzdG5hbWUiOiJcdTA2NDVcdTA2MkRcdTA2NDVcdTA2MkYiLCJsYXN0"
        "bmFtZSI6Ilx1MDY0NVx1MDYzMVx1MDYyN1x1MDYyRlx1MDZDQyIsIm5hdGlvbmFsX2lkIj"
        "oiMzI0MTQwNjg1NyIsIm5hdGlvbmFsX2lkX3ZlcmlmaWVkIjoidHJ1ZSIsImVtYWlsIjoi"
        "bW9oYW1tYWRtb3JhZGkuZWVAZ21haWwuY29tIiwicGhvbmVfbnVtYmVyIjoiMDkzODgyNT"
        "ExMjYiLCJwaG9uZV9udW1iZXJfdmVyaWZpZWQiOiJ0cnVlIiwiY3VzdG9tZXJfaXNpbiI6"
        "IjExMjkzMjQxNDA2ODU3IiwiY3VzdG9tZXJfc2VnbWVudF9pZCI6IjA0MzU1NDQ0NTMxMT"
        "UyNTE0MTU1MTUxNDU1NTUxMTQ1NDU1NTIyMzM1NDQ0NDU1NTU0IiwiY29udHJhY3QiOlsi"
        "VGVzdENvbnRyYWN0XzEuMCIsIkVjb250cmFjdF8yLjAiLCJDb2RhbENhdXRpb25UcmFuc2"
        "FjdGlvbl8xLjAiLCJCYXNpY01hcmtldFJpc2tfMS4wIiwiV2FsbGV0Q29udHJhY3RfMS4x"
        "IiwiT21zTW9maWRfMS4wIiwiVHJhZGVPcHRpb25Db250cmFjdEVhc3lUcmFkZXJfMS4wIi"
        "wiQmFzaWNPcHRpb25SaXNrLUVhc3lUcmFkZXJfMS4wIiwiQ292ZXJlZFdhcnJhbnRzXzEu"
        "MCIsIkhhbWlDb250cmFjdF8xLjAiLCJQYXBlckVDb250cmFjdF8xLjAiLCJQYXBlclRlc3"
        "RDb250cmFjdF8xLjAiXSwic2lkIjoiMTZBNDY0N0ZDRDRDODFFMEVDRkZCMjY5NjZBNDZB"
        "NzcifQ.oZysPqXW-lO4febIau4eaL6dINWva-6YpQBPPJTZixl6ju-AOMVtnXEcqHXHYMD"
        "V4M3uT0x0ZoRWRrq49yDTXNEQuYfJ8ppvOoN8DtPFYJA4ZnDb_4JwN2GB1Ld-N9z9SAt6w"
        "NwRvEv6SY0eGmvDNY81wRO-haUlsyU-6ur3JU5UVIrMAmrQULl-32__hZnRY9Ovo4GpB9s"
        "sEqmI7AUivLJawrGyWNY9dpJu8UWkveasxlQwmeOxCeb9kKfbbpsr4WNVPdYwzji-LzytX"
        "8hrpazaLruZ8vyF5CNK8yj_AGMJy-PHcSarX1_IG2jYKQJIk5Pr5-HNWEmpVH1tzsaqyQ"
    )

    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
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
            "Authorization": f"Bearer {MofidAuth.token}",
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
