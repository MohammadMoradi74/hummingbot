import asyncio
import base64
import gzip
import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import msgpack

from hummingbot.connector.exchange.mobin import mobin_constants as CONSTANTS, mobin_utils, mobin_web_utils as web_utils
from hummingbot.connector.exchange.mobin.mobin_order_book import MobinOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.mobin.mobin_exchange import MobinExchange


class MobinAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2
    ONE_HOUR = 60 * 60

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'MobinExchange',
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._domain = domain
        self._api_factory = api_factory

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """
        Retrieves a copy of the full order book from the exchange, for a particular trading pair.

        :param trading_pair: the trading pair for which the order book will be retrieved

        :return: the response from the exchange (JSON dictionary)
        """
        params = {
            "id": await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair),
        }

        rest_assistant = await self._api_factory.get_rest_assistant()
        data = await rest_assistant.execute_request(
            url=web_utils.private_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self._domain),
            params=params,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
            is_auth_required=True,  # <-- ensure auth is applied
        )

        return data

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                # SignalR Subscription for Trades
                subscribe_trade = {
                    "arguments": [{"subscribed": [symbol], "unsubscribed": []}],
                    "target": "SubscribeTrade",
                    "type": 1
                }
                await ws._connection._connection.send_str(json.dumps(subscribe_trade) + "\x1e")

                # SignalR Subscription for Order Book (Information/State)
                subscribe_info = {
                    "arguments": [{"subscribed": [symbol], "unsubscribed": []}],
                    "target": "SubscribeInformation",
                    "type": 1
                }
                await ws._connection._connection.send_str(json.dumps(subscribe_info) + "\x1e")

            self.logger().info("Subscribed to Mobin SignalR channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to order book trading and delta streams...",
                exc_info=True
            )
            raise

    async def _connected_websocket_assistant(self) -> WSAssistant:
        # 1. Negotiate to get connectionToken
        negotiate_url = f"https://pusher11.mobinsb.{self._domain}/mmtp/negotiate?negotiateVersion=1"
        rest_assistant = await self._api_factory.get_rest_assistant()

        # We must provide throttler_limit_id. You can use a generic name or path.
        negotiate_resp = await rest_assistant.execute_request(
            url=negotiate_url,
            method=RESTMethod.POST,
            is_auth_required=True,
            throttler_limit_id="negotiate"  # Added this required argument
        )
        connection_token = negotiate_resp["connectionToken"]

        # 2. Connect to WebSocket with the token
        ws: WSAssistant = await self._api_factory.get_ws_assistant()

        ws_url = f"wss://pusher11.mobinsb.{self._domain}/mmtp?id={connection_token}"
        ws_headers = ws._auth.header_for_authentication()
        await ws.connect(ws_url=ws_url, ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL, ws_headers=ws_headers)

        # 3. SignalR Handshake
        # Handshake message must end with \x1e
        handshake_payload = {"protocol": "json", "version": 1}
        await ws._connection._connection.send_str(json.dumps(handshake_payload) + "\x1e")

        # Wait for handshake response (usually "{}\x1e")
        await ws._connection._connection.receive_str()

        return ws

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = MobinOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        return snapshot_msg

    @staticmethod
    def _decode_signalr_message(b64_payload: str):
        """Decode a gzip+base64 encoded MessagePack payload from SignalR"""
        # Step 1: Decode base64 and decompress gzip
        decoded_bytes = gzip.decompress(base64.b64decode(b64_payload))

        # Step 2: Decode MessagePack
        # Use raw=False to get strings instead of bytes
        data = msgpack.unpackb(decoded_bytes, raw=False, strict_map_key=False)

        return data

    @staticmethod
    def _coerce_signalr_message(event_message: Any) -> Optional[Dict[str, Any]]:
        if isinstance(event_message, dict):
            return event_message
        for message in mobin_utils.iter_signalr_frames(event_message):
            return message
        return None

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        async for ws_response in websocket_assistant.iter_messages():
            data = ws_response.data
            if data is None:
                continue
            for message in mobin_utils.iter_signalr_frames(data):
                channel: str = self._channel_originating_message(event_message=message)
                valid_channels = self._get_messages_queue_keys()
                if channel in valid_channels:
                    self._message_queue[channel].put_nowait(message)
                else:
                    await self._process_message_for_unknown_channel(
                        event_message=message,
                        websocket_assistant=websocket_assistant,
                    )

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        message = self._coerce_signalr_message(raw_message)
        if message is None:
            return
        if "arguments" not in message:
            return
        decoded_message = self._decode_signalr_message(message["arguments"][1])
        instrument_id = decoded_message.get("InstrumentId")
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(instrument_id)
        trade_message = MobinOrderBook.trade_message_from_exchange(
            decoded_message, {"trading_pair": trading_pair})
        message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Any, message_queue: asyncio.Queue):
        message = self._coerce_signalr_message(raw_message)
        if message is None:
            return
        if "arguments" not in message:
            return
        decoded_message = self._decode_signalr_message(message["arguments"][1])
        instrument_id = decoded_message.get("InstrumentId")
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(instrument_id)
        order_book_message: OrderBookMessage = MobinOrderBook.diff_message_from_exchange(
            decoded_message, time.time(), {"trading_pair": trading_pair})
        message_queue.put_nowait(order_book_message)

    def _channel_originating_message(self, event_message: Any) -> str:
        message = self._coerce_signalr_message(event_message)
        if message is None:
            return ""
        if message.get("type") == 6:
            return ""
        if message.get("target") == "time" or "time" in message:
            return ""
        channel = ""
        if "arguments" in message:
            event_type = message["arguments"][0]
            if event_type == CONSTANTS.DIFF_EVENT_TYPE:
                channel = self._diff_messages_queue_key
            elif event_type == CONSTANTS.TRADE_EVENT_TYPE:
                channel = self._trade_messages_queue_key
        return channel
