from typing import Dict

import aiohttp

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class BitpinAuth(AuthBase):
    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider
        self.access_token = None
        self.refresh_token = None

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions. It also adds
        the required parameter in the request header.
        :param request: the request to be configured for authenticated interaction
        """
        if self.access_token is None:
            await self.authenticate()

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        request.headers = headers

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated. Bitpin does not use this
        functionality
        """
        return request  # pass-through

    def header_for_authentication(self) -> Dict[str, str]:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}"}

    async def authenticate(self):
        """
        Sends the authentication request to the Bitpin API to get the access and refresh tokens.
        """
        # TODO: Clean up the mess!
        url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        headers = {
            "Content-Type": "application/json"
        }
        payload = {
            "api_key": self.api_key,
            "secret_key": self.secret_key
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data.get("access")
                    self.refresh_token = data.get("refresh")
                    print("Authentication successful!")
                else:
                    raise Exception(f"Authentication failed with status code {response.status}")
