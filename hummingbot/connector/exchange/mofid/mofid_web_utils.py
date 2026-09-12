import time
from typing import Callable, Optional

import hummingbot.connector.exchange.mofid.mofid_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.connector.utils import TimeSynchronizerRESTPreProcessor
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided public REST endpoint
    :param path_url: a public REST endpoint (must start with /)
    :param domain: unused for Mofid (host is fixed); kept for template compatibility
    :return: the full URL to the endpoint
    """
    return CONSTANTS.REST_URL + CONSTANTS.PUBLIC_API_VERSION + path_url


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for provided private REST endpoint
    :param path_url: a private REST endpoint (must start with /)
    :param domain: unused for Mofid (host is fixed); kept for template compatibility
    :return: the full URL to the endpoint
    """
    return CONSTANTS.REST_URL + CONSTANTS.PRIVATE_API_VERSION + path_url


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        time_synchronizer: Optional[TimeSynchronizer] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        time_provider: Optional[Callable] = None,
        auth: Optional[AuthBase] = None, ) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    time_synchronizer = time_synchronizer or TimeSynchronizer()
    # Live: /easy/api/account/server-time/{ms} requires Bearer (401 without auth).
    time_provider = time_provider or (lambda: get_current_server_time(
        throttler=throttler,
        domain=domain,
        auth=auth,
    ))
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[
            TimeSynchronizerRESTPreProcessor(synchronizer=time_synchronizer, time_provider=time_provider),
        ])
    return api_factory


def build_api_factory_without_time_synchronizer_pre_processor(
        throttler: AsyncThrottler,
        auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    api_factory = WebAssistantsFactory(throttler=throttler, auth=auth)
    return api_factory


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


def server_time_path(client_ms: Optional[int] = None) -> str:
    """GET path: /easy/api/account/server-time/{clientUnixMs}."""
    client_ms = int(client_ms if client_ms is not None else time.time() * 1e3)
    return f"{CONSTANTS.SERVER_TIME_PATH_URL}/{client_ms}"


def parse_server_timestamp_ms(response: dict) -> float:
    """
    Live body: {"diff": <int>, "serverTimestamp": <epoch_ms>}.
    TimeSynchronizer expects milliseconds since epoch.
    """
    if "serverTimestamp" not in response:
        raise KeyError(f"Mofid server-time missing serverTimestamp: {response!r}")
    return float(response["serverTimestamp"])


async def get_current_server_time(
        throttler: Optional[AsyncThrottler] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        auth: Optional[AuthBase] = None,
) -> float:
    throttler = throttler or create_throttler()
    api_factory = build_api_factory_without_time_synchronizer_pre_processor(
        throttler=throttler,
        auth=auth,
    )
    rest_assistant = await api_factory.get_rest_assistant()
    path_url = server_time_path()
    response = await rest_assistant.execute_request(
        url=private_rest_url(path_url=path_url, domain=domain),
        method=RESTMethod.GET,
        throttler_limit_id=CONSTANTS.SERVER_TIME_PATH_URL,
        is_auth_required=True,
    )
    return parse_server_timestamp_ms(response)
