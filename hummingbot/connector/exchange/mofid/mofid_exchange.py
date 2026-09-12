import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_utils, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source import MofidAPIOrderBookDataSource
from hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source import MofidAPIUserStreamDataSource
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class MofidExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(
            self,
            mofid_api_key: str,
            mofid_api_secret: str,
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DEFAULT_DOMAIN,
            client_config_map: Optional["ClientConfigAdapter"] = None,
            balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
            rate_limits_share_pct: Decimal = Decimal("100"),
    ):
        self.api_key = mofid_api_key
        self.secret_key = mofid_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._symbol_names: Dict[str, str] = {}  # isin -> display name for place body
        self._symbol_commissions: Dict[str, Decimal] = {}  # isin -> commission rate
        self._open_orders_cache: List[Dict[str, Any]] = []
        self._open_orders_cache_ts: float = 0.0
        self._balance_refresh_task: Optional[asyncio.Task] = None
        self._balance_refresh_debounce_s = 0.5
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        # Money WS meta is opaque; balances refresh via REST debounce, not inline WS fields.
        self.real_time_balance_update = False

    @property
    def authenticator(self):
        return MofidAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer,
        )

    @property
    def name(self) -> str:
        if self._domain == "ir":
            return "mofid"
        return f"mofid_{self._domain}"

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
        # DELETE returns isSuccessful immediately; WS CancelByBroker follows.
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT]

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "Mofid order not found" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "Mofid order not found" in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth,
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return MofidAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return MofidAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(
            self,
            base_currency: str,
            quote_currency: str,
            order_type: OrderType,
            order_side: TradeType,
            amount: Decimal,
            price: Decimal = s_decimal_NaN,
            is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        commission = self._symbol_commissions.get(base_currency)
        if commission is not None:
            return DeductedFromReturnsTradeFee(percent=commission)
        is_maker = order_type is OrderType.LIMIT_MAKER
        return DeductedFromReturnsTradeFee(percent=self.estimate_fee_pct(is_maker))

    @staticmethod
    def _format_create_date_time() -> str:
        # HAR: "9/5/2026, 5:50:48 PM"
        now = datetime.now()
        hour_12 = now.hour % 12 or 12
        am_pm = "AM" if now.hour < 12 else "PM"
        return f"{now.month}/{now.day}/{now.year}, {hour_12}:{now.minute:02d}:{now.second:02d} {am_pm}"

    def _commission_for_isin(self, isin: str) -> Decimal:
        return self._symbol_commissions.get(isin, Decimal("0.0012"))

    @staticmethod
    def _format_oms_rejection(action: str, result: Dict[str, Any]) -> str:
        """Build a clear error from place/cancel OMS body.

        Live failed place (price band): isSuccessful=False but still returns an orphan id —
        do not treat that id as an accepted exchange order. Prefer omsError code/name/error
        (e.g. 7003 PriceIsNotInRangeError), else message.
        """
        parts: List[str] = []
        for err in result.get("omsError") or []:
            if not isinstance(err, dict):
                continue
            code = err.get("code")
            name = err.get("name") or ""
            text = err.get("error") or ""
            chunk = " ".join(str(x) for x in (code, name, text) if x not in (None, ""))
            if chunk:
                parts.append(chunk)
        detail = "; ".join(parts) if parts else (result.get("message") or str(result))
        return f"Mofid {action} rejected: {detail}"

    async def _place_order(
            self,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            trade_type: TradeType,
            order_type: OrderType,
            price: Decimal,
            **kwargs,
    ) -> Tuple[str, float]:
        isin = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        side = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        commission = self._commission_for_isin(isin)
        price_i = int(price)
        qty_i = int(amount)
        notional = Decimal(price_i) * Decimal(qty_i)
        if trade_type is TradeType.BUY:
            total_value = int(notional * (Decimal("1") + commission))
        else:
            total_value = int(notional * (Decimal("1") - commission))

        api_params = {
            "order": {
                "price": price_i,
                "quantity": qty_i,
                "side": side,
                "validityType": CONSTANTS.VALIDITY_TYPE_DAY,
                "createDateTime": self._format_create_date_time(),
                "commission": float(commission),
                "symbolIsin": isin,
                "symbolName": self._symbol_names.get(isin, ""),
                "orderModelType": CONSTANTS.ORDER_MODEL_TYPE_LIMIT,
                "totalValue": total_value,
                "orderFrom": CONSTANTS.ORDER_FROM,
            }
        }

        order_result = await self._api_post(
            path_url=CONSTANTS.ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_PATH_URL,
        )

        # Strict True: failed responses may still include a disposable "id".
        if order_result.get("isSuccessful") is not True:
            raise IOError(self._format_oms_rejection("place order", order_result))

        exchange_order_id = str(order_result["id"])
        # Live: buyPower drops / block rises as soon as order is OnBoard; don't wait only on money WS.
        self._schedule_balance_refresh()
        return exchange_order_id, self.current_timestamp

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        cancel_result = await self._api_delete(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data={"orderId": exchange_order_id, "orderFrom": CONSTANTS.ORDER_FROM},
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL,
        )
        if cancel_result.get("isSuccessful") is True:
            # Live: cancel clears block and restores buyPowerT0 promptly.
            self._schedule_balance_refresh()
            return True
        raise IOError(self._format_oms_rejection("cancel order", cancel_result))

    async def _make_network_check_request(self):
        # Same authenticated server-time endpoint used for TimeSynchronizer.
        path_url = web_utils.server_time_path()
        await self._api_get(
            path_url=path_url,
            is_auth_required=True,
            limit_id=CONSTANTS.SERVER_TIME_PATH_URL,
        )

    async def _update_time_synchronizer(self, pass_on_non_cancelled_error: bool = False):
        # Prefer connector auth path: standalone web_utils call without Bearer returns 401.
        try:
            await self._time_synchronizer.update_server_time_offset_with_time_provider(
                time_provider=self._get_current_server_time_ms()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if not pass_on_non_cancelled_error:
                self.logger().exception(f"Error requesting time from {self.name_cap} server")
                raise

    async def _get_current_server_time_ms(self) -> float:
        """Return Mofid serverTimestamp (epoch ms) for TimeSynchronizer."""
        response = await self._api_get(
            path_url=web_utils.server_time_path(),
            is_auth_required=True,
            limit_id=CONSTANTS.SERVER_TIME_PATH_URL,
        )
        return web_utils.parse_server_timestamp_ms(response)

    async def _make_trading_rules_request(self) -> Any:
        return await self._api_post(
            path_url=CONSTANTS.EXCHANGE_INFO_PATH_URL,
            data={"hash": ""},
            is_auth_required=True,
            limit_id=CONSTANTS.EXCHANGE_INFO_PATH_URL,
        )

    async def _make_trading_pairs_request(self) -> Any:
        return await self._make_trading_rules_request()

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        symbols = exchange_info_dict.get("symbols") or []
        retval = []
        for rule in filter(mofid_utils.is_exchange_information_valid, symbols):
            try:
                isin = rule.get("symbolIsin")
                if not isin:
                    continue
                trading_pair = combine_to_hb_trading_pair(base=isin, quote=CONSTANTS.QUOTE_ASSET)
                min_buy = Decimal(str(rule.get("minValidBuyVolume", 1)))
                min_sell = Decimal(str(rule.get("minValidSellVolume", 1)))
                min_order_size = min(min_buy, min_sell)
                tick_size = Decimal(str(rule.get("minDealablePrice", 1)))
                step_size = Decimal(str(rule.get("minDealableCount", 1)))
                # Broker enforces min sell notional (~500k IRR) server-side; leave loose here.
                min_notional = Decimal("1")

                name = rule.get("symbolName") or ""
                self._symbol_names[isin] = name
                buy_c = Decimal(str(rule.get("buyCommission", "0")))
                sell_c = Decimal(str(rule.get("sellCommission", "0")))
                # Use max so fee estimate never under-charges vs either side.
                self._symbol_commissions[isin] = max(buy_c, sell_c) if (buy_c or sell_c) else Decimal("0.0012")

                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=min_order_size,
                        min_price_increment=tick_size,
                        min_base_amount_increment=step_size,
                        min_notional_size=min_notional,
                    )
                )
            except Exception:
                self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")
        return retval

    async def _update_trading_fees(self):
        pass

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        symbols = exchange_info.get("symbols") or []
        for symbol_data in filter(mofid_utils.is_exchange_information_valid, symbols):
            isin = symbol_data.get("symbolIsin")
            if not isin:
                continue
            mapping[isin] = combine_to_hb_trading_pair(base=isin, quote=CONSTANTS.QUOTE_ASSET)
        self._set_trading_pair_symbol_map(mapping)

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        return trading_pair.split("-")[0]

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        # Prefer live cache from order-book symbol stream when present.
        ds = self.order_book_tracker.data_source
        if isinstance(ds, MofidAPIOrderBookDataSource):
            cached = ds._last_traded_prices.get(trading_pair)
            if cached is not None:
                return float(cached)

        isin = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        snapshot = await self._api_get(
            path_url=f"{CONSTANTS.SNAPSHOT_PATH_URL}/{isin}",
            is_auth_required=True,
            limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
        )
        buys = snapshot.get("buySheets") or []
        sells = snapshot.get("sellSheets") or []
        best_bid = float(buys[0]["price"]) if buys else None
        best_ask = float(sells[0]["price"]) if sells else None
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2.0
        if best_bid is not None:
            return best_bid
        if best_ask is not None:
            return best_ask
        raise IOError(f"No price available for {trading_pair}")

    async def _get_open_orders(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        now = self.current_timestamp
        if not force_refresh and now - self._open_orders_cache_ts < 5.0:
            return self._open_orders_cache
        response = await self._api_get(
            path_url=CONSTANTS.OPEN_ORDERS_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.OPEN_ORDERS_PATH_URL,
        )
        self._open_orders_cache = response.get("orders") or []
        self._open_orders_cache_ts = now
        return self._open_orders_cache

    def _find_open_order(self, exchange_order_id: str) -> Optional[Dict[str, Any]]:
        for row in self._open_orders_cache:
            if str(row.get("id")) == str(exchange_order_id):
                return row
        return None

    @staticmethod
    def _map_rest_order_to_state(row: Dict[str, Any]) -> OrderState:
        """Map GET /core/api/order row → HB OrderState.

        Live: filled orders stay on this list briefly with orderState=20 / OrderExecuted
        and executedQuantity; treat that as FILLED (not OPEN).
        """
        executed = Decimal(str(row.get("executedQuantity", 0) or 0))
        quantity = Decimal(str(row.get("quantity", 0) or 0))
        order_state = row.get("orderState")

        # Prefer quantity evidence when present (open list can lag on state int).
        if quantity > 0 and executed >= quantity:
            return OrderState.FILLED
        if executed > 0 and executed < quantity:
            return OrderState.PARTIALLY_FILLED

        if order_state in CONSTANTS.ORDER_STATE:
            return CONSTANTS.ORDER_STATE[order_state]

        # Fallback: orderStateStr from same payload (e.g. "OnSending").
        state_str = row.get("orderStateStr")
        if state_str in CONSTANTS.WS_ORDER_STATE:
            return CONSTANTS.WS_ORDER_STATE[state_str]

        return OrderState.OPEN

    def _parse_mofid_timestamp(self, ts_str: Optional[str]) -> float:
        if not ts_str:
            return self.current_timestamp
        # REST: "2026-09-05T17:53:29.2545955+03:30" or "2026-09-05T17:58:15"
        # WS Trade date: "20260905175354"
        cleaned = ts_str.replace("Z", "+00:00")
        try:
            if "T" in cleaned:
                # trim >6 fractional digits
                if "." in cleaned:
                    head, rest = cleaned.split(".", 1)
                    frac = ""
                    tz = ""
                    for i, ch in enumerate(rest):
                        if ch.isdigit():
                            frac += ch
                        else:
                            tz = rest[i:]
                            break
                    cleaned = f"{head}.{frac[:6]}{tz}"
                return datetime.fromisoformat(cleaned).timestamp()
            if len(cleaned) == 14 and cleaned.isdigit():
                return datetime.strptime(cleaned, "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass
        return self.current_timestamp

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        await self._get_open_orders(force_refresh=True)
        row = self._find_open_order(exchange_order_id)

        if row is not None:
            return OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self._parse_mofid_timestamp(row.get("createDateTime")),
                new_state=self._map_rest_order_to_state(row),
                misc_updates={"error_message": row.get("error")} if row.get("error") else None,
            )

        # Not in open book — check fills; empty fills ⇒ treat as canceled.
        trades = await self._fetch_order_trades(exchange_order_id)
        if trades:
            last = trades[-1]
            if not last.get("hasRemain", True) or Decimal(str(last.get("remain", 1))) == 0:
                new_state = OrderState.FILLED
            else:
                new_state = OrderState.PARTIALLY_FILLED
            return OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self._parse_mofid_timestamp(last.get("date") or last.get("createDateTime")),
                new_state=new_state,
            )

        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=OrderState.CANCELED,
        )

    async def _fetch_order_trades(self, exchange_order_id: str) -> List[Dict[str, Any]]:
        # Live (tmp3): GET /easy/api/orderHistory/trades/ + header order-id=<place id / isr>.
        # Correct shape is a bare list; can lag ~0.5s–minutes behind OMS (empty [] right after fill).
        result = await self._api_get(
            path_url=CONSTANTS.ORDER_TRADES_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_TRADES_PATH_URL,
            headers={"order-id": str(exchange_order_id)},
        )
        if isinstance(result, list):
            return result
        return result.get("trades") or result.get("records") or []

    def _trade_updates_from_raw_trades(
            self,
            order: InFlightOrder,
            trades: List[Dict[str, Any]],
    ) -> List[TradeUpdate]:
        """Parse fill rows from either open-order embed or orderHistory/trades."""
        trade_updates: List[TradeUpdate] = []
        for trade in trades:
            # Embed uses isCancel; history uses isCanceled (null when live).
            if trade.get("isCanceled") or trade.get("isCancel"):
                continue
            qty = Decimal(str(trade.get("quantity", 0)))
            if qty <= 0:
                continue
            price = Decimal(str(trade["price"]))
            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=order.trade_type,
                percent=self._commission_for_isin(order.base_asset),
            )
            trade_id = str(trade.get("tradeNumber") or trade.get("id"))
            # Embed: dateTime; history: date / createDateTime (live probe 2026-09-12).
            ts_raw = (
                trade.get("dateTime")
                or trade.get("date")
                or trade.get("createDateTime")
            )
            trade_updates.append(
                TradeUpdate(
                    trade_id=trade_id,
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=qty,
                    fill_quote_amount=qty * price,
                    fill_price=price,
                    fill_timestamp=self._parse_mofid_timestamp(ts_raw),
                    is_taker=False,
                )
            )
        return trade_updates

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        if order.exchange_order_id is None:
            return []

        exchange_order_id = order.exchange_order_id
        # Primary: GET /core/api/order → orders[].trades (same poll as status).
        # Live: embed appears with orderState=20 / executedQuantity within ~0.5s; do not wait on history.
        row = self._find_open_order(exchange_order_id)
        if row is None:
            await self._get_open_orders(force_refresh=True)
            row = self._find_open_order(exchange_order_id)

        embedded = list((row or {}).get("trades") or [])
        if embedded:
            return self._trade_updates_from_raw_trades(order, embedded)

        # Fallback when order already dropped off the open list or embed still empty.
        return self._trade_updates_from_raw_trades(
            order,
            await self._fetch_order_trades(exchange_order_id),
        )

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        money = await self._api_get(
            path_url=CONSTANTS.MONEY_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.MONEY_PATH_URL,
        )
        quote = CONSTANTS.QUOTE_ASSET
        # Live GET /easy/api/money (tmp3): buyPowerT0 = spendable; block/blockT2 = open-order lock.
        # While a buy rests: buyPower falls, block rises, t2 stays flat — so total ≠ t2 for HB.
        # Use available=buyPowerT0, total=buyPowerT0+block (prefer block, else blockT2).
        available = Decimal(str(money.get("buyPowerT0", 0) or 0))
        blocked = Decimal(str(money.get("block", money.get("blockT2", 0)) or 0))
        total = available + blocked
        self._account_available_balances[quote] = available
        self._account_balances[quote] = total
        remote_asset_names.add(quote)

        portfolio = await self._api_get(
            path_url=CONSTANTS.PORTFOLIO_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.PORTFOLIO_PATH_URL,
        )
        for item in portfolio.get("items") or []:
            isin = item.get("symbolIsin")
            if not isin:
                continue
            asset_qty = Decimal(str(item.get("asset", 0)))
            self._account_available_balances[isin] = asset_qty
            self._account_balances[isin] = asset_qty
            remote_asset_names.add(isin)

        for asset_name in local_asset_names.difference(remote_asset_names):
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

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
            self.logger().warning("Failed to refresh Mofid balances after user-stream event.", exc_info=True)

    def _process_money_event(self, meta: Any) -> None:
        # Lightstreamer money payload is opaque meta; refresh via REST.
        self._schedule_balance_refresh()

    def _trade_update_from_ws_trade(
            self,
            tracked_order: InFlightOrder,
            payload: Dict[str, Any],
    ) -> TradeUpdate:
        qty = Decimal(str(payload.get("quantity", 0)))
        price = Decimal(str(payload.get("price", tracked_order.price)))
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=self.trade_fee_schema(),
            trade_type=tracked_order.trade_type,
            percent=self._commission_for_isin(tracked_order.base_asset),
        )
        return TradeUpdate(
            trade_id=str(payload.get("tradeNumber")),
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=tracked_order.exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            fee=fee,
            fill_base_amount=qty,
            fill_quote_amount=qty * price,
            fill_price=price,
            fill_timestamp=self._parse_mofid_timestamp(payload.get("date")),
            is_taker=False,
        )

    async def _user_stream_event_listener(self):
        """
        Processes private Lightstreamer events queued by MofidAPIUserStreamDataSource.

        Queue items: {"e": "order"|"money"|"login", "meta": <json-or-str>, ...}
        Order meta is OrderResult or Trade JSON (field isr = exchange order id).
        """
        async for event_message in self._iter_user_event_queue():
            try:
                event_type = event_message.get("e")
                if event_type == CONSTANTS.USER_MONEY_EVENT_TYPE:
                    self._process_money_event(event_message.get("meta"))
                    continue
                if event_type != CONSTANTS.USER_ORDER_EVENT_TYPE:
                    continue

                meta = event_message.get("meta")
                if isinstance(meta, str):
                    try:
                        payload = json.loads(meta)
                    except json.JSONDecodeError:
                        continue
                elif isinstance(meta, dict):
                    payload = meta
                else:
                    continue

                exchange_order_id = str(payload.get("isr") or payload.get("orderId") or "")
                if not exchange_order_id:
                    continue

                tracked_order = self._order_tracker.all_updatable_orders_by_exchange_order_id.get(exchange_order_id)
                if tracked_order is None:
                    continue

                msg_type = payload.get("type")
                state_str = payload.get("state")
                new_state = CONSTANTS.WS_ORDER_STATE.get(state_str) if state_str else None

                if msg_type == "Trade" and Decimal(str(payload.get("quantity", 0))) > 0:
                    self._order_tracker.process_trade_update(
                        self._trade_update_from_ws_trade(tracked_order, payload)
                    )
                    self._schedule_balance_refresh()

                if new_state is not None:
                    # Ensure fills land before FILLED terminal state.
                    if new_state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) and msg_type != "Trade":
                        await self._update_orders_fills(orders=[tracked_order])
                    order_update = OrderUpdate(
                        client_order_id=tracked_order.client_order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=tracked_order.trading_pair,
                        update_timestamp=self._parse_mofid_timestamp(payload.get("date")),
                        new_state=new_state,
                    )
                    self._order_tracker.process_order_update(order_update)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    def _get_poll_interval(self, timestamp: float) -> float:
        if self.in_flight_orders:
            return self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        return super()._get_poll_interval(timestamp)
