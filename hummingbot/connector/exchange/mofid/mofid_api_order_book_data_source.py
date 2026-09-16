import asyncio
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.exchange.mofid.mofid_lightstreamer import (
    BESTLIMIT_SCHEMA,
    SYMBOL_TRADE_SCHEMA,
    bestlimit_state_to_bids_asks,
    merge_bestlimit_fields,
    parse_conok_session_id,
    parse_u_update_line,
)
from hummingbot.connector.exchange.mofid.mofid_order_book import MofidOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSPlainTextRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.mofid.mofid_exchange import MofidExchange


class MofidAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: "MofidExchange",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._domain = domain
        self._api_factory = api_factory
        self._ls_session_id: Optional[str] = None
        self._next_sub_id = 1
        self._next_req_id = 1
        self._sub_id_to_isin: Dict[str, str] = {}
        self._sub_id_to_channel: Dict[str, str] = {}
        self._isin_to_bestlimit_sub_id: Dict[str, str] = {}
        self._isin_to_symbol_sub_id: Dict[str, str] = {}
        self._bestlimit_state: Dict[str, Dict[str, Optional[str]]] = {}
        self._symbol_state: Dict[str, Dict[str, Optional[str]]] = {}
        self._last_traded_prices: Dict[str, float] = {}

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return {tp: self._last_traded_prices[tp] for tp in trading_pairs if tp in self._last_traded_prices}

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        isin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{isin}"
        rest_assistant = await self._api_factory.get_rest_assistant()
        return await rest_assistant.execute_request(
            url=web_utils.private_rest_url(path_url=path_url, domain=self._domain),
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
            is_auth_required=True,
        )

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        return MofidOrderBook.snapshot_message_from_exchange(
            snapshot,
            time.time(),
            metadata={"trading_pair": trading_pair},
        )

    async def _create_lightstreamer_session(self) -> str:
        auth = self._api_factory._auth
        ls_user = auth.ls_user if isinstance(auth, MofidAuth) else ""
        body = urllib.parse.urlencode({
            "LS_phase": "1",
            "LS_cause": "new.api",
            "LS_polling": "true",
            "LS_polling_millis": "0",
            "LS_idle_millis": "0",
            "LS_cid": CONSTANTS.LS_CID,
            "LS_adapter_set": CONSTANTS.LS_ADAPTER_SET,
            "LS_user": ls_user,
        })
        rest_assistant = await self._api_factory.get_rest_assistant()
        request = RESTRequest(
            method=RESTMethod.POST,
            url=CONSTANTS.LS_CREATE_SESSION_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            throttler_limit_id=CONSTANTS.LS_CREATE_SESSION_LIMIT_ID,
        )
        async with self._api_factory._throttler.execute_task(limit_id=CONSTANTS.LS_CREATE_SESSION_LIMIT_ID):
            response = await rest_assistant.call(request=request)
            if response.status >= 400:
                text = await response.text()
                raise IOError(
                    f"Error creating Lightstreamer session. HTTP {response.status}. Error: {text}"
                )
            text = await response.text()
        return parse_conok_session_id(text)

    async def _connected_websocket_assistant(self) -> WSAssistant:
        self._ls_session_id = await self._create_lightstreamer_session()
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        auth = self._api_factory._auth
        ws_headers = auth.ws_connect_headers() if isinstance(auth, MofidAuth) else {}
        await ws.connect(
            ws_url=CONSTANTS.WSS_URL,
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
            ws_headers=ws_headers,
        )
        bind = (
            f"bind_session\r\n"
            f"LS_session={self._ls_session_id}&LS_phase=2&LS_cause=loop1&"
        )
        await ws.send(WSPlainTextRequest(payload=bind))
        while True:
            response = await ws.receive()
            if response is None:
                raise ConnectionError("Lightstreamer websocket closed during bind_session")
            data = response.data
            if isinstance(data, str) and "CONOK," in data:
                break
        return ws

    def _ls_control(
            self,
            group: str,
            schema: List[str],
            adapter: str,
            sub_id: int,
            req_id: int,
            op: str = "add",
    ) -> str:
        params = {
            "LS_mode": "MERGE",
            "LS_group": group,
            "LS_schema": " ".join(schema),
            "LS_data_adapter": adapter,
            "LS_snapshot": "true",
            "LS_subId": str(sub_id),
            "LS_op": op,
            "LS_reqId": str(req_id),
        }
        if self._ls_session_id:
            params["LS_session"] = self._ls_session_id
        return "control\r\n" + urllib.parse.urlencode(params) + "&"

    def _alloc_sub(self, isin: str, channel: str) -> tuple:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        req_id = self._next_req_id
        self._next_req_id += 1
        self._sub_id_to_isin[str(sub_id)] = isin
        self._sub_id_to_channel[str(sub_id)] = channel
        return sub_id, req_id

    async def _send_bestlimit_subscription(self, ws: WSAssistant, isin: str, subscribe: bool) -> None:
        if subscribe:
            sub_id, req_id = self._alloc_sub(isin, CONSTANTS.LS_CHANNEL_BESTLIMIT)
            self._isin_to_bestlimit_sub_id[isin] = str(sub_id)
            self._bestlimit_state[isin] = {}
            payload = self._ls_control(
                group=f"bestlimit:{isin}",
                schema=BESTLIMIT_SCHEMA,
                adapter=CONSTANTS.LS_BESTLIMIT_ADAPTER,
                sub_id=sub_id,
                req_id=req_id,
                op="add",
            )
        else:
            sub_id_str = self._isin_to_bestlimit_sub_id.get(isin)
            if sub_id_str is None:
                return
            req_id = self._next_req_id
            self._next_req_id += 1
            payload = self._ls_control(
                group=f"bestlimit:{isin}",
                schema=BESTLIMIT_SCHEMA,
                adapter=CONSTANTS.LS_BESTLIMIT_ADAPTER,
                sub_id=int(sub_id_str),
                req_id=req_id,
                op="delete",
            )
            self._sub_id_to_isin.pop(sub_id_str, None)
            self._sub_id_to_channel.pop(sub_id_str, None)
            self._isin_to_bestlimit_sub_id.pop(isin, None)
            self._bestlimit_state.pop(isin, None)
        await ws.send(WSPlainTextRequest(payload=payload))

    async def _send_symbol_subscription(self, ws: WSAssistant, isin: str, subscribe: bool) -> None:
        if subscribe:
            sub_id, req_id = self._alloc_sub(isin, CONSTANTS.LS_CHANNEL_SYMBOL)
            self._isin_to_symbol_sub_id[isin] = str(sub_id)
            self._symbol_state[isin] = {}
            payload = self._ls_control(
                group=f"symbol:{isin}",
                schema=SYMBOL_TRADE_SCHEMA,
                adapter=CONSTANTS.LS_SYMBOL_ADAPTER,
                sub_id=sub_id,
                req_id=req_id,
                op="add",
            )
        else:
            sub_id_str = self._isin_to_symbol_sub_id.get(isin)
            if sub_id_str is None:
                return
            req_id = self._next_req_id
            self._next_req_id += 1
            payload = self._ls_control(
                group=f"symbol:{isin}",
                schema=SYMBOL_TRADE_SCHEMA,
                adapter=CONSTANTS.LS_SYMBOL_ADAPTER,
                sub_id=int(sub_id_str),
                req_id=req_id,
                op="delete",
            )
            self._sub_id_to_isin.pop(sub_id_str, None)
            self._sub_id_to_channel.pop(sub_id_str, None)
            self._isin_to_symbol_sub_id.pop(isin, None)
            self._symbol_state.pop(isin, None)
        await ws.send(WSPlainTextRequest(payload=payload))

    async def _subscribe_channels(self, ws: WSAssistant):
        try:
            for trading_pair in self._trading_pairs:
                isin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                await self._send_bestlimit_subscription(ws, isin, subscribe=True)
                await self._send_symbol_subscription(ws, isin, subscribe=True)
            self.logger().info("Subscribed to Mofid Lightstreamer bestlimit and symbol channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to order book trading and delta streams...",
                exc_info=True,
            )
            raise

    async def _handle_bestlimit_update(self, isin: str, raw_fields: List[str]) -> None:
        previous = self._bestlimit_state.get(isin, {})
        merged = merge_bestlimit_fields(previous, raw_fields)
        self._bestlimit_state[isin] = merged
        bids, asks = bestlimit_state_to_bids_asks(merged, depth=CONSTANTS.ORDER_BOOK_DEPTH)
        self._message_queue[self._diff_messages_queue_key].put_nowait({
            "e": CONSTANTS.DIFF_EVENT_TYPE,
            "isin": isin,
            "bids": bids,
            "asks": asks,
        })

    async def _handle_symbol_update(self, isin: str, raw_fields: List[str]) -> None:
        previous = self._symbol_state.get(isin, {})
        merged = merge_bestlimit_fields(previous, raw_fields, schema=SYMBOL_TRADE_SCHEMA)
        self._symbol_state[isin] = merged

        price_s = merged.get("last-trade-price")
        if not price_s:
            return
        price = float(price_s)
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(isin)
        self._last_traded_prices[trading_pair] = price

        # First snapshot only seeds state — avoid emitting day-total as one trade.
        if not previous.get("total-number-of-shares-traded"):
            return

        vol_s = merged.get("total-number-of-shares-traded")
        old_vol = float(previous.get("total-number-of-shares-traded") or 0)
        new_vol = float(vol_s or old_vol)
        delta = new_vol - old_vol
        if delta <= 0:
            return

        now = time.time()
        amount = int(delta) if delta == int(delta) else delta
        self._message_queue[self._trade_messages_queue_key].put_nowait({
            "e": CONSTANTS.TRADE_EVENT_TYPE,
            "isin": isin,
            "price": str(price) if price != int(price) else str(int(price)),
            "amount": str(amount),
            "timestamp": now,
            "trade_id": int(now * 1e3),
        })

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        async for ws_response in websocket_assistant.iter_messages():
            data = ws_response.data
            if data is None:
                continue
            if not isinstance(data, str):
                continue
            for line in data.replace("\r\n", "\n").split("\n"):
                line = line.strip()
                if not line or line.startswith("PROBE") or line.startswith("SYNC"):
                    continue
                if line.startswith("REQOK") or line.startswith("SUBOK") or line.startswith("CONF"):
                    continue
                parsed = parse_u_update_line(line)
                if parsed is None:
                    continue
                sub_id, raw_fields = parsed
                isin = self._sub_id_to_isin.get(sub_id)
                channel = self._sub_id_to_channel.get(sub_id)
                if isin is None or channel is None:
                    continue
                if channel == CONSTANTS.LS_CHANNEL_BESTLIMIT:
                    await self._handle_bestlimit_update(isin, raw_fields)
                elif channel == CONSTANTS.LS_CHANNEL_SYMBOL:
                    await self._handle_symbol_update(isin, raw_fields)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        isin = raw_message.get("isin")
        if not isin:
            return
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(isin)
        trade_message = MofidOrderBook.trade_message_from_exchange(
            raw_message,
            metadata={"trading_pair": trading_pair},
        )
        message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        isin = raw_message.get("isin")
        if not isin:
            return
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(isin)
        order_book_message = MofidOrderBook.diff_message_from_exchange(
            raw_message,
            time.time(),
            metadata={"trading_pair": trading_pair},
        )
        message_queue.put_nowait(order_book_message)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        event_type = event_message.get("e")
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
            isin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await self._send_bestlimit_subscription(self._ws_assistant, isin, subscribe=True)
            await self._send_symbol_subscription(self._ws_assistant, isin, subscribe=True)
            self.add_trading_pair(trading_pair)
            self.logger().info(f"Subscribed to {trading_pair} bestlimit and symbol channels")
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
            isin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await self._send_bestlimit_subscription(self._ws_assistant, isin, subscribe=False)
            await self._send_symbol_subscription(self._ws_assistant, isin, subscribe=False)
            self.remove_trading_pair(trading_pair)
            self.logger().info(f"Unsubscribed from {trading_pair} bestlimit and symbol channels")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error unsubscribing from {trading_pair} channels")
            return False
