from typing import Dict

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class MofidAuth(AuthBase):
    # Manual paste from browser/curl (test account). expires_in ~12h.
    # Lightstreamer create_session LS_user (= JWT customer_isin claim).
    ls_user: str = "11293241406857"
    token: str = (
        "eyJhbGciOiJSUzI1NiIsImtpZCI6ImI3MmYyMjczZTE4YTQ0YjQ5OTFmMDg3ODIzNzQyYmI1IiwidHlwIjoiYXQrand0In0."
        "eyJpc3MiOiJodHRwczovL2xvZ2luLmVtb2ZpZC5jb20iLCJuYmYiOjE3ODg1ODI2NjEsImlhdCI6MTc4ODU4MjY2MSwiZXhwIjox"
        "Nzg4NjI1ODYxLCJhdWQiOlsiZWFzeTJfYXBpIiwibXRzX2FwaSIsImh0dHBzOi8vbG9naW4uZW1vZmlkLmNvbS9yZXNvdXJjZXMi"
        "XSwic2NvcGUiOlsiZWFzeTJfYXBpIiwibXRzX2FwaSIsIm9wZW5pZCIsInByb2ZpbGUiLCJsb2dpbl9kZWxlZ2F0aW9uLWFwaSJd"
        "LCJhbXIiOlsicHdkIl0sImNsaWVudF9pZCI6ImVhc3lfcGtjZSIsInN1YiI6IjYwMTU2ZjQzLTg3ZTktNDAwYS04ZTJjLTYxZmFh"
        "MDRmNmJkNSIsImF1dGhfdGltZSI6MTc4ODAyNDM2OCwiaWRwIjoibG9jYWwiLCJwayI6IjYwMTU2ZjQzLTg3ZTktNDAwYS04ZTJj"
        "LTYxZmFhMDRmNmJkNSIsInR3b19mYWN0b3JfZW5hYmxlZCI6ImZhbHNlIiwidXNlclR5cGUiOiJIYWdoaWdoaSIsImRpc3BsYXlf"
        "bmFtZSI6Ilx1MDY0NVx1MDYyRFx1MDY0NVx1MDYyRiBcdTA2NDVcdTA2MzFcdTA2MjdcdTA2MkZcdTA2Q0MiLCJmaXJzdG5hbWUi"
        "OiJcdTA2NDVcdTA2MkRcdTA2NDVcdTA2MkYiLCJsYXN0bmFtZSI6Ilx1MDY0NVx1MDYzMVx1MDYyN1x1MDYyRlx1MDZDQyIsIm5h"
        "dGlvbmFsX2lkIjoiMzI0MTQwNjg1NyIsIm5hdGlvbmFsX2lkX3ZlcmlmaWVkIjoidHJ1ZSIsImVtYWlsIjoibW9oYW1tYWRtb3Jh"
        "ZGkuZWVAZ21haWwuY29tIiwicGhvbmVfbnVtYmVyIjoiMDkzODgyNTExMjYiLCJwaG9uZV9udW1iZXJfdmVyaWZpZWQiOiJ0cnVl"
        "IiwiY3VzdG9tZXJfaXNpbiI6IjExMjkzMjQxNDA2ODU3IiwiY3VzdG9tZXJfc2VnbWVudF9pZCI6IjA0MzU1NDQ0NTMxMTUyNTE0"
        "MTU1MTUxNDU1NTUxMTQ1NDU1NTIyMzM1NDQ0NDU1NTU0IiwiY29udHJhY3QiOlsiVGVzdENvbnRyYWN0XzEuMCIsIkVjb250cmFj"
        "dF8yLjAiLCJDb2RhbENhdXRpb25UcmFuc2FjdGlvbl8xLjAiLCJCYXNpY01hcmtldFJpc2tfMS4wIiwiV2FsbGV0Q29udHJhY3Rf"
        "MS4xIiwiT21zTW9maWRfMS4wIiwiVHJhZGVPcHRpb25Db250cmFjdEVhc3lUcmFkZXJfMS4wIiwiQmFzaWNPcHRpb25SaXNrLUVh"
        "c3lUcmFkZXJfMS4wIiwiQ292ZXJlZFdhcnJhbnRzXzEuMCIsIkhhbWlDb250cmFjdF8xLjAiLCJQYXBlckVDb250cmFjdF8xLjAi"
        "LCJQYXBlclRlc3RDb250cmFjdF8xLjAiXSwic2lkIjoiNEEzN0I3M0I2QjI2Q0ZFMjg4OTJERTgxQ0VERkQ2MEQifQ."
        "wYqP7Wld7p2I5gSb5ZMlD7DsAekZcAAQiV5NM_jOSwXSeZOCI7AhkWiuZvElNgF82P2_tbS9hmImP3GmX5u4vsrngU3R1tqRMvWN"
        "GFZoIxRTuyHwisYLN4trnXOA4gC4jgmZC3gCZ9j3chejNeGILTpDjpuiqxjuBaE2nDX0ZrcRKwPnjV50hPGrqgWWoUd2y_qKkYMn"
        "I_qGWMtGK_qGOp7-W2eDMtV5YGc3Fz3d6HNcA2IqQu7Afj3tftbCGHBlfcKYTeRXUSAmObG9ngV4J8kjCd2Fip6CkIx2MEyfqiCm"
        "yr7e89uU28wEODxsKuZ9tL0TcEPlVAkN4Zl8NMvMQA"
    )

    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request

    def header_for_authentication(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {MofidAuth.token}"}
