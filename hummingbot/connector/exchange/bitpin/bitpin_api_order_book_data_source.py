import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.bitpin import bitpin_constants as CONSTANTS, bitpin_web_utils as web_utils
from hummingbot.connector.exchange.bitpin.bitpin_order_book import BitpinOrderBook
from hummingbot.connector.exchange.bitpin.bitpin_ws_utils import BitpinWSHelper
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.bitpin.bitpin_exchange import BitpinExchange


class BitpinAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2
    ONE_HOUR = 60 * 60

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'BitpinExchange',
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
        rest_assistant = await self._api_factory.get_rest_assistant()
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        data = await rest_assistant.execute_request(
            url=web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL + symbol + '/',
                                          domain=self._domain),
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
        )

        return data

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            # 1) Centrifugo connect (no token for public channels)
            await BitpinWSHelper.send_connect(ws)
            await BitpinWSHelper.wait_for_connect_reply(ws)
            # 2) One subscribe per channel per trading pair
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(
                    trading_pair=trading_pair
                )
                await BitpinWSHelper.subscribe(ws, BitpinWSHelper.orderbook_channel(symbol))
                await BitpinWSHelper.subscribe(ws, BitpinWSHelper.matches_channel(symbol))
            self.logger().info("Subscribed to public order book and trade channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to order book trading and delta streams...",
                exc_info=True,
            )
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        async for ws_response in websocket_assistant.iter_messages():
            data = BitpinWSHelper.normalize_message(ws_response.data)
            if data is None:
                continue

            if BitpinWSHelper.is_ping(data):
                await websocket_assistant.send(WSJSONRequest(payload=BitpinWSHelper.pong_payload(data)))
                continue

            event_data = BitpinWSHelper.extract_event_data(data)
            if event_data is None:
                continue  # connect/subscribe acks, errors already handled

            channel = self._channel_originating_message(event_message=event_data)
            if channel in self._get_messages_queue_keys():
                self._message_queue[channel].put_nowait(event_data)
            else:
                await self._process_message_for_unknown_channel(
                    event_message=event_data, websocket_assistant=websocket_assistant
                )

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=BitpinWSHelper.ws_url(CONSTANTS.WS_DOMAIN),  # use .ir, not hardcoded org
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
        )
        return ws

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = BitpinOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        return snapshot_msg

    async def _parse_trade_message(self, raw_message, message_queue):
        symbol = raw_message["symbol"]
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        trade_message = BitpinOrderBook.trade_message_from_exchange(
            raw_message, {"trading_pair": trading_pair}
        )
        message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message, message_queue):
        symbol = raw_message["symbol"]
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        order_book_message = BitpinOrderBook.diff_message_from_exchange(
            raw_message, time.time(), {"trading_pair": trading_pair}
        )
        message_queue.put_nowait(order_book_message)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        event_type = event_message.get("event")
        if event_type == CONSTANTS.DIFF_EVENT_TYPE:
            return self._diff_messages_queue_key
        if event_type == CONSTANTS.TRADE_EVENT_TYPE:
            return self._trade_messages_queue_key
        return ""

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            self.logger().warning(f"Cannot subscribe to {trading_pair}: WebSocket not connected")
            return False
        try:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await BitpinWSHelper.subscribe(self._ws_assistant, BitpinWSHelper.orderbook_channel(symbol))
            await BitpinWSHelper.subscribe(self._ws_assistant, BitpinWSHelper.matches_channel(symbol))
            self.add_trading_pair(trading_pair)
            self.logger().info(f"Subscribed to {trading_pair} order book and trade channels")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error subscribing to {trading_pair} channels")
            return False

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            self.logger().warning(f"Cannot unsubscribe from {trading_pair}: WebSocket not connected")
            return False
        try:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await BitpinWSHelper.unsubscribe(self._ws_assistant, BitpinWSHelper.orderbook_channel(symbol))
            await BitpinWSHelper.unsubscribe(self._ws_assistant, BitpinWSHelper.matches_channel(symbol))
            self.remove_trading_pair(trading_pair)
            self.logger().info(f"Unsubscribed from {trading_pair} order book and trade channels")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error unsubscribing from {trading_pair} channels")
            return False
