import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.nobitex import (
    nobitex_constants as CONSTANTS,
    nobitex_utils,
    nobitex_web_utils as web_utils,
)
from hummingbot.connector.exchange.nobitex.nobitex_api_order_book_data_source import NobitexAPIOrderBookDataSource
from hummingbot.connector.exchange.nobitex.nobitex_api_user_stream_data_source import NobitexAPIUserStreamDataSource
from hummingbot.connector.exchange.nobitex.nobitex_auth import NobitexAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import TradeFillOrderDetails, combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class NobitexExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(
            self,
            nobitex_api_key: str,
            nobitex_api_secret: str,
            balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
            rate_limits_share_pct: Decimal = Decimal("100"),
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        self.api_key = nobitex_api_key
        self.secret_key = nobitex_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs or []
        self._last_trades_poll_nobitex_timestamp = 1.0
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def nobitex_execution_type(order_type: OrderType) -> str:
        if order_type == OrderType.MARKET:
            return CONSTANTS.EXECUTION_MARKET
        return CONSTANTS.EXECUTION_LIMIT

    @property
    def authenticator(self):
        return NobitexAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer,
        )

    @property
    def name(self) -> str:
        return "nobitex" if self._domain == CONSTANTS.DEFAULT_DOMAIN else f"nobitex_{self._domain}"

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
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception) -> bool:
        # Nobitex uses Ed25519 timestamp headers; no Binance-style -1021 clock skew code.
        text = str(request_exception).lower()
        return "timestamp" in text and ("invalid" in text or "expired" in text)

    def _is_nobitex_order_not_found(self, error: Exception) -> bool:
        text = str(error).lower()
        return (
            "status is 404" in text
            or "notfound" in text
            or "matches the given query" in text
            or "not found" in text
            or "does not exist" in text
        )

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return self._is_nobitex_order_not_found(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return self._is_nobitex_order_not_found(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth,
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return NobitexAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return NobitexAPIUserStreamDataSource(
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
        is_maker = True if is_maker is None else is_maker
        return DeductedFromReturnsTradeFee(percent=self.estimate_fee_pct(is_maker))

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
        src, dst = nobitex_utils.currencies_from_trading_pair(trading_pair)
        api_params: Dict[str, Any] = {
            "type": CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL,
            "srcCurrency": src,
            "dstCurrency": dst,
            "amount": f"{amount:f}",
            "execution": self.nobitex_execution_type(order_type),
            "clientOrderId": order_id,
        }
        if order_type is OrderType.LIMIT or order_type is OrderType.MARKET:
            # Market: price is an optional safety bound; still send when provided.
            if price is not None and price.is_nan() is False:
                api_params["price"] = f"{price:f}"

        order_result = await self._api_post(
            path_url=CONSTANTS.ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_PATH_URL,
        )
        if not isinstance(order_result, dict) or order_result.get("status") != "ok":
            raise IOError(f"Nobitex place order failed: {order_result}")

        order = order_result.get("order") or {}
        o_id = order.get("id")
        if o_id in (None, ""):
            raise IOError(f"Nobitex place order ok but missing id: {order_result}")
        o_id = str(o_id)
        created = order.get("created_at")
        if created:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        else:
            ts = self._time_synchronizer.time()
        return o_id, ts

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        api_params: Dict[str, Any] = {"status": "canceled"}
        if tracked_order.exchange_order_id and tracked_order.exchange_order_id != "UNKNOWN":
            api_params["order"] = int(tracked_order.exchange_order_id)
        else:
            api_params["clientOrderId"] = order_id

        cancel_result = await self._api_post(
            path_url=CONSTANTS.ORDER_CANCEL_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_CANCEL_PATH_URL,
        )
        if not isinstance(cancel_result, dict):
            return False
        if cancel_result.get("status") == "failed":
            return False
        updated = cancel_result.get("updatedStatus") or (cancel_result.get("order") or {}).get("status")
        return updated == "Canceled" or cancel_result.get("status") == "ok"

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        nobitex = exchange_info_dict.get("nobitex") or {}
        amount_precisions: Dict[str, str] = nobitex.get("amountPrecisions") or {}
        price_precisions: Dict[str, str] = nobitex.get("pricePrecisions") or {}
        min_orders: Dict[str, str] = nobitex.get("minOrders") or {}

        retval: List[TradingRule] = []
        for symbol, amount_step in amount_precisions.items():
            if not nobitex_utils.is_exchange_information_valid(symbol):
                continue
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=symbol)
                base, quote = nobitex_utils.split_exchange_symbol(symbol)
                api_quote = nobitex_utils.hb_quote_to_api(quote)
                min_notional = Decimal(str(min_orders.get(api_quote, "0")))
                price_step = Decimal(str(price_precisions.get(symbol, "0.01")))
                step_size = Decimal(str(amount_step))
                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=step_size,
                        min_price_increment=price_step,
                        min_base_amount_increment=step_size,
                        min_notional_size=min_notional,
                    )
                )
            except Exception:
                self.logger().exception(f"Error parsing trading pair rule {symbol}. Skipping.")
        return retval

    async def _status_polling_loop_fetch_updates(self):
        await self._update_order_fills_from_trades()
        await super()._status_polling_loop_fetch_updates()

    async def _update_trading_fees(self):
        pass

    def _order_state_from_nobitex(self, order_data: Dict[str, Any]) -> OrderState:
        status = order_data.get("status", "")
        state = CONSTANTS.ORDER_STATE.get(status, OrderState.FAILED)
        # Partial fill while still open
        if state is OrderState.OPEN:
            matched = Decimal(str(order_data.get("matchedAmount") or order_data.get("filledAmount") or "0"))
            if matched > 0:
                return OrderState.PARTIALLY_FILLED
        if state is OrderState.FILLED:
            unmatched = Decimal(str(order_data.get("unmatchedAmount") or "0"))
            if unmatched > 0 and order_data.get("partial"):
                return OrderState.PARTIALLY_FILLED
        return state

    async def _user_stream_event_listener(self):
        async for event_message in self._iter_user_event_queue():
            try:
                channel = event_message.get("_channel", "")
                if channel.startswith(CONSTANTS.WS_PRIVATE_TRADES_CHANNEL_PREFIX):
                    await self._process_user_trade_event(event_message)
                elif channel.startswith(CONSTANTS.WS_PRIVATE_ORDERS_CHANNEL_PREFIX):
                    await self._process_user_order_event(event_message)
                else:
                    # Some payloads omit _channel or nest differently — try heuristics
                    if "status" in event_message and (
                        "orderId" in event_message
                        or "id" in event_message
                        or "clientOrderId" in event_message
                    ):
                        await self._process_user_order_event(event_message)
                    elif "orderId" in event_message and "price" in event_message and "amount" in event_message:
                        await self._process_user_trade_event(event_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _process_user_order_event(self, event_message: Dict[str, Any]):
        client_order_id = event_message.get("clientOrderId")
        # WS docs use orderId; REST-shaped payloads may use id. Never store "".
        raw_id = event_message.get("orderId", event_message.get("id"))
        exchange_order_id = str(raw_id) if raw_id not in (None, "") else None
        tracked_order = None
        if client_order_id:
            tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
        if tracked_order is None and exchange_order_id:
            for order in self._order_tracker.all_updatable_orders.values():
                if order.exchange_order_id == exchange_order_id:
                    tracked_order = order
                    client_order_id = order.client_order_id
                    break
        if tracked_order is None:
            return

        if event_message.get("status") == "Failed":
            self._update_order_after_failure(
                order_id=tracked_order.client_order_id,
                trading_pair=tracked_order.trading_pair,
                exception=IOError(f"Nobitex order failed: {event_message}"),
            )
            return

        new_state = self._order_state_from_nobitex(event_message)
        if new_state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            await self._update_orders_fills(orders=[tracked_order])

        created = event_message.get("created_at")
        if created:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        else:
            event_time = event_message.get("eventTime")
            ts = (float(event_time) / 1000.0) if event_time else self.current_timestamp

        self._order_tracker.process_order_update(
            OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=ts,
                new_state=new_state,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
            )
        )

    async def _process_user_trade_event(self, event_message: Dict[str, Any]):
        exchange_order_id = str(event_message.get("orderId", ""))
        tracked_order = None
        for order in self._order_tracker.all_fillable_orders.values():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                break
        if tracked_order is None:
            return

        fee_amount = Decimal(str(event_message.get("fee") or "0"))
        # Fee is usually in quote (dst); use trading pair quote
        fee_token = tracked_order.quote_asset
        fee = TradeFeeBase.new_spot_fee(
            fee_schema=self.trade_fee_schema(),
            trade_type=tracked_order.trade_type,
            percent_token=fee_token,
            flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)],
        )
        amount = Decimal(str(event_message["amount"]))
        price = Decimal(str(event_message["price"]))
        quote_amount = Decimal(str(event_message.get("total") or (amount * price)))

        ts_raw = event_message.get("timestamp")
        if ts_raw:
            fill_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
        else:
            fill_ts = self.current_timestamp

        self._order_tracker.process_trade_update(
            TradeUpdate(
                trade_id=str(event_message["id"]),
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                fee=fee,
                fill_base_amount=amount,
                fill_quote_amount=quote_amount,
                fill_price=price,
                fill_timestamp=fill_ts,
            )
        )

    async def _update_order_fills_from_trades(self):
        small_interval_last_tick = self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        small_interval_current_tick = self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        long_interval_last_tick = self._last_poll_timestamp / self.LONG_POLL_INTERVAL
        long_interval_current_tick = self.current_timestamp / self.LONG_POLL_INTERVAL

        if not (long_interval_current_tick > long_interval_last_tick
                or (self.in_flight_orders and small_interval_current_tick > small_interval_last_tick)):
            return

        self._last_trades_poll_nobitex_timestamp = self._time_synchronizer.time()
        order_by_exchange_id_map = {
            order.exchange_order_id: order
            for order in self._order_tracker.all_fillable_orders.values()
            if order.exchange_order_id
        }

        tasks = []
        trading_pairs = list(self.trading_pairs)
        for trading_pair in trading_pairs:
            src, dst = nobitex_utils.currencies_from_trading_pair(trading_pair)
            params = {"srcCurrency": src, "dstCurrency": dst, "pageSize": "100"}
            tasks.append(self._api_get(
                path_url=CONSTANTS.MY_TRADES_PATH_URL,
                params=params,
                is_auth_required=True,
                limit_id=CONSTANTS.MY_TRADES_PATH_URL,
            ))

        results = await safe_gather(*tasks, return_exceptions=True)
        for trades_resp, trading_pair in zip(results, trading_pairs):
            if isinstance(trades_resp, Exception):
                self.logger().network(
                    f"Error fetching trades update for {trading_pair}: {trades_resp}.",
                    app_warning_msg=f"Failed to fetch trade update for {trading_pair}.",
                )
                continue
            trades = trades_resp.get("trades", []) if isinstance(trades_resp, dict) else []
            for trade in trades:
                exchange_order_id = str(trade.get("orderId"))
                if exchange_order_id in order_by_exchange_id_map:
                    tracked_order = order_by_exchange_id_map[exchange_order_id]
                    fee_amount = Decimal(str(trade.get("fee") or "0"))
                    fee = TradeFeeBase.new_spot_fee(
                        fee_schema=self.trade_fee_schema(),
                        trade_type=tracked_order.trade_type,
                        percent_token=tracked_order.quote_asset,
                        flat_fees=[TokenAmount(amount=fee_amount, token=tracked_order.quote_asset)],
                    )
                    amount = Decimal(str(trade["amount"]))
                    price = Decimal(str(trade["price"]))
                    fill_ts = datetime.fromisoformat(
                        str(trade["timestamp"]).replace("Z", "+00:00")
                    ).timestamp()
                    self._order_tracker.process_trade_update(
                        TradeUpdate(
                            trade_id=str(trade["id"]),
                            client_order_id=tracked_order.client_order_id,
                            exchange_order_id=exchange_order_id,
                            trading_pair=trading_pair,
                            fee=fee,
                            fill_base_amount=amount,
                            fill_quote_amount=Decimal(str(trade.get("total") or amount * price)),
                            fill_price=price,
                            fill_timestamp=fill_ts,
                        )
                    )
                elif self.is_confirmed_new_order_filled_event(
                        str(trade["id"]), exchange_order_id, trading_pair):
                    self._current_trade_fills.add(TradeFillOrderDetails(
                        market=self.display_name,
                        exchange_trade_id=str(trade["id"]),
                        symbol=trading_pair,
                    ))
                    amount = Decimal(str(trade["amount"]))
                    price = Decimal(str(trade["price"]))
                    fill_ts = datetime.fromisoformat(
                        str(trade["timestamp"]).replace("Z", "+00:00")
                    ).timestamp()
                    self.trigger_event(
                        MarketEvent.OrderFilled,
                        OrderFilledEvent(
                            timestamp=fill_ts,
                            order_id=self._exchange_order_ids.get(exchange_order_id),
                            trading_pair=trading_pair,
                            trade_type=TradeType.BUY if trade.get("type") == "buy" else TradeType.SELL,
                            order_type=OrderType.LIMIT,
                            price=price,
                            amount=amount,
                            trade_fee=DeductedFromReturnsTradeFee(
                                flat_fees=[TokenAmount(
                                    trading_pair.split("-")[1],
                                    Decimal(str(trade.get("fee") or "0")),
                                )]
                            ),
                            exchange_trade_id=str(trade["id"]),
                        ),
                    )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates: List[TradeUpdate] = []
        if order.exchange_order_id is None:
            return trade_updates

        src, dst = nobitex_utils.currencies_from_trading_pair(order.trading_pair)
        response = await self._api_get(
            path_url=CONSTANTS.MY_TRADES_PATH_URL,
            params={"srcCurrency": src, "dstCurrency": dst, "pageSize": "100"},
            is_auth_required=True,
            limit_id=CONSTANTS.MY_TRADES_PATH_URL,
        )
        trades = response.get("trades", []) if isinstance(response, dict) else []
        for trade in trades:
            if str(trade.get("orderId")) != str(order.exchange_order_id):
                continue
            fee_amount = Decimal(str(trade.get("fee") or "0"))
            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=order.trade_type,
                percent_token=order.quote_asset,
                flat_fees=[TokenAmount(amount=fee_amount, token=order.quote_asset)],
            )
            amount = Decimal(str(trade["amount"]))
            price = Decimal(str(trade["price"]))
            fill_ts = datetime.fromisoformat(str(trade["timestamp"]).replace("Z", "+00:00")).timestamp()
            trade_updates.append(
                TradeUpdate(
                    trade_id=str(trade["id"]),
                    client_order_id=order.client_order_id,
                    exchange_order_id=str(order.exchange_order_id),
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=amount,
                    fill_quote_amount=Decimal(str(trade.get("total") or amount * price)),
                    fill_price=price,
                    fill_timestamp=fill_ts,
                )
            )
        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        api_params: Dict[str, Any] = {}
        if tracked_order.exchange_order_id and tracked_order.exchange_order_id != "UNKNOWN":
            api_params["id"] = int(tracked_order.exchange_order_id)
        else:
            api_params["clientOrderId"] = tracked_order.client_order_id

        updated = await self._api_post(
            path_url=CONSTANTS.ORDER_STATUS_PATH_URL,
            data=api_params,
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_STATUS_PATH_URL,
        )
        if not isinstance(updated, dict) or updated.get("status") != "ok":
            raise IOError(f"Nobitex order status failed: {updated}")

        order = updated["order"]
        new_state = self._order_state_from_nobitex(order)
        created = order.get("created_at")
        ts = (
            datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
            if created else self.current_timestamp
        )
        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(order["id"]),
            trading_pair=tracked_order.trading_pair,
            update_timestamp=ts,
            new_state=new_state,
        )

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        account_info = await self._api_get(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.ACCOUNTS_PATH_URL,
        )
        wallets = account_info.get("wallets") or {}
        # /v2/wallets → { "RLS": {"balance", "blocked"}, ... }
        if isinstance(wallets, dict):
            items = wallets.items()
        else:
            items = []

        for asset_key, entry in items:
            key = str(asset_key).upper()
            asset_name = "IRT" if key == "RLS" else key
            balance = Decimal(str(entry.get("balance") or "0"))
            blocked = Decimal(str(entry.get("blocked") or "0"))
            self._account_available_balances[asset_name] = balance
            self._account_balances[asset_name] = balance + blocked
            remote_asset_names.add(asset_name)

        for asset_name in local_asset_names.difference(remote_asset_names):
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        nobitex = exchange_info.get("nobitex") or {}
        amount_precisions = nobitex.get("amountPrecisions") or {}
        for symbol in amount_precisions:
            if not nobitex_utils.is_exchange_information_valid(symbol):
                continue
            try:
                base, quote = nobitex_utils.split_exchange_symbol(symbol)
                hb_pair = combine_to_hb_trading_pair(base=base, quote=quote)
                if hb_pair in mapping.inverse:
                    continue
                mapping[symbol] = hb_pair
            except Exception:
                continue
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        resp = await self._api_get(
            path_url=CONSTANTS.MARKET_STATS_PATH_URL,
            limit_id=CONSTANTS.MARKET_STATS_PATH_URL,
        )
        key = nobitex_utils.stats_market_key(trading_pair)
        stats = (resp.get("stats") or {}).get(key) or {}
        return float(stats["latest"])
