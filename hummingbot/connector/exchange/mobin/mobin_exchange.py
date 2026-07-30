import asyncio
import base64
import gzip
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import msgpack
from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.mobin import mobin_constants as CONSTANTS, mobin_utils, mobin_web_utils as web_utils
from hummingbot.connector.exchange.mobin.mobin_api_order_book_data_source import MobinAPIOrderBookDataSource
from hummingbot.connector.exchange.mobin.mobin_api_user_stream_data_source import MobinAPIUserStreamDataSource
from hummingbot.connector.exchange.mobin.mobin_auth import MobinAuth
from hummingbot.connector.exchange.mobin.mobin_utils import iter_signalr_frames
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class MobinExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(self,
                 mobin_api_key: str,
                 mobin_api_secret: str,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 client_config_map: Optional["ClientConfigAdapter"] = None,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100"),
                 ):
        self.api_key = mobin_api_key
        self.secret_key = mobin_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._last_trades_poll_mobin_timestamp = 1.0
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        # Mobin specific params
        self._mobin_numeric_order_ids: Dict[str, str] = {}  # uniqueKey -> Today id
        self._mobin_unique_key_by_request_id: Dict[str, str] = {}  # RequestId -> uniqueKey
        self._today_orders_cache: List[Dict[str, Any]] = []
        self._today_orders_cache_ts: float = 0.0
        self._balance_refresh_task: Optional[asyncio.Task] = None
        self._balance_refresh_debounce_s = 0.5

    @staticmethod
    def to_hb_order_type(mobin_type: str) -> OrderType:
        return OrderType[mobin_type]

    @property
    def authenticator(self):
        return MobinAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer)

    @property
    def name(self) -> str:
        if self._domain == "ir":
            return "mobin"
        else:
            return f"mobin_{self._domain}"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self._domain

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.PING_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return False

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT]

    async def get_all_pairs_prices(self) -> List[Dict[str, str]]:
        pairs_prices = await self._api_get(path_url=CONSTANTS.TICKER_BOOK_PATH_URL)
        return pairs_prices

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        # Mobin doesn't use timestamp-based auth, so no time sync errors
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "Order not found in Today" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        msg = str(cancelation_exception)
        # Do NOT match 1600 / OriginalOrderIsNotInBook — that is "not in book", often filled
        return ("Mobin order not found for cancel" in msg
                or "Order not found in Today" in msg)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return MobinAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return MobinAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = order_type is OrderType.LIMIT_MAKER
        return DeductedFromReturnsTradeFee(percent=self.estimate_fee_pct(is_maker))

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:
        amount_str = f"{int(amount)}"
        type_str = 1  # only supports limit order
        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        price_str = f"{int(price)}"
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        api_params = {"instrumentId": symbol,
                      "quantity": amount_str,
                      "price": price_str,
                      "accountType": 1,  # Broker: 1, Bank: 2, TraderCredit: 3
                      "orderType": type_str,  # None: 0, LimitOrder: 1, MarketOnOpeningOrder: 2, MarketOrder: 3, ...
                      "validityType": 1,  # Day: 1, GoodTillDate: 2, GoodTillCancelled: 3, FillAndKill: 4, ...
                      "pending": 0,
                      "orderSide": side_str,  # Buy: 1, Sell: 2
                      "requestType": 1,  # Creation: 1, Modification: 2, 	Cancellation: 3
                      "lockedPrice": 0,
                      "usePledge": "false"}

        order_result = await self._api_post(
            path_url=CONSTANTS.ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True,
        )

        if not order_result.get("success") or order_result.get("requestErrorCode", 0) != 0:
            raise IOError(f"SaveRequest rejected: {order_result}")

        unique_key = order_result["uniqueKey"]
        transact_time = self.current_timestamp

        # Resolve numeric id early (short retry — request is async)
        for _ in range(2):
            numeric_id = await self._resolve_numeric_order_id(unique_key)
            if numeric_id is not None:
                break
            await self._sleep(0.3)
        return unique_key, transact_time

    async def _get_today_orders(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        now = self.current_timestamp
        if not force_refresh and now - self._today_orders_cache_ts < 10.0:
            return self._today_orders_cache
        rows = await self._api_get(
            path_url=CONSTANTS.MY_ORDERS_PATH_URL,
            params={"displayFailedRequest": "False"},
            is_auth_required=True,
            limit_id=CONSTANTS.MY_ORDERS_PATH_URL,
        )
        self._today_orders_cache = rows
        self._today_orders_cache_ts = now
        return rows

    def _find_today_order_by_unique_key(self, unique_key: str) -> Optional[Dict[str, Any]]:
        for row in self._today_orders_cache:
            if row.get("uniqueKey") == unique_key and row.get("requestType") == 1:
                return row
        return None

    async def _resolve_numeric_order_id(self, unique_key: str) -> Optional[str]:
        if unique_key in self._mobin_numeric_order_ids:
            return self._mobin_numeric_order_ids[unique_key]
        await self._get_today_orders(force_refresh=True)
        row = self._find_today_order_by_unique_key(unique_key)
        if row and row.get("id") is not None:
            numeric_id = str(row["id"])
            self._mobin_numeric_order_ids[unique_key] = numeric_id
            self._mobin_unique_key_by_request_id[numeric_id] = unique_key
            return numeric_id
        return None

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        # Same race: strategy cancel at 30s is fine, but cancel during PENDING_CREATE must wait.
        unique_key = await tracked_order.get_exchange_order_id()
        numeric_id = await self._resolve_numeric_order_id(unique_key)
        if numeric_id is None:
            # nothing live to cancel
            raise IOError(f"Mobin order not found for cancel (uniqueKey={unique_key})")

        order_side = CONSTANTS.SIDE_BUY if tracked_order.trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        api_params = {"orderId": int(numeric_id), "orderSide": order_side}

        cancel_result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL,
        )
        code = int(cancel_result.get("requestErrorCode", 0) or 0)
        if cancel_result.get("success", True) and code == 0:
            return True

        # 1600 OriginalOrderIsNotInBook / 1104 OriginalOrderWasNotFound:
        # cancel/modify rejected because original order is gone (often filled).
        # Cancel did NOT succeed. Reconcile via Today; if Today lags, keep live.
        # Do NOT raise — that hits process_order_not_found → FAILED → double ROTATE.
        if code in (CONSTANTS.ORIGINAL_ORDER_IS_NOT_IN_BOOK,
                    CONSTANTS.ORIGINAL_ORDER_WAS_NOT_FOUND):
            await self._get_today_orders(force_refresh=True)
            row = self._find_today_order_by_unique_key(unique_key)
            if row is not None:
                order_update = OrderUpdate(
                    client_order_id=tracked_order.client_order_id,
                    exchange_order_id=unique_key,
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=self.current_timestamp,
                    new_state=self._map_today_row_to_state(row),
                )
                self._order_tracker.process_order_update(order_update)
            else:
                self.logger().warning(
                    f"Cancel code={code} but Today miss uniqueKey={unique_key}; "
                    f"leaving open for status poll"
                )
            return False

        raise IOError(f"Cancel rejected: {cancel_result}")

    async def _make_trading_rules_request(self) -> Any:
        exchange_info = await self._api_get(path_url=self.trading_rules_request_path, is_auth_required=True)
        return exchange_info

    async def _make_trading_pairs_request(self) -> Any:
        exchange_info = await self._api_get(path_url=self.trading_pairs_request_path, is_auth_required=True)
        return exchange_info

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Example:
        {
            "symbol": "ETHBTC",
            "baseAssetPrecision": 8,
            "quotePrecision": 8,
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.00000100",
                    "maxPrice": "100000.00000000",
                    "tickSize": "0.00000100"
                }, {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00100000",
                    "maxQty": "100000.00000000",
                    "stepSize": "0.00100000"
                }, {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": "0.00100000"
                }
            ]
        }
        """
        # exchange_info_dict is already a list
        trading_pair_rules = exchange_info_dict
        retval = []
        for rule in filter(mobin_utils.is_exchange_information_valid, trading_pair_rules):
            try:
                instrument_id = rule.get("instrumentId")
                if not instrument_id:
                    continue
                trading_pair = combine_to_hb_trading_pair(base=instrument_id, quote="IRR")

                min_order_size = Decimal(str(rule.get("orderMinimumQuantity", 1)))
                tick_size = Decimal(str(rule.get("fixedPriceTick", 1)))
                step_size = Decimal(str(rule.get("lot", 1)))
                # min amount of order value; 1M or 5M rial for ETFs set to 1_000 to sell small amounts for buy,
                # handle in strategy. let the Mobin API reject other situations
                min_notional = Decimal(1)
                retval.append(
                    TradingRule(trading_pair,
                                min_order_size=min_order_size,
                                min_price_increment=tick_size,
                                min_base_amount_increment=step_size,
                                min_notional_size=min_notional))

            except Exception:
                self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")
        return retval

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    @staticmethod
    def _decode_signalr_payload(b64_payload: str) -> Dict[str, Any]:
        decoded_bytes = gzip.decompress(base64.b64decode(b64_payload))
        return msgpack.unpackb(decoded_bytes, raw=False, strict_map_key=False)

    @staticmethod
    def _is_user_stream_message(message: Dict[str, Any]) -> bool:
        if message.get("type") == 6:
            return False
        if message.get("target") == "time":
            return False
        return message.get("target") == "sle" and "arguments" in message

    async def _resolve_unique_key_from_request_event(self, data: Dict[str, Any]) -> Optional[str]:
        request_ids = []
        if data.get("RequestId") is not None:
            request_ids.append(str(data["RequestId"]))
        if data.get("OriginalRequestId"):
            request_ids.append(str(data["OriginalRequestId"]))

        for request_id in request_ids:
            unique_key = self._mobin_unique_key_by_request_id.get(request_id)
            if unique_key:
                return unique_key

        await self._get_today_orders(force_refresh=True)
        for request_id in request_ids:
            for row in self._today_orders_cache:
                if str(row.get("id")) == request_id and row.get("requestType") == 1:
                    unique_key = row.get("uniqueKey")
                    if unique_key:
                        self._mobin_unique_key_by_request_id[request_id] = unique_key
                        self._mobin_numeric_order_ids[unique_key] = request_id
                        return unique_key
        return None

    @staticmethod
    def _map_request_event_to_state(data: Dict[str, Any]) -> Optional[OrderState]:
        event_type = data.get("RequestEventType")
        req_type = data.get("Type")

        if event_type == "RejectedByOMS":
            # Cancel rejected because order left the book (1600 OriginalOrderIsNotInBook)
            # or other cancel reject — do NOT fail the live order. REST /Orders/Today reconciles.
            if req_type == "Cancellation":
                return None
            return OrderState.FAILED

        if event_type == "Traded" and req_type == "Creation":
            remaining = Decimal(str(data.get("RemainingQuantity", 0)))
            if remaining > 0:
                return OrderState.PARTIALLY_FILLED
            return OrderState.FILLED

        if event_type == "Cancelled" and req_type == "Cancellation":
            executed = Decimal(str(data.get("ExecutedQuantity", 0)))
            # if partially/fully traded before cancel, treat as filled not canceled
            if executed > 0:
                remaining = Decimal(str(data.get("RemainingQuantity", 0)))
                return OrderState.FILLED if remaining <= 0 else OrderState.PARTIALLY_FILLED
            return OrderState.CANCELED

        if event_type == "SentByOMS" and req_type == "Cancellation":
            return OrderState.PENDING_CANCEL

        if event_type == "Created" and req_type == "Creation":
            if data.get("IsInBook"):
                return OrderState.OPEN
            return OrderState.PENDING_CREATE

        # Unknown / intermediate events — let REST polling handle them
        return None

    def _process_account_update(self, data: Dict[str, Any]) -> None:
        # WS Account payload updates IRR (quote) balance
        if "TradableRemain" in data:
            free = Decimal(str(data.get("TradableRemain", 0)))
            total = Decimal(str(data.get("Remain", data.get("TradableRemain", 0))))
            self._account_available_balances["IRR"] = free
            self._account_balances["IRR"] = total

    def _schedule_balance_refresh(self) -> None:
        if self._balance_refresh_task is not None and not self._balance_refresh_task.done():
            self._balance_refresh_task.cancel()
        self._balance_refresh_task = safe_ensure_future(self._debounced_balance_refresh())

    async def _debounced_balance_refresh(self) -> None:
        try:
            await self._sleep(self._balance_refresh_debounce_s)
            await self._update_balances()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger().warning("Failed to refresh Mobin balances after user-stream event.", exc_info=True)

    async def _user_stream_event_listener(self):
        """
        Processes Mobin private SignalR user stream events.

        Queue items are either:
          - a parsed dict (one frame), from MobinAPIUserStreamDataSource, or
          - a raw WS string with one or more frames separated by \\x1e
        """
        async for event_message in self._iter_user_event_queue():
            try:
                for message in iter_signalr_frames(event_message):
                    if not self._is_user_stream_message(message):
                        continue

                    event_name = message["arguments"][0]  # "Request" or "Account"
                    payload = self._decode_signalr_payload(message["arguments"][1])

                    if event_name == "Account":
                        self._process_account_update(payload)
                        continue

                    if event_name != "Request":
                        continue

                    unique_key = await self._resolve_unique_key_from_request_event(payload)
                    if unique_key is None:
                        self.logger().debug(
                            f"Untracked Mobin request event: "
                            f"RequestId={payload.get('RequestId')} "
                            f"OriginalRequestId={payload.get('OriginalRequestId')} "
                            f"event={payload.get('RequestEventType')} "
                            f"type={payload.get('Type')}"
                        )
                        continue

                    tracked_order = self._order_tracker.all_updatable_orders_by_exchange_order_id.get(unique_key)
                    if tracked_order is None:
                        continue

                    new_state = self._map_request_event_to_state(payload)
                    if new_state is None:
                        continue

                    ts_str = payload.get("DateTime") or payload.get("DateOfEvent")
                    misc_updates = None
                    if payload.get("Description"):
                        misc_updates = {"error_message": payload["Description"]}

                    order_update = OrderUpdate(
                        client_order_id=tracked_order.client_order_id,
                        exchange_order_id=unique_key,
                        trading_pair=tracked_order.trading_pair,
                        update_timestamp=self._parse_mobin_timestamp(ts_str),
                        new_state=new_state,
                        misc_updates=misc_updates,
                    )
                    self._order_tracker.process_order_update(order_update)

                    executed_qty = payload.get("ExecutedQuantity")
                    if executed_qty is not None:
                        executed_qty = Decimal(str(executed_qty))
                        if executed_qty > tracked_order.executed_amount_base:
                            fill_delta = executed_qty - tracked_order.executed_amount_base
                            if fill_delta > 0:
                                price = Decimal(str(payload.get("Price", tracked_order.price)))
                                fee = TradeFeeBase.new_spot_fee(
                                    fee_schema=self.trade_fee_schema(),
                                    trade_type=tracked_order.trade_type,
                                    percent=self.estimate_fee_pct(is_maker=True),
                                )
                                trade_update = TradeUpdate(
                                    trade_id=f"{payload.get('RequestId')}-{executed_qty}",
                                    client_order_id=tracked_order.client_order_id,
                                    exchange_order_id=unique_key,
                                    trading_pair=tracked_order.trading_pair,
                                    fee=fee,
                                    fill_base_amount=fill_delta,
                                    fill_quote_amount=fill_delta * price,
                                    fill_price=price,
                                    fill_timestamp=self._parse_mobin_timestamp(ts_str),
                                    is_taker=False,
                                )
                                self._order_tracker.process_trade_update(trade_update)
                                # Update balance, as balance is not updated via websocket. Only IRR is updated with
                                # Account message in websocket. For updating other assets use rest api
                                self._schedule_balance_refresh()

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        # Since Mobin has no per-fill API, /Orders/Today gives order-level aggregates,
        # not individual trades like Binance myTrades. You need to synthesize TradeUpdate objects
        # from each matching order row.

        trade_updates = []

        if order.exchange_order_id is not None:
            all_orders_response = await self._api_get(
                path_url=CONSTANTS.MY_ORDERS_PATH_URL,
                params={
                    "displayFailedRequest": "False"
                },
                is_auth_required=True,
                limit_id=CONSTANTS.MY_ORDERS_PATH_URL)

            matching_orders = [row for row in all_orders_response if
                               row.get("uniqueKey") == order.exchange_order_id
                               and row.get("requestType") == 1]

            for order_data in matching_orders:
                executed_qty = Decimal(str(order_data.get("executedQuantity", 0)))
                if executed_qty <= 0:
                    continue
                fill_delta = executed_qty - order.executed_amount_base
                if fill_delta <= 0:
                    continue

                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent=self.estimate_fee_pct(is_maker=True),  # get fee rate, equal for maker and taker
                )

                ts_str = order_data.get("orderTime") or order_data.get("insertDateTime")
                fill_price = Decimal(str(order_data["price"]))
                fill_quote = fill_delta * fill_price
                fill_timestamp = datetime.strptime(ts_str, "%d %b %Y %H:%M:%S.%f").timestamp()

                trade_updates.append(TradeUpdate(
                    trade_id=f"{order_data['id']}-{executed_qty}",
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,  # uniqueKey, not numeric id
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=fill_delta,
                    fill_quote_amount=fill_quote,
                    fill_price=fill_price,
                    fill_timestamp=fill_timestamp,
                    is_taker=False,
                ))

        return trade_updates

    def _map_today_row_to_state(self, row: Dict[str, Any]) -> OrderState:
        executed = Decimal(str(row.get("executedQuantity", 0)))
        remaining = Decimal(str(row.get("remainingQuantity", 0)))
        quantity = Decimal(str(row.get("quantity", 0)))
        order_state = row.get("orderState")

        # Fill before errorCode (1600 race can leave error-ish fields on a filled Creation row)
        if executed > 0 and (remaining <= 0 or executed >= quantity):
            return OrderState.FILLED
        if executed > 0 and remaining > 0:
            return OrderState.PARTIALLY_FILLED

        if row.get("errorCode"):
            return OrderState.FAILED
        if order_state == 3:
            return OrderState.OPEN
        if order_state == 5:
            return OrderState.FAILED
        if order_state == 7:
            return OrderState.CANCELED if executed == 0 else OrderState.FILLED
        return OrderState.OPEN

    def _parse_mobin_timestamp(self, ts_str: Optional[str]) -> float:
        if not ts_str:
            return self.current_timestamp
        # handles "08 Jun 2026 11:24:08.583952" and "2 June 2026 16:06:52.982"
        for fmt in ("%d %b %Y %H:%M:%S.%f", "%d %B %Y %H:%M:%S.%f"):
            try:
                return datetime.strptime(ts_str, fmt).timestamp()
            except ValueError:
                continue
        return self.current_timestamp

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        # Wait until place-order assigns exchange_order_id (uniqueKey), or TimeoutError (10s).
        # Base class handles TimeoutError without treating it as "Order not found in Today".
        unique_key = await tracked_order.get_exchange_order_id()
        await self._get_today_orders(force_refresh=True)
        row = self._find_today_order_by_unique_key(unique_key)

        if row is None:
            raise IOError(f"Order not found in Today for uniqueKey={unique_key}")

        if row.get("id") is not None:
            self._mobin_numeric_order_ids[unique_key] = str(row["id"])
            self._mobin_unique_key_by_request_id[str(row["id"])] = unique_key

        new_state = self._map_today_row_to_state(row)
        ts_str = row.get("lastUpdate") or row.get("orderTime") or row.get("insertDateTime")

        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=unique_key,  # keep uniqueKey as exchange_order_id
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self._parse_mobin_timestamp(ts_str),
            new_state=new_state,
        )

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        # Mobin has two different endpoints to get balances, one for base_assets (PORTFOLIO_PATH_URL) and one for
        # quote_asset (ACCOUNTS_PATH_URL)
        account_info = await self._api_get(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            is_auth_required=True)

        # update IRR
        asset_name = "IRR"
        free_balance = Decimal(account_info["remain"]) - Decimal(account_info["block"])
        total_balance = Decimal(account_info["remain"])
        self._account_available_balances[asset_name] = free_balance
        self._account_balances[asset_name] = total_balance
        remote_asset_names.add(asset_name)

        portfolio_info = await self._api_get(
            path_url=CONSTANTS.PORTFOLIO_PATH_URL,
            is_auth_required=True)

        for balance_entry in portfolio_info:
            asset_name = balance_entry["instrumentId"]
            total_balance = Decimal(balance_entry["runTimeAsset"])
            sell_locked = Decimal(balance_entry["sellOpenOrderQuantity"])
            free_balance = total_balance - sell_locked
            self._account_available_balances[asset_name] = free_balance
            self._account_balances[asset_name] = total_balance
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    async def _update_time_synchronizer(self, pass_on_non_cancelled_error: bool = False):
        """
        Mobin uses JWT Bearer auth, not timestamp-based HMAC.
        Server time synchronization is not required.
        """
        return

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(mobin_utils.is_exchange_information_valid, exchange_info):
            instrument_id = symbol_data.get("instrumentId")
            if not instrument_id:
                continue

            mapping[instrument_id] = combine_to_hb_trading_pair(base=instrument_id, quote='IRR')

        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        params = {
            "id": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        }

        resp_json = await self._api_request(
            method=RESTMethod.GET,
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_PATH_URL,
            params=params,
            is_auth_required=True
        )

        return float(resp_json["lastTradePrice"])

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        return trading_pair.split("-")[0]

    async def _make_network_check_request(self):
        await self._api_get(
            path_url=CONSTANTS.PING_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.PING_PATH_URL
        )
