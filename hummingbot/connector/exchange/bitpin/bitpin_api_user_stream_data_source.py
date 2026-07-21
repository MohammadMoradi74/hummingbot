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
        self._planned_ws_reconnect: bool = False
        self._ws_credentials_refresh_lock = asyncio.Lock()

    async def _ensure_ws_credentials_task_running(self):
        if self._manage_ws_credentials_task is not None and not self._manage_ws_credentials_task.done():
            return
        if self._manage_ws_credentials_task is not None:
            self._manage_ws_credentials_task.cancel()
            try:
                await self._manage_ws_credentials_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._manage_ws_credentials_task = safe_ensure_future(self._manage_ws_credentials_task_loop())

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Creates an instance of WSAssistant connected to the exchange
        """
        await self._ensure_ws_credentials_task_running()
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
                    try:
                        self._ws_token, self._user_identifier = await self._fetch_ws_credentials()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exception:
                        self.logger().warning(f"Failed to fetch WS credentials: {exception}")
                        await self._sleep(5)
                        continue
                    self.logger().info(
                        f"Successfully obtained WS credentials for user {self._user_identifier}"
                    )
                    self._ws_credentials_initialized_event.set()
                    self._last_ws_credentials_refresh_ts = now

                elif now - self._last_ws_credentials_refresh_ts >= self.WS_CREDENTIALS_REFRESH_INTERVAL:
                    async with self._ws_credentials_refresh_lock:
                        now = int(time.time())
                        if now - self._last_ws_credentials_refresh_ts < self.WS_CREDENTIALS_REFRESH_INTERVAL:
                            continue
                        # Refresh token, then always reconnect. Centrifugo connection TTL
                        # (~15.5m → close 3005) is not extended by REST ws-info alone.
                        await self._refresh_ws_credentials()
                        self.logger().info("Refreshing Bitpin WS session before Centrifugo TTL expiry...")
                        self._planned_ws_reconnect = True  # intentional; not an error
                        if self._ws_assistant is not None:
                            await self._ws_assistant.disconnect()
                        self._last_ws_credentials_refresh_ts = now
                else:
                    remaining = self.WS_CREDENTIALS_REFRESH_INTERVAL - (now - self._last_ws_credentials_refresh_ts)
                    await self._sleep(max(1, remaining))
        finally:
            self._ws_credentials_initialized_event.clear()

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        try:
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
        except (ConnectionError, TypeError):
            # Proactive TTL refresh disconnects mid-read; aiohttp can yield msg.data=None → TypeError
            if self._planned_ws_reconnect:
                return
            raise

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        if self._planned_ws_reconnect:
            self._planned_ws_reconnect = False
            return  # ws-info + credential task already fresh; listen loop will reconnect

        websocket_assistant and await websocket_assistant.disconnect()
        await self._sleep(5)
        # Keep _ws_token + credential task alive — reconnect reuses cached ws-info
