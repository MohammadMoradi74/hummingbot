import asyncio
from typing import Dict, Optional

import aiohttp

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest

counter_token = 0


class MobinAuth(AuthBase):
    _token_lock: asyncio.Lock = asyncio.Lock()
    token: Optional[str] = None

    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions. It also adds
        the required parameter in the request header.
        :param request: the request to be configured for authenticated interaction
        """
        if MobinAuth.token is None:
            await self.authenticate()

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        request.headers = headers

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated. Mobin does not use this
        functionality
        """
        return request  # pass-through

    def header_for_authentication(self) -> Dict[str, str]:
        return {"Content-Type": "application/json",
                # TODO: This line was removed in the previous commit. Check the conflict!
                "Authorization": f"Bearer {MobinAuth.token}"}

    async def authenticate(self):
        """
        Sends the authentication request to the Mobin API to get tokens.
        """
        # TODO: Clean up the mess!
        url = "https://externalapi.mobinsb.ir/api/V1/Authentication/GenerateToken"
        headers = {
            "Content-Type": "application/json"
        }
        payload = {
            "userName": self.api_key,
            "password": self.secret_key
        }

        async with MobinAuth._token_lock:
            async with aiohttp.ClientSession() as session:
                global counter_token
                print(f'send requests to auth: {counter_token}')
                counter_token += 1
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        MobinAuth.token = data["data"]["token"]
                        print(f"Authentication successful! Token: {MobinAuth.token[0:5]}...")
                    else:
                        raise Exception(f"Authentication failed with status code {response.status}")
