import asyncio
import time
from typing import TYPE_CHECKING, List, Optional

from hummingbot.connector.exchange.bitpin import bitpin_constants as CONSTANTS
from hummingbot.connector.exchange.bitpin.bitpin_auth import BitpinAuth
from hummingbot.connector.exchange.bitpin.bitpin_ws_utils import BitpinWSHelper
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.bitpin.bitpin_exchange import BitpinExchange


class BitpinAPIUserStreamDataSource(UserStreamTrackerDataSource):
    WS_CREDENTIALS_REFRESH_INTERVAL = CONSTANTS.WS_INFO_REFRESH_INTERVAL

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 auth: BitpinAuth,
                 trading_pairs: List[str],
                 connector: 'BitpinExchange',
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__()
        self._auth: BitpinAuth = auth

        self._current_listen_key = None
        self._domain = domain
        self._api_factory = api_factory

        self._listen_key_initialized_event: asyncio.Event = asyncio.Event()

        self._ws_credentials_initialized_event = asyncio.Event()
        self._last_ws_credentials_refresh_ts = 0
        self._manage_ws_credentials_task = None

        self._last_listen_key_ping_ts = 0
        self._ws_token: Optional[str] = None
        self._user_identifier: Optional[str] = None

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Creates an instance of WSAssistant connected to the exchange
        """
        self._manage_ws_credentials_task = safe_ensure_future(self._manage_ws_credentials_task_loop())
        await self._ws_credentials_initialized_event.wait()
        ws = await self._get_ws_assistant()
        await ws.connect(
            ws_url=BitpinWSHelper.ws_url(CONSTANTS.WS_DOMAIN),
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
        )
        return ws

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        try:
            # 1) Connect to Centrifugo WITH ws_token (required for private channel)
            await BitpinWSHelper.send_connect(websocket_assistant, token=self._ws_token)
            await BitpinWSHelper.wait_for_connect_reply(websocket_assistant)
            # 2) Subscribe to private user orders channel
            channel = BitpinWSHelper.user_orders_channel(self._user_identifier)
            await BitpinWSHelper.subscribe(websocket_assistant, channel)
            self.logger().info(f"Subscribed to private user stream channel: {channel}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to user stream...",
                exc_info=True,
            )
            raise

    async def _fetch_ws_credentials(self) -> tuple[str, str]:
        try:
            rest_assistant = await self._api_factory.get_rest_assistant()
            return await self._auth.get_ws_credentials(rest_assistant)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            raise IOError(f"Error fetching WS credentials. Error: {exception}") from exception

    async def _refresh_ws_credentials(self) -> bool:
        try:
            ws_token, user_identifier = await self._fetch_ws_credentials()
            token_changed = ws_token != self._ws_token
            self._ws_token = ws_token
            self._user_identifier = user_identifier
            return not token_changed  # False => force reconnect (token changed/expired)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            self.logger().warning(f"Failed to refresh WS credentials: {exception}")
            return False

    async def _manage_ws_credentials_task_loop(self):
        try:
            while True:
                now = int(time.time())
                if self._ws_token is None:
                    self._ws_token, self._user_identifier = await self._fetch_ws_credentials()
                    self.logger().info(
                        f"Successfully obtained WS credentials for user {self._user_identifier}"
                    )
                    self._ws_credentials_initialized_event.set()
                    self._last_ws_credentials_refresh_ts = now

                elif now - self._last_ws_credentials_refresh_ts >= self.WS_CREDENTIALS_REFRESH_INTERVAL:
                    unchanged = await self._refresh_ws_credentials()
                    if not unchanged:
                        self.logger().info("WS token changed/expired. Forcing reconnect...")
                        if self._ws_assistant is not None:
                            await self._ws_assistant.disconnect()
                        break
                    self.logger().info("Refreshed WS credentials.")
                    self._last_ws_credentials_refresh_ts = now
                else:
                    await self._sleep(self.WS_CREDENTIALS_REFRESH_INTERVAL)
        finally:
            self._ws_credentials_initialized_event.clear()

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        async for ws_response in websocket_assistant.iter_messages():
            data = BitpinWSHelper.normalize_message(ws_response.data)
            if data is None:
                continue

            if BitpinWSHelper.is_ping(data):
                await websocket_assistant.send(WSJSONRequest(payload=BitpinWSHelper.pong_payload(data)))
                continue

            event_data = BitpinWSHelper.extract_event_data(data)
            if event_data:
                await self._process_event_message(event_message=event_data, queue=queue)

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        await super()._on_user_stream_interruption(websocket_assistant=websocket_assistant)
        self._manage_ws_credentials_task and self._manage_ws_credentials_task.cancel()
        self._ws_token = None
        self._user_identifier = None
        self._ws_credentials_initialized_event.clear()
        await self._sleep(5)
