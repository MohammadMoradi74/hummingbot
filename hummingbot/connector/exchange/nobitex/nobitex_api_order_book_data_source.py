import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS, nobitex_web_utils as web_utils
from hummingbot.connector.exchange.nobitex.nobitex_order_book import NobitexOrderBook
from hummingbot.connector.exchange.nobitex.nobitex_ws_utils import NobitexWSHelper
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.nobitex.nobitex_exchange import NobitexExchange


class NobitexAPIOrderBookDataSource(OrderBookTrackerDataSource):
    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'NobitexExchange',
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
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        rest_assistant = await self._api_factory.get_rest_assistant()
        return await rest_assistant.execute_request(
            url=web_utils.public_rest_url(
                path_url=f"{CONSTANTS.SNAPSHOT_PATH_URL}/{symbol}",
                domain=self._domain,
            ),
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
        )

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        return NobitexOrderBook.snapshot_message_from_exchange(
            snapshot,
            time.time(),
            metadata={"trading_pair": trading_pair},
        )

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=CONSTANTS.WSS_URL,
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
        )
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        try:
            await NobitexWSHelper.send_connect(ws)
            await NobitexWSHelper.wait_for_connect_reply(ws)
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                await NobitexWSHelper.subscribe(ws, NobitexWSHelper.orderbook_channel(symbol))
                await NobitexWSHelper.subscribe(ws, NobitexWSHelper.trades_channel(symbol))
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
            data = NobitexWSHelper.normalize_message(ws_response.data)
            if data is None:
                continue
            if NobitexWSHelper.is_ping(data):
                await websocket_assistant.send(WSJSONRequest(payload=NobitexWSHelper.pong_payload(data)))
                continue
            event_data = NobitexWSHelper.extract_event_data(data)
            if event_data is None:
                continue
            channel = self._channel_originating_message(event_message=event_data)
            if channel in self._get_messages_queue_keys():
                self._message_queue[channel].put_nowait(event_data)
            else:
                await self._process_message_for_unknown_channel(
                    event_message=event_data, websocket_assistant=websocket_assistant
                )

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = event_message.get("_channel", "")
        if channel.startswith(CONSTANTS.WS_ORDERBOOK_CHANNEL_PREFIX):
            return self._diff_messages_queue_key
        if channel.startswith(CONSTANTS.WS_TRADES_CHANNEL_PREFIX):
            return self._trade_messages_queue_key
        return ""

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        symbol = NobitexWSHelper.symbol_from_channel(raw_message["_channel"])
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        trade_message = NobitexOrderBook.trade_message_from_exchange(
            raw_message, {"trading_pair": trading_pair})
        message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        symbol = NobitexWSHelper.symbol_from_channel(raw_message["_channel"])
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        order_book_message = NobitexOrderBook.diff_message_from_exchange(
            raw_message, time.time(), {"trading_pair": trading_pair})
        message_queue.put_nowait(order_book_message)

    async def _parse_order_book_snapshot_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        trading_pair = raw_message.get("trading_pair")
        if trading_pair is None:
            symbol = NobitexWSHelper.symbol_from_channel(raw_message["_channel"])
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        snapshot_msg = NobitexOrderBook.snapshot_message_from_exchange(
            raw_message, time.time(), {"trading_pair": trading_pair})
        message_queue.put_nowait(snapshot_msg)

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            self.logger().warning(f"Cannot subscribe to {trading_pair}: WebSocket not connected")
            return False
        try:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await NobitexWSHelper.subscribe(self._ws_assistant, NobitexWSHelper.orderbook_channel(symbol))
            await NobitexWSHelper.subscribe(self._ws_assistant, NobitexWSHelper.trades_channel(symbol))
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
            await NobitexWSHelper.unsubscribe(self._ws_assistant, NobitexWSHelper.orderbook_channel(symbol))
            await NobitexWSHelper.unsubscribe(self._ws_assistant, NobitexWSHelper.trades_channel(symbol))
            self.remove_trading_pair(trading_pair)
            self.logger().info(f"Unsubscribed from {trading_pair} order book and trade channels")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error unsubscribing from {trading_pair} channels")
            return False
