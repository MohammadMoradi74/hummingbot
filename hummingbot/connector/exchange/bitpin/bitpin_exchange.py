import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import ContentTypeError
from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.bitpin import (
    bitpin_constants as CONSTANTS,
    bitpin_utils,
    bitpin_web_utils as web_utils,
)
from hummingbot.connector.exchange.bitpin.bitpin_api_order_book_data_source import BitpinAPIOrderBookDataSource
from hummingbot.connector.exchange.bitpin.bitpin_api_user_stream_data_source import BitpinAPIUserStreamDataSource
from hummingbot.connector.exchange.bitpin.bitpin_auth import BitpinAuth
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
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class BitpinExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(self,
                 bitpin_api_key: str,
                 bitpin_api_secret: str,
                 balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
                 rate_limits_share_pct: Decimal = Decimal("100"),
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 ):
        self.api_key = bitpin_api_key
        self.secret_key = bitpin_api_secret
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._last_trades_poll_bitpin_timestamp = 1.0
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def bitpin_order_type(order_type: OrderType) -> str:
        return order_type.name.lower()

    @staticmethod
    def to_hb_order_type(bitpin_type: str) -> OrderType:
        return OrderType[bitpin_type]

    @property
    def authenticator(self):
        return BitpinAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer,
            domain=self._domain)

    @property
    def name(self) -> str:
        if self._domain == "ir":
            return "bitpin"
        else:
            return f"bitpin_{self._domain}"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self ._domain

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

    async def get_all_pairs_prices(self) -> List[Dict[str, str]]:
        pairs_prices = await self._api_get(path_url=CONSTANTS.TICKER_BOOK_PATH_URL)
        return pairs_prices

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception)
        is_time_synchronizer_related = ("-1021" in error_description
                                        and "Timestamp for this request" in error_description)
        return is_time_synchronizer_related

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return str(CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE) in str(
            status_update_exception
        ) and CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        msg = str(cancelation_exception)
        if str(CONSTANTS.UNKNOWN_ORDER_ERROR_CODE) in msg and CONSTANTS.UNKNOWN_ORDER_MESSAGE in msg:
            return True
        if "status is 404" in msg and "Not found" in msg:
            return True
        return False

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return BitpinAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return BitpinAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    async def _api_request(
            self,
            path_url,
            overwrite_url: Optional[str] = None,
            method: RESTMethod = RESTMethod.GET,
            params: Optional[Dict[str, Any]] = None,
            data: Optional[Dict[str, Any]] = None,
            is_auth_required: bool = False,
            return_err: bool = False,
            limit_id: Optional[str] = None,
            headers: Optional[Dict[str, Any]] = None,
            **kwargs,
    ) -> Dict[str, Any]:
        if is_auth_required:
            rest_assistant = await self._web_assistants_factory.get_rest_assistant()
            await self._auth.ensure_authenticated(rest_assistant)
        try:
            return await super()._api_request(path_url=path_url, overwrite_url=overwrite_url, method=method,
                                              params=params, data=data, is_auth_required=is_auth_required,
                                              return_err=return_err, limit_id=limit_id, headers=headers, **kwargs, )
        except OSError as e:
            if is_auth_required and ("401" in str(e) or "token_not_valid" in str(e)):
                self.logger().info("Bitpin access token expired; refreshed and retrying request")
                await self._auth.refresh_authenticate(rest_assistant)
                return await super()._api_request(path_url=path_url, overwrite_url=overwrite_url, method=method,
                                                  params=params, data=data, is_auth_required=is_auth_required,
                                                  return_err=return_err, limit_id=limit_id, headers=headers, **kwargs, )
            raise

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

    def _parse_order_timestamp(self, order_data: Dict[str, Any]) -> float:
        return self._find_update_time_order_data(order_data)

    def _is_successful_cancel_response(self, cancel_result: Any) -> bool:
        # Bitpin returns 204 No Content → REST layer may give None or empty body
        return cancel_result is None or cancel_result == ""

    async def _cancel_order_on_exchange(self, path_url: str) -> bool:
        cancel_result = await self._api_delete(
            path_url=path_url,
            limit_id=CONSTANTS.ORDER_PATH_URL,
            is_auth_required=True,
        )
        return self._is_successful_cancel_response(cancel_result)

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
        amount_str = f"{amount:.6f}"
        type_str = BitpinExchange.bitpin_order_type(order_type)
        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        api_params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side_str,
            "base_amount": amount_str,
            "type": type_str,
            "identifier": order_id,
        }
        if order_type in (OrderType.LIMIT, OrderType.LIMIT_MAKER):
            api_params["price"] = f"{price:f}"

        try:
            order_result = await self._api_post(
                path_url=CONSTANTS.ORDER_PATH_URL,
                data=api_params,
                is_auth_required=True,
                limit_id=CONSTANTS.ORDER_PATH_URL,
            )
            if not order_result or "id" not in order_result:
                raise IOError(f"Bitpin place order returned unexpected payload: {order_result}")

            o_id = str(order_result["id"])
            transact_time = self._parse_order_timestamp(order_result)
            return o_id, transact_time

        except IOError as e:
            error_description = str(e)
            is_server_overloaded = ("status is 503" in error_description and
                                    "Unknown error, please check your request or try again later." in error_description)
            if is_server_overloaded:
                # Same pattern as binance/mexc: allow status polling to recover the real id
                return "UNKNOWN", self._time_synchronizer.time()
            raise

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        """
        Cancel via exchange order id when available; otherwise via client identifier.
        Bitpin docs:
          DELETE /odr/orders/{order_id}/
          DELETE /odr/orders/identifier/{order_identifier}/
        """
        exchange_order_id = tracked_order.exchange_order_id

        cancel_paths: List[str] = []

        if exchange_order_id and exchange_order_id != "UNKNOWN":
            cancel_paths.append(f"{CONSTANTS.ORDER_PATH_URL}{exchange_order_id}/")

        # Always allow identifier cancel (fixes None exchange_order_id + 503 UNKNOWN cases)
        cancel_paths.append(f"{CONSTANTS.ORDER_CANCEL_BY_IDENTIFIER_PATH_URL}{order_id}/")

        last_error: Optional[Exception] = None

        for path_url in cancel_paths:
            try:
                if await self._cancel_order_on_exchange(path_url):
                    return True
            except ContentTypeError as e:
                if e.status == 204:
                    return True
                last_error = e
            except OSError as e:
                if self._is_order_not_found_during_cancelation_error(e):
                    # Try next path (e.g. id cancel 406 → identifier cancel)
                    last_error = e
                    continue
                raise

        if last_error is not None and self._is_order_not_found_during_cancelation_error(last_error):
            # Let ExchangePyBase call process_order_not_found()
            raise last_error

        return False

    async def _resolve_order_after_cancel_not_found(self, order: InFlightOrder) -> bool:
        """Cancel failed because order is gone from the open book (filled/canceled).
        Resolve via GET order + fills instead of treating as lost."""
        try:
            await self._update_orders_fills(orders=[order])
            order_update = await self._request_order_status(tracked_order=order)
            self._order_tracker.process_order_update(order_update)
            return order_update.new_state in (OrderState.FILLED, OrderState.CANCELED)
        except Exception:
            self.logger().debug(
                f"Could not resolve order {order.client_order_id} after cancel not-found.",
                exc_info=True,
            )
            return False

    async def _execute_order_cancel(self, order: InFlightOrder) -> Optional[str]:
        try:
            cancelled = await self._execute_order_cancel_and_process_update(order=order)
            if cancelled:
                return order.client_order_id
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.logger().warning(
                f"Failed to cancel the order {order.client_order_id} because it does not have an exchange order id yet"
            )
            await self._order_tracker.process_order_not_found(order.client_order_id)
        except Exception as ex:
            if self._is_order_not_found_during_cancelation_error(cancelation_exception=ex):
                if await self._resolve_order_after_cancel_not_found(order):
                    return order.client_order_id
                self.logger().warning(f"Failed to cancel order {order.client_order_id} (order not found)")
                await self._order_tracker.process_order_not_found(order.client_order_id)
            else:
                self.logger().error(f"Failed to cancel order {order.client_order_id}", exc_info=True)
        return None

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
        retval = []
        for rule in filter(bitpin_utils.is_exchange_information_valid, exchange_info_dict):
            try:
                # trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule.get("symbol"))
                # filters = rule.get("filters")
                # price_filter = [f for f in filters if f.get("filterType") == "PRICE_FILTER"][0]
                # lot_size_filter = [f for f in filters if f.get("filterType") == "LOT_SIZE"][0]
                # min_notional_filter = [f for f in filters if f.get("filterType") in ["MIN_NOTIONAL", "NOTIONAL"]][0]

                # TODO: fix this, use internal functions
                trading_pair = rule.get("symbol").replace('_', '-')
                min_order_size = 10 ** (-int(rule.get("base_amount_precision")))
                tick_size = 10 ** (-int(rule.get("price_precision")))
                step_size = 10 ** (-int(rule.get("base_amount_precision")))
                min_notional = 1e5 if rule.get("quote") == 'IRT' else 2

                retval.append(
                    TradingRule(trading_pair,
                                min_order_size=Decimal(min_order_size),
                                min_price_increment=Decimal(tick_size),
                                min_base_amount_increment=Decimal(step_size),
                                min_notional_size=Decimal(min_notional)))

            except Exception:
                self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")
        return retval

    async def _status_polling_loop_fetch_updates(self):
        await self._update_order_fills_from_trades()
        await super()._status_polling_loop_fetch_updates()

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _user_stream_event_listener(self):
        """
        This functions runs in background continuously processing the events received from the exchange by the user
        stream data source. It keeps reading events from the queue until the task is interrupted.
        The events received are balance updates, order updates and trade events.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                event_type = event_message.get("event")
                if event_type == "user_match_update":
                    # Handle trade update
                    client_order_id = event_message.get("identifier")
                    tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)

                    if tracked_order is not None:
                        fee = TradeFeeBase.new_spot_fee(
                            fee_schema=self.trade_fee_schema(),
                            trade_type=tracked_order.trade_type,
                            percent_token=event_message["commission_currency"],
                            flat_fees=[TokenAmount(
                                amount=Decimal(event_message["commission"]),
                                token=event_message["commission_currency"]
                            )]
                        )
                        trade_update = TradeUpdate(
                            trade_id=str(event_message["id"]),
                            client_order_id=client_order_id,
                            exchange_order_id=str(event_message["order_id"]),
                            trading_pair=tracked_order.trading_pair,
                            fee=fee,
                            fill_base_amount=Decimal(event_message["base_amount"]),
                            fill_quote_amount=Decimal(event_message["quote_amount"]),
                            fill_price=Decimal(event_message["price"]),
                            fill_timestamp=datetime.fromisoformat(
                                event_message["event_time"].replace('Z', '+00:00')).timestamp()
                        )
                        self._order_tracker.process_trade_update(trade_update)

                # TODO: NEED IMPLEMENTATION
                elif event_type == "user_order_update":
                    # Handle order status update
                    client_order_id = event_message.get("identifier")
                    tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)

                    if tracked_order is not None:
                        new_state = self._find_state_from_order_data(event_message)

                        order_update = OrderUpdate(
                            trading_pair=tracked_order.trading_pair,
                            update_timestamp=datetime.fromisoformat(
                                event_message["event_time"].replace('Z', '+00:00')).timestamp(),
                            new_state=CONSTANTS.ORDER_STATE[new_state],
                            client_order_id=client_order_id,
                            exchange_order_id=str(event_message["id"]),
                        )
                        self._order_tracker.process_order_update(order_update=order_update)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _update_order_fills_from_trades(self):
        """
        This is intended to be a backup measure to get filled events with trade ID for orders,
        in case Bitpin's user stream events are not working.
        NOTE: It is not required to copy this functionality in other connectors.
        This is separated from _update_order_status which only updates the order status without producing filled
        events, since Bitpin's get order endpoint does not return trade IDs.
        The minimum poll interval for order status is 10 seconds.
        """
        small_interval_last_tick = self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        small_interval_current_tick = self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        long_interval_last_tick = self._last_poll_timestamp / self.LONG_POLL_INTERVAL
        long_interval_current_tick = self.current_timestamp / self.LONG_POLL_INTERVAL

        if (long_interval_current_tick > long_interval_last_tick
                or (self.in_flight_orders and small_interval_current_tick > small_interval_last_tick)):
            # There is no time params for bitpin. Maby using limit can solve the problem.
            # query_time = int(self._last_trades_poll_bitpin_timestamp * 1e3)
            self._last_trades_poll_bitpin_timestamp = self._time_synchronizer.time()
            order_by_exchange_id_map = {}
            for order in self._order_tracker.all_fillable_orders.values():
                order_by_exchange_id_map[order.exchange_order_id] = order

            tasks = []
            trading_pairs = self.trading_pairs
            for trading_pair in trading_pairs:
                params = {
                    "symbol": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                }
                # There is no param for time
                # if self._last_poll_timestamp > 0:
                #     params["startTime"] = query_time
                tasks.append(self._api_get(
                    path_url=CONSTANTS.MY_TRADES_PATH_URL,
                    params=params,
                    limit_id='/odr/fills/',
                    is_auth_required=True))

            self.logger().debug(f"Polling for order fills of {len(tasks)} trading pairs.")
            results = await safe_gather(*tasks, return_exceptions=True)

            for trades, trading_pair in zip(results, trading_pairs):

                if isinstance(trades, Exception):
                    self.logger().network(
                        f"Error fetching trades update for the order {trading_pair}: {trades}.",
                        app_warning_msg=f"Failed to fetch trade update for {trading_pair}."
                    )
                    continue
                for trade in trades:
                    exchange_order_id = str(trade["order_id"])
                    if exchange_order_id in order_by_exchange_id_map:
                        # This is a fill for a tracked order
                        tracked_order = order_by_exchange_id_map[exchange_order_id]
                        fee = TradeFeeBase.new_spot_fee(
                            fee_schema=self.trade_fee_schema(),
                            trade_type=tracked_order.trade_type,
                            percent_token=trade["commission_currency"],
                            flat_fees=[TokenAmount(amount=Decimal(trade["commission"]),
                                                   token=trade["commission_currency"])]
                        )
                        trade_update = TradeUpdate(
                            trade_id=str(trade["id"]),
                            client_order_id=tracked_order.client_order_id,
                            exchange_order_id=exchange_order_id,
                            trading_pair=trading_pair,
                            fee=fee,
                            fill_base_amount=Decimal(trade["base_amount"]),
                            fill_quote_amount=Decimal(trade["quote_amount"]),
                            fill_price=Decimal(trade["price"]),
                            fill_timestamp=datetime.fromisoformat(trade["created_at"]).timestamp(),
                        )
                        self._order_tracker.process_trade_update(trade_update)
                    elif self.is_confirmed_new_order_filled_event(str(trade["id"]), exchange_order_id, trading_pair):
                        # This is a fill of an order registered in the DB but not tracked any more
                        self._current_trade_fills.add(TradeFillOrderDetails(
                            market=self.display_name,
                            exchange_trade_id=str(trade["id"]),
                            symbol=trading_pair))
                        self.trigger_event(
                            MarketEvent.OrderFilled,
                            OrderFilledEvent(
                                timestamp=datetime.fromisoformat(trade["created_at"]).timestamp(),
                                order_id=self._exchange_order_ids.get(str(trade["order_id"]), None),
                                trading_pair=trading_pair,
                                trade_type=TradeType.BUY if trade["side"] == 'buy' else TradeType.SELL,
                                # Bitpin doesn't return order type in this . set all to market
                                order_type=OrderType.MARKET,
                                price=Decimal(trade["price"]),
                                amount=Decimal(trade["base_amount"]),
                                trade_fee=DeductedFromReturnsTradeFee(
                                    flat_fees=[
                                        TokenAmount(
                                            trade["commission_currency"],
                                            Decimal(trade["commission"])
                                        )
                                    ]
                                ),
                                exchange_trade_id=str(trade["id"])
                            ))
                        self.logger().info(f"Recreating missing trade in TradeFill: {trade}")

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
            all_fills_response = await self._api_get(
                path_url=CONSTANTS.MY_TRADES_PATH_URL,
                params={
                    "symbol": exchange_symbol,
                    "side": order.trade_type.name.lower()
                },
                is_auth_required=True,
                limit_id=CONSTANTS.MY_TRADES_PATH_URL)

            # Bitpin returns order_id as int; InFlightOrder stores str
            target_order_id = str(order.exchange_order_id)
            matched_fills = [fill for fill in all_fills_response if str(fill["order_id"]) == target_order_id]

            for trade in matched_fills:
                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=order.trade_type,
                    percent_token=trade["commission_currency"],
                    flat_fees=[TokenAmount(amount=Decimal(trade["commission"]), token=trade["commission_currency"])]
                )
                trade_update = TradeUpdate(
                    trade_id=str(trade["id"]),
                    client_order_id=order.client_order_id,
                    exchange_order_id=str(trade["order_id"]),
                    trading_pair=order.trading_pair,  # HB pair, not BTC_IRT
                    fee=fee,
                    fill_base_amount=Decimal(trade["base_amount"]),
                    fill_quote_amount=Decimal(trade["quote_amount"]),
                    fill_price=Decimal(trade["price"]),
                    fill_timestamp=datetime.fromisoformat(trade["created_at"]).timestamp(),
                )
                trade_updates.append(trade_update)

        return trade_updates

    # TODO: write unit tests for _find_state_from_order_data covering all state branches
    #   - "initial" → PENDING_CREATE
    #   - "active" with/without req_to_cancel, and 0, partial, full fills
    #   - "closed"/closed_at → FILLED vs CANCELED
    #   - any unexpected → FAILED
    def _find_state_from_order_data(self, order_data: dict) -> str:
        """
        Determine the internal order‐state key from raw exchange order data.

        Args:
            order_data (dict):  The JSON‐like payload from the exchange, expected to contain:
                - "state" (str):        e.g. "initial", "active", or "closed"
                - "req_to_cancel" (bool):  whether the user has requested a cancel
                - "dealed_base_amount" (str|Decimal): how much of the base asset has filled
                - "base_amount" (str|Decimal):        the total base amount of the order
                - "closed_at" (str|None):             timestamp if the order is closed

        Returns:
            str:  One of the keys in ORDER_STATE, namely:
                  "PENDING_CREATE", "OPEN", "PENDING_CANCEL",
                  "PARTIALLY_FILLED", "FILLED", "CANCELED", or "FAILED".

        Logic:
        1. If the exchange state is "initial", we haven’t submitted yet → PENDING_CREATE
        2. If "active" and cancel requested → PENDING_CANCEL
           ├─ else if no fills yet → OPEN
           ├─ else if partially filled → PARTIALLY_FILLED
           └─ else (filled == total) → FILLED
        3. If "closed" or a non-null closed_at timestamp
           ├─ fully filled → FILLED
           └─ otherwise → CANCELED
        4. Any other combination → FAILED
        """
        state = order_data.get("state")
        req_to_cancel = order_data.get("req_to_cancel", False)
        filled = Decimal(order_data.get("dealed_base_amount", "0"))
        total = Decimal(order_data.get("base_amount", "0"))
        closed = order_data.get("closed_at") is not None

        if state == "initial":
            return "PENDING_CREATE"

        if state == "active":
            if req_to_cancel:
                return "PENDING_CANCEL"
            if filled == 0:
                return "OPEN"
            if filled < total:
                return "PARTIALLY_FILLED"
            return "FILLED"

        if state == "closed" or closed:
            if filled == total and total > 0:
                return "FILLED"
            return "CANCELED"

        return "FAILED"

    def _find_update_time_order_data(self, order_data: dict) -> float:
        """
        Extract the POSIX timestamp (in seconds) for this order update.

        Chooses 'closed_at' if present and non-null; otherwise falls back to 'created_at'.

        Args:
            order_data (dict):
                A single-order payload from the exchange API. Expected keys:
                  - 'created_at' (str): ISO8601 timestamp when the order was created.
                  - 'closed_at'  (str|None): ISO8601 timestamp when the order was closed, or None/"null".

        Returns:
            float:  The UNIX timestamp (seconds since epoch, including fractional part)
                    corresponding to closed_at (if set) or created_at.
        """
        ts_str = order_data.get('closed_at')
        if not ts_str or ts_str == "null":
            ts_str = order_data.get('created_at')

        return datetime.fromisoformat(ts_str).timestamp()

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        if tracked_order.exchange_order_id and tracked_order.exchange_order_id != "UNKNOWN":
            path_url = f"{CONSTANTS.ORDER_PATH_URL}{tracked_order.exchange_order_id}/"
        else:
            path_url = f"{CONSTANTS.ORDER_CANCEL_BY_IDENTIFIER_PATH_URL}{tracked_order.client_order_id}/"

        updated_order_data = await self._api_get(
            path_url=path_url,
            limit_id=CONSTANTS.ORDER_PATH_URL,
            is_auth_required=True)

        new_state = CONSTANTS.ORDER_STATE[self._find_state_from_order_data(updated_order_data)]

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(updated_order_data["id"]),
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self._find_update_time_order_data(updated_order_data),
            new_state=new_state,
        )

        return order_update

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        balances = await self._api_get(
            path_url=CONSTANTS.ACCOUNTS_PATH_URL,
            is_auth_required=True)

        for balance_entry in balances:
            asset_name = balance_entry["asset"]
            free_balance = Decimal(balance_entry["balance"])
            total_balance = Decimal(balance_entry["balance"]) + Decimal(balance_entry["frozen"])
            self._account_available_balances[asset_name] = free_balance
            self._account_balances[asset_name] = total_balance
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(bitpin_utils.is_exchange_information_valid, exchange_info):
            mapping[symbol_data["symbol"]] = combine_to_hb_trading_pair(base=symbol_data["base"],
                                                                        quote=symbol_data["quote"])
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        resp_json = await self._api_request(
            method=RESTMethod.GET,
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_PATH_URL,
        )

        return float({item['symbol']: item for item in resp_json}[symbol]['price'])
