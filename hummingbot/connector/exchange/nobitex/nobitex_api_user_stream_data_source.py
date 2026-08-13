import asyncio
import time
from typing import TYPE_CHECKING, List, Optional

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS
from hummingbot.connector.exchange.nobitex.nobitex_auth import NobitexAuth
from hummingbot.connector.exchange.nobitex.nobitex_ws_utils import NobitexWSHelper
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.nobitex.nobitex_exchange import NobitexExchange


class NobitexAPIUserStreamDataSource(UserStreamTrackerDataSource):
    """
    Private Centrifugo user stream.
    Docs: https://apidocs.nobitex.ir/websocket/get-websocket-token
    Channels: private:orders#{websocketAuthParam}, private:trades#{websocketAuthParam}
    """

    WS_CREDENTIALS_REFRESH_INTERVAL = CONSTANTS.WS_TOKEN_REFRESH_INTERVAL

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 auth: NobitexAuth,
                 trading_pairs: List[str],
                 connector: "NobitexExchange",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__()
        self._auth: NobitexAuth = auth
        self._trading_pairs = trading_pairs
        self._connector = connector
        self._domain = domain
        self._api_factory = api_factory

        self._ws_credentials_initialized_event = asyncio.Event()
        self._last_ws_credentials_refresh_ts = 0
        self._manage_ws_credentials_task = None

        self._ws_token: Optional[str] = None
        self._websocket_auth_param: Optional[str] = None
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
        await self._ensure_ws_credentials_task_running()
        await self._ws_credentials_initialized_event.wait()
        if self._ws_token is None:
            async with self._ws_credentials_refresh_lock:
                if self._ws_token is None:
                    self._ws_token, self._websocket_auth_param = await self._fetch_ws_credentials()
                    self._last_ws_credentials_refresh_ts = int(time.time())
                    self._ws_credentials_initialized_event.set()
        ws = await self._get_ws_assistant()
        await ws.connect(
            ws_url=CONSTANTS.WSS_URL,
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
        )
        return ws

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        try:
            await NobitexWSHelper.send_connect(websocket_assistant, token=self._ws_token)
            await NobitexWSHelper.wait_for_connect_reply(websocket_assistant)

            orders_channel = NobitexWSHelper.private_orders_channel(self._websocket_auth_param)
            trades_channel = NobitexWSHelper.private_trades_channel(self._websocket_auth_param)
            await NobitexWSHelper.subscribe(websocket_assistant, orders_channel)
            await NobitexWSHelper.subscribe(websocket_assistant, trades_channel)
            self.logger().info(
                f"Subscribed to private user stream channels: {orders_channel}, {trades_channel}"
            )
        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            if "token" in str(e).lower() or "expired" in str(e).lower() or "109" in str(e):
                self._ws_token = None
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
            return await self._auth.get_ws_credentials(rest_assistant, domain=self._domain)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            raise IOError(f"Error fetching WS credentials. Error: {exception}") from exception

    async def _refresh_ws_credentials(self) -> bool:
        try:
            ws_token, websocket_auth_param = await self._fetch_ws_credentials()
            self._ws_token = ws_token
            self._websocket_auth_param = websocket_auth_param
            return True
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
                        self._ws_token, self._websocket_auth_param = await self._fetch_ws_credentials()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exception:
                        self.logger().warning(f"Failed to fetch WS credentials: {exception}")
                        await self._sleep(5)
                        continue
                    self.logger().info(
                        f"Successfully obtained WS credentials "
                        f"(websocketAuthParam={self._websocket_auth_param})"
                    )
                    self._ws_credentials_initialized_event.set()
                    self._last_ws_credentials_refresh_ts = now

                elif now - self._last_ws_credentials_refresh_ts >= self.WS_CREDENTIALS_REFRESH_INTERVAL:
                    async with self._ws_credentials_refresh_lock:
                        now = int(time.time())
                        if now - self._last_ws_credentials_refresh_ts < self.WS_CREDENTIALS_REFRESH_INTERVAL:
                            continue
                        ok = await self._refresh_ws_credentials()
                        if not ok:
                            await self._sleep(5)
                            continue
                        self.logger().info("Refreshing Nobitex WS session before token TTL expiry...")
                        self._planned_ws_reconnect = True
                        if self._ws_assistant is not None:
                            await self._ws_assistant.disconnect()
                        self._last_ws_credentials_refresh_ts = now
                else:
                    remaining = self.WS_CREDENTIALS_REFRESH_INTERVAL - (
                        now - self._last_ws_credentials_refresh_ts
                    )
                    await self._sleep(max(1, remaining))
        finally:
            self._ws_credentials_initialized_event.clear()

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        try:
            async for ws_response in websocket_assistant.iter_messages():
                data = NobitexWSHelper.normalize_message(ws_response.data)
                if data is None:
                    continue

                if NobitexWSHelper.is_ping(data):
                    await websocket_assistant.send(WSJSONRequest(payload=NobitexWSHelper.pong_payload(data)))
                    continue

                event_data = NobitexWSHelper.extract_event_data(data)
                if event_data:
                    await self._process_event_message(event_message=event_data, queue=queue)
        except (ConnectionError, TypeError):
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
            return

        websocket_assistant and await websocket_assistant.disconnect()
        await self._sleep(5)
