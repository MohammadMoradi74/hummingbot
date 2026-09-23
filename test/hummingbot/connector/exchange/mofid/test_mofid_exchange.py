import json
import re
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

from aioresponses import aioresponses
from aioresponses.core import RequestCall

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_exchange import MofidExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase


class MofidExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "IRR"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.trading_pair_2 = f"{cls.base_asset}-USDT"

    @property
    def all_symbols_url(self):
        return web_utils.private_rest_url(CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)

    @property
    def latest_prices_url(self):
        isin = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        return web_utils.private_rest_url(f"{CONSTANTS.SNAPSHOT_PATH_URL}/{isin}", domain=self.exchange._domain)

    @property
    def network_status_url(self):
        # Path includes client ms; tests match via regex override.
        return web_utils.private_rest_url(CONSTANTS.SERVER_TIME_PATH_URL, domain=self.exchange._domain)

    @property
    def trading_rules_url(self):
        return web_utils.private_rest_url(CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)

    @property
    def order_creation_url(self):
        return web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL, domain=self.exchange._domain)

    @property
    def balance_url(self):
        return web_utils.private_rest_url(CONSTANTS.MONEY_PATH_URL, domain=self.exchange._domain)

    def _symbol_entry(
            self,
            isin: str,
            active: bool = True,
            can_buy: bool = True,
            can_sell: bool = True,
            min_dealable_price: int = 1,
            min_dealable_count: int = 1,
            min_vol: int = 1,
            max_vol: int = 100000,
            buy_commission: str = "0.0012",
            sell_commission: str = "0.0012",
            name: str = "Test",
    ) -> Dict[str, Any]:
        return {
            "symbolIsin": isin,
            "symbolName": name,
            "isActive": active,
            "canBuy": can_buy,
            "canSell": can_sell,
            "buyCommission": buy_commission,
            "sellCommission": sell_commission,
            "minValidBuyVolume": min_vol,
            "maxValidBuyVolume": max_vol,
            "minValidSellVolume": min_vol,
            "maxValidSellVolume": max_vol,
            "minDealablePrice": min_dealable_price,
            "minDealableCount": min_dealable_count,
        }

    @property
    def all_symbols_request_mock_response(self):
        return {
            "symbols": [
                self._symbol_entry(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)),
            ],
            "hash": "testhash",
        }

    @property
    def latest_prices_request_mock_response(self):
        mid = self.expected_latest_price
        return {
            "isin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "buySheets": [{"price": mid - 1, "volume": 100}],
            "sellSheets": [{"price": mid + 1, "volume": 100}],
        }

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        response = {
            "symbols": [
                self._symbol_entry(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)),
                self._symbol_entry("INVALID", active=False, can_buy=False, can_sell=False),
            ],
            "hash": "testhash",
        }
        return "INVALID-IRR", response

    @property
    def network_status_request_successful_mock_response(self):
        return {"diff": 0, "serverTimestamp": int(time.time() * 1e3)}

    @property
    def trading_rules_request_mock_response(self):
        return {
            "symbols": [
                self._symbol_entry(
                    self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    min_dealable_price=1,
                    min_dealable_count=1,
                    min_vol=1,
                ),
            ],
            "hash": "testhash",
        }

    @property
    def trading_rules_request_erroneous_mock_response(self):
        return {
            "symbols": [
                {
                    "symbolIsin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    "isActive": True,
                    "canBuy": True,
                    "canSell": True,
                    # missing minDealable* → KeyError/TypeError path via Decimal(None) avoided;
                    # force parse error with bad types
                    "minValidBuyVolume": "bad",
                    "minValidSellVolume": "bad",
                    "minDealablePrice": None,
                    "minDealableCount": None,
                }
            ],
            "hash": "testhash",
        }

    @property
    def order_creation_request_successful_mock_response(self):
        return {
            "isSuccessful": True,
            "id": str(self.expected_exchange_order_id),
            "message": "",
            "omsError": None,
        }

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        # Used only as money payload; portfolio mocked separately in _configure_balance_response.
        return {
            "t0": 2000,
            "t1": 2000,
            "t2": 2000,
            "buyPowerT0": 2000,
            "buyPowerT1": 2000,
            "buyPowerT2": 2000,
            "block": 0,
        }

    @property
    def balance_request_mock_response_only_base(self):
        return {
            "t0": 0,
            "t1": 0,
            "t2": 0,
            "buyPowerT0": 0,
            "buyPowerT1": 0,
            "buyPowerT2": 0,
            "block": 0,
        }

    @property
    def balance_event_websocket_update(self):
        return {"e": CONSTANTS.USER_MONEY_EVENT_TYPE, "meta": {}}

    @property
    def expected_latest_price(self):
        return 9999.9

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT]

    @property
    def expected_trading_rule(self):
        rule = self.trading_rules_request_mock_response["symbols"][0]
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal(str(rule["minValidBuyVolume"])),
            min_price_increment=Decimal(str(rule["minDealablePrice"])),
            min_base_amount_increment=Decimal(str(rule["minDealableCount"])),
            min_notional_size=Decimal("1"),
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        erroneous_rule = self.trading_rules_request_erroneous_mock_response["symbols"][0]
        return f"Error parsing the trading pair rule {erroneous_rule}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return "1121DMbrkZTestOid1"

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return True

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return False

    @property
    def expected_partial_fill_price(self) -> Decimal:
        return Decimal("10500")

    @property
    def expected_partial_fill_amount(self) -> Decimal:
        return Decimal("1")

    @aioresponses()
    async def test_update_order_status_when_order_has_not_changed_and_one_partial_fill(self, mock_api):
        # Abstract test uses amount=1; Mofid fills are integers — use amount=100 so partial=1 stays open.
        # Fills come from open-order embedded trades (not orderHistory) when present.
        import asyncio

        from hummingbot.core.data_type.in_flight_order import OrderState
        from hummingbot.core.event.events import OrderFilledEvent

        self.exchange._set_current_timestamp(1640780000)
        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=str(self.expected_exchange_order_id),
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("100"),
        )
        order: InFlightOrder = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        order_url = self.configure_partially_filled_order_status_response(order=order, mock_api=mock_api)

        self.assertTrue(order.is_open)
        await self.exchange._update_order_status()
        await asyncio.sleep(0.1)

        if order_url:
            order_status_request = self._all_executed_requests(mock_api, order_url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        self.assertTrue(order.is_open)
        self.assertEqual(OrderState.PARTIALLY_FILLED, order.current_state)

        if self.is_order_fill_http_update_included_in_status_update:
            self.assertEqual(self.expected_partial_fill_amount, order.executed_amount_base)
            self.assertEqual(self.expected_partial_fill_price, order.average_executed_price)
            fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
            self.assertEqual(self.expected_partial_fill_amount, fill_event.amount)
            self.assertEqual(self.expected_partial_fill_price, fill_event.price)

    @aioresponses()
    async def test_update_order_status_when_filled(self, mock_api):
        # Fills from open-order embedded trades; history URL may not be hit.
        import asyncio

        from hummingbot.core.event.events import BuyOrderCompletedEvent, OrderFilledEvent

        self.exchange._set_current_timestamp(1640780000)
        request_sent_event = asyncio.Event()

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=str(self.expected_exchange_order_id),
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order: InFlightOrder = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        urls = self.configure_completely_filled_order_status_response(
            order=order,
            mock_api=mock_api,
            callback=lambda *args, **kwargs: request_sent_event.set(),
        )

        await self.exchange._update_order_status()
        await request_sent_event.wait()
        await asyncio.sleep(0.1)

        for url in (urls if isinstance(urls, list) else [urls]):
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        await order.wait_until_completely_filled()
        self.assertTrue(order.is_done)
        self.assertTrue(order.is_filled)

        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(order.client_order_id, fill_event.order_id)
        self.assertEqual(order.amount, fill_event.amount)
        self.assertEqual(order.price, fill_event.price)
        self.assertEqual(self.expected_fill_fee, fill_event.trade_fee)

        buy_event: BuyOrderCompletedEvent = self.buy_order_completed_logger.event_log[0]
        self.assertEqual(order.client_order_id, buy_event.order_id)
        self.assertEqual(order.amount, buy_event.base_asset_amount)
        self.assertEqual(order.amount * order.price, buy_event.quote_asset_amount)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)

    @aioresponses()
    async def test_lost_order_included_in_order_fills_update_and_not_in_order_status_update(self, mock_api):
        # Lost-order fills use the same embed-first path when the order is still on /core/api/order.
        import asyncio

        from hummingbot.core.event.events import OrderFilledEvent

        self.exchange._set_current_timestamp(1640780000)
        request_sent_event = asyncio.Event()

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=str(self.expected_exchange_order_id),
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order: InFlightOrder = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        for _ in range(self.exchange._order_tracker._lost_order_count_limit + 1):
            await self.exchange._order_tracker.process_order_not_found(client_order_id=order.client_order_id)

        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)

        self.configure_completely_filled_order_status_response(
            order=order,
            mock_api=mock_api,
            callback=lambda *args, **kwargs: request_sent_event.set(),
        )

        await self.exchange._update_order_status()
        await request_sent_event.wait()
        await order.wait_until_completely_filled()
        await asyncio.sleep(0.1)

        self.assertTrue(order.is_done)
        self.assertTrue(order.is_failure)

        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(order.client_order_id, fill_event.order_id)
        self.assertEqual(order.amount, fill_event.amount)
        self.assertEqual(self.expected_fill_fee, fill_event.trade_fee)

        self.assertEqual(0, len(self.buy_order_completed_logger.event_log))
        self.assertIn(order.client_order_id, self.exchange._order_tracker.all_fillable_orders)

        request_sent_event.clear()
        # Second pass: history fallback (order may already be filled from embed; still configure).
        self.configure_full_fill_trade_response(
            order=order,
            mock_api=mock_api,
            callback=lambda *args, **kwargs: request_sent_event.set(),
        )
        # Empty open book so lost-order fill update hits history.
        open_url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        mock_api.get(
            re.compile(f"^{open_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"orders": []}),
            repeat=True,
            callback=lambda *args, **kwargs: request_sent_event.set(),
        )

        await self.exchange._update_lost_orders_status()
        await request_sent_event.wait()
        await asyncio.sleep(0.1)

        self.assertTrue(order.is_done)
        self.assertTrue(order.is_failure)
        self.assertEqual(1, len(self.order_filled_logger.event_log))
        self.assertEqual(0, len(self.buy_order_completed_logger.event_log))
        self.assertNotIn(order.client_order_id, self.exchange._order_tracker.all_fillable_orders)

    @aioresponses()
    async def test_update_order_status_when_filled_correctly_processed_even_when_trade_fill_update_fails(
            self, mock_api
    ):
        # Empty embed + failing history → status can still mark FILLED without OrderFilledEvent.
        import asyncio

        self.exchange._set_current_timestamp(1640780000)
        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=str(self.expected_exchange_order_id),
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order: InFlightOrder = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        trade_url = self.configure_erroneous_http_fill_trade_response(order=order, mock_api=mock_api)
        urls = self.configure_completely_filled_order_status_response(
            order=order,
            mock_api=mock_api,
            include_embedded_trades=False,
        )

        order.completely_filled_event.set()
        await self.exchange._update_order_status()
        await order.wait_until_completely_filled()
        await asyncio.sleep(0.1)

        for url in (urls if isinstance(urls, list) else [urls]):
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        self.assertTrue(order.is_filled)
        self.assertTrue(order.is_done)

        trades_request = self._all_executed_requests(mock_api, trade_url)[0]
        self.validate_auth_credentials_present(trades_request)
        self.validate_trades_request(order=order, request_call=trades_request)

        self.assertEqual(0, len(self.order_filled_logger.event_log))
        buy_event = self.buy_order_completed_logger.event_log[0]
        self.assertEqual(order.client_order_id, buy_event.order_id)
        self.assertEqual(Decimal(0), buy_event.base_asset_amount)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)

    @property
    def expected_fill_fee(self) -> TradeFeeBase:
        return DeductedFromReturnsTradeFee(percent=Decimal("0.0012"))

    @property
    def expected_fill_trade_id(self) -> str:
        return "30000"

    def test_map_rest_order_states_onsending_and_filled_on_open_list(self):
        from hummingbot.core.data_type.in_flight_order import OrderState

        map_state = self.exchange._map_rest_order_to_state
        self.assertEqual(
            OrderState.PENDING_CREATE,
            map_state({"orderState": 7, "orderStateStr": "OnSending", "quantity": 100, "executedQuantity": 0}),
        )
        self.assertEqual(
            OrderState.PENDING_CREATE,
            map_state({"orderStateStr": "OnSending", "quantity": 100, "executedQuantity": 0}),
        )
        self.assertEqual(
            OrderState.OPEN,
            map_state({"orderState": 6, "orderStateStr": "OnBoard", "quantity": 100, "executedQuantity": 0}),
        )
        # Live: OrderExecuted still on GET /core/api/order briefly
        self.assertEqual(
            OrderState.FILLED,
            map_state({"orderState": 20, "orderStateStr": "OrderExecuted", "quantity": 100, "executedQuantity": 100}),
        )
        self.assertEqual(
            OrderState.FILLED,
            map_state({"orderState": 6, "quantity": 100, "executedQuantity": 100}),
        )
        self.assertEqual(OrderState.FILLED, CONSTANTS.WS_ORDER_STATE["OrderExecuted"])
        self.assertEqual(OrderState.PENDING_CREATE, CONSTANTS.WS_ORDER_STATE["OnSending"])
        self.assertEqual(OrderState.PENDING_CREATE, CONSTANTS.ORDER_STATE[7])
        self.assertEqual(OrderState.FILLED, CONSTANTS.ORDER_STATE[20])

    def test_format_oms_rejection_surfaces_code_and_ignores_orphan_id(self):
        # Live tmp3 failed place: isSuccessful=False with orphan id + omsError 7003.
        result = {
            "isSuccessful": False,
            "id": "1121DMbrkZJixnoY",
            "message": "7003: قیمت خارج از محدوده مجاز می‌باشد",
            "omsError": [{
                "name": "PriceIsNotInRangeError",
                "error": "قیمت خارج از محدوده مجاز می‌باشد",
                "code": 7003,
            }],
        }
        msg = self.exchange._format_oms_rejection("place order", result)
        self.assertIn("7003", msg)
        self.assertIn("PriceIsNotInRangeError", msg)
        self.assertNotIn("1121DMbrkZJixnoY", msg)

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        return base_token

    def create_exchange_instance(self):
        client_config_map = ClientConfigAdapter(ClientConfigMap())
        return MofidExchange(
            client_config_map=client_config_map,
            mofid_api_key="testAPIKey",
            mofid_api_secret="testSecret",
            trading_pairs=[self.trading_pair],
        )

    def validate_auth_credentials_present(self, request_call: RequestCall):
        headers = request_call.kwargs.get("headers") or {}
        self.assertEqual(headers.get("Authorization"), "Bearer testAPIKey")

    def _request_json(self, request_call: RequestCall) -> Dict[str, Any]:
        data = request_call.kwargs.get("data")
        if data is None:
            return {}
        if isinstance(data, (bytes, bytearray)):
            data = data.decode()
        if isinstance(data, str):
            return json.loads(data)
        return dict(data)

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        body = self._request_json(request_call)
        order_body = body["order"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), order_body["symbolIsin"])
        expected_side = CONSTANTS.SIDE_BUY if order.trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        self.assertEqual(expected_side, order_body["side"])
        self.assertEqual(int(order.amount), order_body["quantity"])
        self.assertEqual(int(order.price), order_body["price"])
        self.assertEqual(CONSTANTS.ORDER_FROM, order_body["orderFrom"])

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        body = self._request_json(request_call)
        self.assertEqual(str(order.exchange_order_id), str(body["orderId"]))
        self.assertEqual(CONSTANTS.ORDER_FROM, body["orderFrom"])

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        # Open-orders list has no query params.
        pass

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        headers = request_call.kwargs.get("headers") or {}
        self.assertEqual(str(order.exchange_order_id), headers.get("order-id"))

    def configure_all_symbols_response(
            self,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> List[str]:
        url = self.all_symbols_url
        mock_api.post(url, body=json.dumps(self.all_symbols_request_mock_response), callback=callback)
        return [url]

    def configure_trading_rules_response(
            self,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> List[str]:
        url = self.trading_rules_url
        mock_api.post(url, body=json.dumps(self.trading_rules_request_mock_response), callback=callback)
        return [url]

    def configure_erroneous_trading_rules_response(
            self,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> List[str]:
        url = self.trading_rules_url
        mock_api.post(url, body=json.dumps(self.trading_rules_request_erroneous_mock_response), callback=callback)
        return [url]

    def configure_successful_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"isSuccessful": True, "id": str(order.exchange_order_id), "message": "", "omsError": None}
        mock_api.delete(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.delete(regex_url, status=400, callback=callback)
        return url

    def configure_order_not_found_error_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"isSuccessful": False, "id": str(order.exchange_order_id), "message": "Mofid order not found"}
        mock_api.delete(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
            self,
            successful_order: InFlightOrder,
            erroneous_order: InFlightOrder,
            mock_api: aioresponses,
    ) -> List[str]:
        return [
            self.configure_successful_cancelation_response(order=successful_order, mock_api=mock_api),
            self.configure_erroneous_cancelation_response(order=erroneous_order, mock_api=mock_api),
        ]

    def _open_order_row(
            self,
            order: InFlightOrder,
            order_state: int,
            executed_quantity: Optional[Decimal] = None,
            trades: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        executed = executed_quantity if executed_quantity is not None else Decimal("0")
        return {
            "id": str(order.exchange_order_id),
            "symbolIsin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "price": int(order.price),
            "quantity": int(order.amount),
            "executedQuantity": int(executed),
            "side": CONSTANTS.SIDE_BUY if order.trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL,
            "orderState": order_state,
            # Live OMS embeds fills here as soon as OrderExecuted (preferred over orderHistory lag).
            "trades": trades if trades is not None else [],
            "createDateTime": "2026-09-05T17:53:29.254595+03:30",
        }

    def _embedded_trade_row(
            self,
            order: InFlightOrder,
            quantity: Decimal,
            price: Decimal,
            trade_id: str,
    ) -> Dict[str, Any]:
        return {
            "dateTime": "2026-09-05T17:58:15+03:30",
            "tradeNumber": int(trade_id),
            "orderId": str(order.exchange_order_id),
            "symbolIsin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "side": CONSTANTS.SIDE_BUY if order.trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL,
            "hidePrice": 0,
            "quantity": int(quantity),
            "price": int(price),
            "isCancel": False,
        }

    def configure_completely_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
            include_embedded_trades: bool = True,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        trades = None
        if include_embedded_trades:
            trades = [
                self._embedded_trade_row(
                    order, order.amount, order.price, self.expected_fill_trade_id
                )
            ]
        response = {
            "orders": [
                self._open_order_row(
                    order,
                    order_state=20,
                    executed_quantity=order.amount,
                    trades=trades if trades is not None else [],
                )
            ]
        }
        # repeat: _update_orders_fills refreshes open orders before status in the same cycle.
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        return url

    def configure_canceled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"orders": [self._open_order_row(order, order_state=18)]}
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        trades_url = web_utils.private_rest_url(CONSTANTS.ORDER_TRADES_PATH_URL)
        mock_api.get(
            re.compile(f"^{trades_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps([]),
            repeat=True,
        )
        return url

    def configure_open_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"orders": [self._open_order_row(order, order_state=6)]}
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        return url

    def configure_http_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=401, callback=callback, repeat=True)
        return url

    def configure_partially_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {
            "orders": [
                self._open_order_row(
                    order,
                    order_state=8,
                    executed_quantity=self.expected_partial_fill_amount,
                    trades=[
                        self._embedded_trade_row(
                            order,
                            self.expected_partial_fill_amount,
                            self.expected_partial_fill_price,
                            self.expected_fill_trade_id,
                        )
                    ],
                )
            ]
        }
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        return url

    def configure_order_not_found_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> List[str]:
        url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=400, body=json.dumps({"message": "Mofid order not found"}), callback=callback, repeat=True)
        return [url]

    def configure_partial_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        # History fallback mock (used when open-order embed is empty).
        url = web_utils.private_rest_url(CONSTANTS.ORDER_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = [
            {
                "isr": str(order.exchange_order_id),
                "tradeNumber": int(self.expected_fill_trade_id),
                "quantity": int(self.expected_partial_fill_amount),
                "price": int(self.expected_partial_fill_price),
                "remain": int(order.amount - self.expected_partial_fill_amount),
                "hasRemain": True,
                "isin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "side": CONSTANTS.SIDE_BUY,
                "date": "2026-09-05T17:58:15",
            }
        ]
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        return url

    def configure_erroneous_http_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=400, callback=callback, repeat=True)
        return url

    def configure_full_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = [
            {
                "isr": str(order.exchange_order_id),
                "tradeNumber": int(self.expected_fill_trade_id),
                "quantity": int(order.amount),
                "price": int(order.price),
                "remain": 0,
                "hasRemain": False,
                "isin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "side": CONSTANTS.SIDE_BUY,
                "date": "2026-09-05T17:58:15",
            }
        ]
        mock_api.get(regex_url, body=json.dumps(response), callback=callback, repeat=True)
        return url

    def order_event_for_new_order_websocket_update(self, order: InFlightOrder):
        return {
            "e": CONSTANTS.USER_ORDER_EVENT_TYPE,
            "meta": {
                "isr": str(order.exchange_order_id),
                "type": "OrderResult",
                "state": "OnBoard",
                "price": int(order.price),
                "quantity": int(order.amount),
                "side": CONSTANTS.SIDE_BUY,
                "symbolIsin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "date": "20260905175452",
            },
        }

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        return {
            "e": CONSTANTS.USER_ORDER_EVENT_TYPE,
            "meta": {
                "isr": str(order.exchange_order_id),
                "type": "OrderResult",
                "state": "CancelByBroker",
                "price": int(order.price),
                "quantity": int(order.amount),
                "side": CONSTANTS.SIDE_BUY,
                "symbolIsin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "date": "20260905175509",
            },
        }

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return {
            "e": CONSTANTS.USER_ORDER_EVENT_TYPE,
            "meta": {
                "isr": str(order.exchange_order_id),
                "type": "Trade",
                "state": "OrderExecuted",
                "tradeNumber": int(self.expected_fill_trade_id),
                "quantity": int(order.amount),
                "price": int(order.price),
                "remain": 0,
                "hasRemain": False,
                "side": CONSTANTS.SIDE_BUY,
                "isin": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                "date": "20260905175355",
            },
        }

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        # Same payload as order fill Trade event; listener handles type=Trade.
        return self.order_event_for_full_fill_websocket_update(order)

    def _configure_balance_response(
            self,
            response: Dict[str, Any],
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None,
    ) -> str:
        money_url = web_utils.private_rest_url(CONSTANTS.MONEY_PATH_URL)
        open_orders_url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        portfolio_url = web_utils.private_rest_url(CONSTANTS.PORTFOLIO_PATH_URL)
        mock_api.get(
            re.compile(f"^{money_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(response),
            callback=callback,
        )
        mock_api.get(
            re.compile(f"^{open_orders_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"orders": []}),
            callback=callback,
        )
        if response.get("buyPowerT2", 0) == 0 and response.get("t2", 0) == 0:
            portfolio = {
                "items": [
                    {"symbolIsin": self.base_asset, "asset": 10},
                ]
            }
        else:
            portfolio = {
                "items": [
                    {"symbolIsin": self.base_asset, "asset": 15},
                ]
            }
        # For base+quote case abstract expects available base=10 total=15.
        # Without a locked field we can't split; override test_update_balances instead.
        mock_api.get(
            re.compile(f"^{portfolio_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(portfolio),
            callback=callback,
        )
        return money_url

    @aioresponses()
    async def test_invalid_trading_pair_not_in_all_trading_pairs(self, mock_api):
        self.exchange._set_trading_pair_symbol_map(None)
        url = self.all_symbols_url
        invalid_pair, response = self.all_symbols_including_invalid_pair_mock_response
        mock_api.post(url, body=json.dumps(response))
        all_trading_pairs = await self.exchange.all_trading_pairs()
        self.assertNotIn(invalid_pair, all_trading_pairs)

    @aioresponses()
    async def test_all_trading_pairs_does_not_raise_exception(self, mock_api):
        self.exchange._set_trading_pair_symbol_map(None)
        url = self.all_symbols_url
        mock_api.post(url, exception=Exception)
        result = await self.exchange.all_trading_pairs()
        self.assertEqual(0, len(result))

    @aioresponses()
    async def test_update_balances(self, mock_api):
        money = self.balance_request_mock_response_for_base_and_quote
        money_url = web_utils.private_rest_url(CONSTANTS.MONEY_PATH_URL)
        open_orders_url = web_utils.private_rest_url(CONSTANTS.OPEN_ORDERS_PATH_URL)
        portfolio_url = web_utils.private_rest_url(CONSTANTS.PORTFOLIO_PATH_URL)
        mock_api.get(
            re.compile(f"^{money_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(money),
        )
        mock_api.get(
            re.compile(f"^{open_orders_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"orders": []}),
        )
        mock_api.get(
            re.compile(f"^{portfolio_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"items": [{"symbolIsin": self.base_asset, "asset": 15}]}),
        )
        await self.exchange._update_balances()

        self.assertEqual(Decimal("15"), self.exchange.available_balances[self.base_asset])
        self.assertEqual(Decimal("15"), self.exchange.get_all_balances()[self.base_asset])
        self.assertEqual(Decimal("2000"), self.exchange.available_balances[self.quote_asset])
        self.assertEqual(Decimal("2000"), self.exchange.get_all_balances()[self.quote_asset])

        # Second poll: only base holding; cash still present from money API.
        mock_api.get(
            re.compile(f"^{money_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(self.balance_request_mock_response_only_base),
        )
        mock_api.get(
            re.compile(f"^{open_orders_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"orders": []}),
        )
        mock_api.get(
            re.compile(f"^{portfolio_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"items": [{"symbolIsin": self.base_asset, "asset": 10}]}),
        )
        await self.exchange._update_balances()
        self.assertEqual(Decimal("10"), self.exchange.available_balances[self.base_asset])
        self.assertEqual(Decimal("0"), self.exchange.available_balances[self.quote_asset])

        # Open buy lock: available=buyPowerT2, total=buyPowerT2+blockT2 (T+0 buyPower ignored).
        mock_api.get(
            re.compile(f"^{money_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({
                "buyPowerT0": 10000000,
                "buyPowerT1": 40497717,
                "buyPowerT2": 40497717,
                "t2": 51513077,
                "block": 11015360,
                "blockT2": 11015360,
            }),
        )
        mock_api.get(
            re.compile(f"^{open_orders_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"orders": []}),
        )
        mock_api.get(
            re.compile(f"^{portfolio_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"items": []}),
        )
        await self.exchange._update_balances()
        self.assertEqual(Decimal("40497717"), self.exchange.available_balances[self.quote_asset])
        self.assertEqual(Decimal("51513077"), self.exchange.get_all_balances()[self.quote_asset])
        self.assertNotIn(self.base_asset, self.exchange.available_balances)

        # Open sell lock: portfolio still reports full asset; available must subtract resting sell qty.
        mock_api.get(
            re.compile(f"^{money_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(money),
        )
        mock_api.get(
            re.compile(f"^{open_orders_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps(
                {
                    "orders": [
                        {
                            "id": "sell-lock-1",
                            "symbolIsin": self.base_asset,
                            "side": CONSTANTS.SIDE_SELL,
                            "quantity": 20,
                            "executedQuantity": 0,
                            "orderState": 6,
                        }
                    ]
                }
            ),
        )
        mock_api.get(
            re.compile(f"^{portfolio_url}".replace(".", r"\.").replace("?", r"\?")),
            body=json.dumps({"items": [{"symbolIsin": self.base_asset, "asset": 20}]}),
        )
        await self.exchange._update_balances()
        self.assertEqual(Decimal("20"), self.exchange.get_all_balances()[self.base_asset])
        self.assertEqual(Decimal("0"), self.exchange.available_balances[self.base_asset])

    @aioresponses()
    async def test_check_network_success(self, mock_api):
        url = self.network_status_url
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps(self.network_status_request_successful_mock_response))
        network_status = await self.exchange.check_network()
        from hummingbot.core.network_iterator import NetworkStatus
        self.assertEqual(NetworkStatus.CONNECTED, network_status)

    @aioresponses()
    async def test_update_time_synchronizer_uses_server_timestamp(self, mock_api):
        url = self.network_status_url
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        server_ms = 1789209510125
        mock_api.get(
            regex_url,
            body=json.dumps({"diff": 50, "serverTimestamp": server_ms}),
            repeat=True,
        )
        await self.exchange._update_time_synchronizer()
        # Offset samples registered ⇒ synchronizer time is finite (not NaN).
        self.assertTrue(len(self.exchange._time_synchronizer._time_offset_ms) > 0)
        synced = self.exchange._time_synchronizer.time()
        self.assertEqual(synced, synced)

    @aioresponses()
    async def test_check_network_failure(self, mock_api):
        url = self.network_status_url
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=500)
        from hummingbot.core.network_iterator import NetworkStatus
        ret = await self.exchange.check_network()
        self.assertEqual(ret, NetworkStatus.NOT_CONNECTED)

    @aioresponses()
    async def test_check_network_raises_cancel_exception(self, mock_api):
        import asyncio
        url = self.network_status_url
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, exception=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.exchange.check_network()

    @aioresponses()
    async def test_get_last_trade_prices(self, mock_api):
        url = self.latest_prices_url
        mock_api.get(url, body=json.dumps(self.latest_prices_request_mock_response))
        latest_prices = await self.exchange.get_last_traded_prices(trading_pairs=[self.trading_pair])
        self.assertEqual(1, len(latest_prices))
        self.assertEqual(self.expected_latest_price, latest_prices[self.trading_pair])

    @aioresponses()
    async def test_update_trading_rules_ignores_rule_with_error(self, mock_api):
        self.exchange._set_current_timestamp(1000)
        self.configure_erroneous_trading_rules_response(mock_api=mock_api)
        await self.exchange._update_trading_rules()
        self.assertEqual(0, len(self.exchange._trading_rules))
        self.assertTrue(self.is_logged("ERROR", self.expected_logged_error_for_erroneous_trading_rule))

    async def test_user_stream_update_for_order_full_fill(self):
        # Override: our Trade event carries fill + OrderExecuted in one meta payload.
        import asyncio

        from hummingbot.core.event.events import BuyOrderCompletedEvent, OrderFilledEvent

        self.exchange._set_current_timestamp(1640780000)
        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=str(self.expected_exchange_order_id),
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        event = self.order_event_for_full_fill_websocket_update(order=order)
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [event, asyncio.CancelledError]
        self.exchange._user_stream_tracker._user_stream = mock_queue

        try:
            await self.exchange._user_stream_event_listener()
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.1)

        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(order.client_order_id, fill_event.order_id)
        self.assertEqual(order.amount, fill_event.amount)
        buy_event: BuyOrderCompletedEvent = self.buy_order_completed_logger.event_log[0]
        self.assertEqual(order.client_order_id, buy_event.order_id)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
