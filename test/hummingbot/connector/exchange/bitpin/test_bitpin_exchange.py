import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses
from aioresponses.core import RequestCall

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.bitpin import bitpin_constants as CONSTANTS, bitpin_web_utils as web_utils
from hummingbot.connector.exchange.bitpin.bitpin_exchange import BitpinExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_client_order_id
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCreatedEvent,
)


class BitpinExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)

    @property
    def latest_prices_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.TICKER_PRICE_CHANGE_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def network_status_url(self):
        url = web_utils.private_rest_url(CONSTANTS.PING_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def trading_rules_url(self):
        url = web_utils.private_rest_url(CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def order_creation_url(self):
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def balance_url(self):
        url = web_utils.private_rest_url(CONSTANTS.ACCOUNTS_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def all_symbols_request_mock_response(self):
        return [
            {
                'symbol': self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                'name': "Tether/Toman",
                'base': self.base_asset,
                'quote': self.quote_asset,
                'tradable': 'true',
                'price_precision': 0,
                'base_amount_precision': 2,
                'quote_amount_precision': 0
            },
        ]

    @property
    def latest_prices_request_mock_response(self):
        return [
            {
                "symbol": "USDT_IRT",
                "price": "80868",
                "daily_change_price": -1.59,
                "low": "80000",
                "high": "82253",
                "timestamp": 1746852797.634557
            }
        ]

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        response = [
            {
                'symbol': self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                'name': "Tether/Toman",
                'base': self.base_asset,
                'quote': self.quote_asset,
                'tradable': 'true',
                'price_precision': 0,
                'base_amount_precision': 2,
                'quote_amount_precision': 0
            },
            {
                'symbol': self.exchange_symbol_for_tokens("INVALID", "PAIR"),
                'name': "INVALID/PAIR",
                'base': "INVALID",
                'quote': "PAIR",
                'tradable': 'false',
                'price_precision': 0,
                'base_amount_precision': 2,
                'quote_amount_precision': 0
            }
        ]

        return "INVALID-PAIR", response

    @property
    def network_status_request_successful_mock_response(self):
        return {}

    @property
    def trading_rules_request_mock_response(self):
        return [
            {
                'symbol': self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                'name': "Tether/Toman",
                'base': self.base_asset,
                'quote': self.quote_asset,
                'tradable': 'true',
                'price_precision': 0,
                'base_amount_precision': 2,
                'quote_amount_precision': 0
            },
        ]

    @property
    def trading_rules_request_erroneous_mock_response(self):
        return [
            {
                'symbol': self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                'name': "Tether/Toman",
                'base': self.base_asset,
                'quote': self.quote_asset,
                'tradable': 'true',
                'price_precision': None,
                'base_amount_precision': 2,
                'quote_amount_precision': None
            },
        ]

    @aioresponses()
    def test_update_trading_rules_ignores_rule_with_error(self, mock_api):
        self.exchange._set_current_timestamp(1000)

        self.configure_erroneous_trading_rules_response(mock_api=mock_api)

        self.async_run_with_timeout(coroutine=self.exchange._update_trading_rules())

        self.assertEqual(0, len(self.exchange._trading_rules))
        self.assertTrue(
            self.is_logged("ERROR", self.expected_logged_error_for_erroneous_trading_rule)
        )

    @property
    def order_creation_request_successful_mock_response(self):
        return {
            'id': self.expected_exchange_order_id,
            'symbol': self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            'type': 'limit',
            'side': 'sell',
            'price': '100000',
            'stop_price': None,
            'oco_target_price': None,
            'base_amount': '1.50',
            'quote_amount': '150000',
            'identifier': 'OID1',
            'state': 'active',
            'closed_at': None,
            'created_at': '2025-05-15T16:30:01.879340+03:30',
            'dealed_base_amount': '0.00',
            'dealed_quote_amount': '0',
            'req_to_cancel': False,
            'commission': '0.00'
        }

    @aioresponses()
    def test_create_buy_limit_order_successfully(self, mock_api):
        self._simulate_trading_rules_initialized()
        request_sent_event = asyncio.Event()
        self.exchange._set_current_timestamp(1640780000)
        self.exchange.authenticator.access_token = 'test_access_token'

        url = self.order_creation_url

        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        creation_response = self.order_creation_request_successful_mock_response

        mock_api.post(url,
                      body=json.dumps(creation_response),
                      callback=lambda *args, **kwargs: request_sent_event.set())

        order_id = self.place_buy_order()
        self.async_run_with_timeout(request_sent_event.wait())

        order_request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(order_request)
        self.assertIn(order_id, self.exchange.in_flight_orders)
        self.validate_order_creation_request(
            order=self.exchange.in_flight_orders[order_id],
            request_call=order_request)

        create_event: BuyOrderCreatedEvent = self.buy_order_created_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, create_event.timestamp)
        self.assertEqual(self.trading_pair, create_event.trading_pair)
        self.assertEqual(OrderType.LIMIT, create_event.type)
        self.assertEqual(Decimal("100"), create_event.amount)
        self.assertEqual(Decimal("10000"), create_event.price)
        self.assertEqual(order_id, create_event.order_id)
        self.assertEqual(str(self.expected_exchange_order_id), create_event.exchange_order_id)

        self.assertTrue(
            self.is_logged(
                "INFO",
                f"Created {OrderType.LIMIT.name} {TradeType.BUY.name} order {order_id} for "
                f"{Decimal('100.000000')} {self.trading_pair} at {Decimal('10000.0000')}."
            )
        )

    @aioresponses()
    def test_create_sell_limit_order_successfully(self, mock_api):
        self._simulate_trading_rules_initialized()
        request_sent_event = asyncio.Event()
        self.exchange._set_current_timestamp(1640780000)
        self.exchange.authenticator.access_token = 'test_access_token'

        url = self.order_creation_url

        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        creation_response = self.order_creation_request_successful_mock_response

        mock_api.post(url,
                      body=json.dumps(creation_response),
                      callback=lambda *args, **kwargs: request_sent_event.set())

        order_id = self.place_sell_order()
        self.async_run_with_timeout(request_sent_event.wait())

        order_request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(order_request)
        self.assertIn(order_id, self.exchange.in_flight_orders)
        self.validate_order_creation_request(
            order=self.exchange.in_flight_orders[order_id],
            request_call=order_request)

        create_event: SellOrderCreatedEvent = self.sell_order_created_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, create_event.timestamp)
        self.assertEqual(self.trading_pair, create_event.trading_pair)
        self.assertEqual(OrderType.LIMIT, create_event.type)
        self.assertEqual(Decimal("100"), create_event.amount)
        self.assertEqual(Decimal("10000"), create_event.price)
        self.assertEqual(order_id, create_event.order_id)
        self.assertEqual(str(self.expected_exchange_order_id), create_event.exchange_order_id)

        self.assertTrue(
            self.is_logged(
                "INFO",
                f"Created {OrderType.LIMIT.name} {TradeType.SELL.name} order {order_id} for "
                f"{Decimal('100.000000')} {self.trading_pair} at {Decimal('10000.0000')}."
            )
        )

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        return [
            {
                "id": 17594549,
                "asset": "USDT",
                "balance": "0.49",
                "frozen": "1.50",
                "service": "main"
            },
            {
                "id": 17594548,
                "asset": "BTC",
                "balance": "0E-8",
                "frozen": "0E-8",
                "service": "main"
            },
            {
                "id": 964711,
                "asset": "IRT",
                "balance": "27246",
                "frozen": "0",
                "service": "main"
            }
        ]

    @property
    def balance_request_mock_response_only_base(self):
        return [
            {
                "id": 17594549,
                "asset": "USDT",
                "balance": "0.49",
                "frozen": "1.50",
                "service": "main"
            },
        ]

    @aioresponses()
    def test_update_balances(self, mock_api):
        response = self.balance_request_mock_response_for_base_and_quote
        self._configure_balance_response(response=response, mock_api=mock_api)

        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        self.async_run_with_timeout(self.exchange._update_balances())

        available_balances = self.exchange.available_balances
        total_balances = self.exchange.get_all_balances()

        self.assertEqual(Decimal("0.49"), available_balances[self.base_asset])
        self.assertEqual(Decimal("27246"), available_balances[self.quote_asset])
        self.assertEqual(Decimal("1.99"), total_balances[self.base_asset])
        self.assertEqual(Decimal("27246"), total_balances[self.quote_asset])

        response = self.balance_request_mock_response_only_base

        self._configure_balance_response(response=response, mock_api=mock_api)
        self.async_run_with_timeout(self.exchange._update_balances())

        available_balances = self.exchange.available_balances
        total_balances = self.exchange.get_all_balances()

        self.assertNotIn(self.quote_asset, available_balances)
        self.assertNotIn(self.quote_asset, total_balances)
        self.assertEqual(Decimal("0.49"), available_balances[self.base_asset])
        self.assertEqual(Decimal("1.99"), total_balances[self.base_asset])

    @property
    def balance_event_websocket_update(self):
        return {
            "e": "outboundAccountPosition",
            "E": 1564034571105,
            "u": 1564034571073,
            "B": [{"a": self.base_asset, "f": "10", "l": "5"}],
        }

    @property
    def expected_latest_price(self):
        return 80868

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    @property
    def expected_trading_rule(self):
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal(10 ** (-int(self.trading_rules_request_mock_response[0]["base_amount_precision"]))),
            min_price_increment=Decimal(10 ** (-int(self.trading_rules_request_mock_response[0]["price_precision"]))),
            min_base_amount_increment=Decimal(
                10 ** (-int(self.trading_rules_request_mock_response[0]["base_amount_precision"]))),
            min_notional_size=Decimal(1e5),
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        erroneous_rule = self.trading_rules_request_erroneous_mock_response[0]
        return f"Error parsing the trading pair rule {erroneous_rule}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return 28

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return True

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return False

    @property
    def expected_partial_fill_price(self) -> Decimal:
        return Decimal(10500)

    @property
    def expected_partial_fill_amount(self) -> Decimal:
        return Decimal("0.5")

    @property
    def expected_fill_fee(self) -> TradeFeeBase:
        return DeductedFromReturnsTradeFee(
            percent_token=self.quote_asset,
            flat_fees=[TokenAmount(token=self.quote_asset, amount=Decimal("30"))])

    @property
    def expected_fill_trade_id(self) -> str:
        return str(30000)

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        return f"{base_token}_{quote_token}"

    def create_exchange_instance(self):
        client_config_map = ClientConfigAdapter(ClientConfigMap())
        self.base_asset = "USDT"
        self.quote_asset = "IRT"
        self.trading_pair = f"{self.base_asset}-{self.quote_asset}"
        return BitpinExchange(
            client_config_map=client_config_map,
            bitpin_api_key="testAPIKey",
            bitpin_api_secret="testSecret",
            trading_pairs=[self.trading_pair],
        )

    def validate_auth_credentials_present(self, request_call: RequestCall):
        self._validate_auth_credentials_taking_parameters_from_argument(
            request_call_tuple=request_call,
            params=request_call.kwargs["params"] or request_call.kwargs["data"]
        )

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_data["symbol"])
        self.assertEqual(order.trade_type.name.lower(), request_data["side"])
        self.assertEqual(BitpinExchange.bitpin_order_type(OrderType.LIMIT), request_data["type"])
        self.assertEqual(Decimal("100"), Decimal(request_data["base_amount"]))
        self.assertEqual(Decimal("10000"), Decimal(request_data["price"]))
        self.assertEqual(order.client_order_id, request_data["identifier"])

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        pass

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        pass

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        request_params = request_call.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                         request_params["symbol"])
        self.assertEqual(order.exchange_order_id, str(request_params["orderId"]))

    def configure_successful_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL) + order.exchange_order_id + '/'
        response = self._order_cancelation_request_successful_mock_response(order=order)
        mock_api.delete(url, status=204, body=response, callback=callback)
        return url

    @aioresponses()
    def test_cancel_order_successfully(self, mock_api):
        request_sent_event = asyncio.Event()
        self.exchange._set_current_timestamp(1640780000)

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=self.exchange_order_id_prefix + "1",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("100"),
            order_type=OrderType.LIMIT,
        )

        self.assertIn(self.client_order_id_prefix + "1", self.exchange.in_flight_orders)
        order: InFlightOrder = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        url = self.configure_successful_cancelation_response(
            order=order,
            mock_api=mock_api,
            callback=lambda *args, **kwargs: request_sent_event.set())

        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        self.exchange.cancel(trading_pair=order.trading_pair, client_order_id=order.client_order_id)
        self.async_run_with_timeout(request_sent_event.wait())

        if url != "":
            cancel_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(cancel_request)
            self.validate_order_cancelation_request(
                order=order,
                request_call=cancel_request)

        if self.exchange.is_cancel_request_in_exchange_synchronous:
            self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
            self.assertTrue(order.is_cancelled)
            cancel_event: OrderCancelledEvent = self.order_cancelled_logger.event_log[0]
            self.assertEqual(self.exchange.current_timestamp, cancel_event.timestamp)
            self.assertEqual(order.client_order_id, cancel_event.order_id)

            self.assertTrue(
                self.is_logged(
                    "INFO",
                    f"Successfully canceled order {order.client_order_id}."
                )
            )
        else:
            self.assertIn(order.client_order_id, self.exchange.in_flight_orders)
            self.assertTrue(order.is_pending_cancel_confirmation)

    @aioresponses()
    def test_cancel_lost_order_raises_failure_event_when_request_fails(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        request_sent_event = asyncio.Event()
        self.exchange._set_current_timestamp(1640780000)

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=self.exchange_order_id_prefix + "1",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("100"),
            order_type=OrderType.LIMIT,
        )

        self.assertIn(self.client_order_id_prefix + "1", self.exchange.in_flight_orders)
        order = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        for _ in range(self.exchange._order_tracker._lost_order_count_limit + 1):
            self.async_run_with_timeout(
                self.exchange._order_tracker.process_order_not_found(client_order_id=order.client_order_id))

        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)

        url = self.configure_erroneous_cancelation_response(
            order=order,
            mock_api=mock_api,
            callback=lambda *args, **kwargs: request_sent_event.set())

        self.async_run_with_timeout(self.exchange._cancel_lost_orders())
        self.async_run_with_timeout(request_sent_event.wait())

        if url:
            cancel_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(cancel_request)
            self.validate_order_cancelation_request(
                order=order,
                request_call=cancel_request)

        self.assertIn(order.client_order_id, self.exchange._order_tracker.lost_orders)
        self.assertEquals(0, len(self.order_cancelled_logger.event_log))
        self.assertTrue(
            any(
                log.msg.startswith(f"Failed to cancel order {order.client_order_id}")
                for log in self.log_records
            )
        )

    @aioresponses()
    def test_cancel_order_not_found_in_the_exchange(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

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

        self.assertIn(self.client_order_id_prefix + "1", self.exchange.in_flight_orders)
        order = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        self.configure_order_not_found_error_cancelation_response(
            order=order, mock_api=mock_api, callback=lambda *args, **kwargs: request_sent_event.set()
        )

        self.exchange.cancel(trading_pair=self.trading_pair, client_order_id=self.client_order_id_prefix + "1")
        self.async_run_with_timeout(request_sent_event.wait())

        self.assertFalse(order.is_done)
        self.assertFalse(order.is_failure)
        self.assertFalse(order.is_cancelled)

        self.assertIn(order.client_order_id, self.exchange._order_tracker.all_updatable_orders)
        self.assertEqual(1, self.exchange._order_tracker._order_not_found_records[order.client_order_id])

    @aioresponses()
    def test_cancel_two_orders_with_cancel_all_and_one_fails(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        self.exchange._set_current_timestamp(1640780000)

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id=self.exchange_order_id_prefix + "1",
            trading_pair=self.trading_pair,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("100"),
            order_type=OrderType.LIMIT,
        )

        self.assertIn(self.client_order_id_prefix + "1", self.exchange.in_flight_orders)
        order1 = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        self.exchange.start_tracking_order(
            order_id="12",
            exchange_order_id="5",
            trading_pair=self.trading_pair,
            trade_type=TradeType.SELL,
            price=Decimal("11000"),
            amount=Decimal("90"),
            order_type=OrderType.LIMIT,
        )

        self.assertIn("12", self.exchange.in_flight_orders)
        order2 = self.exchange.in_flight_orders["12"]

        urls = self.configure_one_successful_one_erroneous_cancel_all_response(
            successful_order=order1,
            erroneous_order=order2,
            mock_api=mock_api)

        cancellation_results = self.async_run_with_timeout(self.exchange.cancel_all(10))

        for url in urls:
            cancel_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(cancel_request)

        self.assertEqual(2, len(cancellation_results))
        self.assertEqual(CancellationResult(order1.client_order_id, True), cancellation_results[0])
        self.assertEqual(CancellationResult(order2.client_order_id, False), cancellation_results[1])

        if self.exchange.is_cancel_request_in_exchange_synchronous:
            self.assertEqual(1, len(self.order_cancelled_logger.event_log))
            cancel_event: OrderCancelledEvent = self.order_cancelled_logger.event_log[0]
            self.assertEqual(self.exchange.current_timestamp, cancel_event.timestamp)
            self.assertEqual(order1.client_order_id, cancel_event.order_id)

            self.assertTrue(
                self.is_logged(
                    "INFO",
                    f"Successfully canceled order {order1.client_order_id}."
                )
            )

    def configure_erroneous_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL) + order.exchange_order_id + '/'
        mock_api.delete(url, status=400, callback=callback)
        return url

    def configure_order_not_found_error_cancelation_response(
            self, order: InFlightOrder, mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"detail": "not allowed"}
        mock_api.delete(regex_url, status=406, body=json.dumps(response), callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
            self,
            successful_order: InFlightOrder,
            erroneous_order: InFlightOrder,
            mock_api: aioresponses) -> List[str]:
        """
        :return: a list of all configured URLs for the cancelations
        """
        all_urls = []
        url = self.configure_successful_cancelation_response(order=successful_order, mock_api=mock_api)
        all_urls.append(url)
        url = self.configure_erroneous_cancelation_response(order=erroneous_order, mock_api=mock_api)
        all_urls.append(url)
        return all_urls

    def configure_completely_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_completely_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    @aioresponses()
    def test_update_order_status_when_filled_correctly_processed_even_when_trade_fill_update_fails(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

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

        if self.is_order_fill_http_update_included_in_status_update:
            trade_url = self.configure_erroneous_http_fill_trade_response(
                order=order,
                mock_api=mock_api)

        urls = self.configure_completely_filled_order_status_response(
            order=order,
            mock_api=mock_api)

        # Since the trade fill update will fail we need to manually set the event
        # to allow the ClientOrderTracker to process the last status update
        order.completely_filled_event.set()
        self.async_run_with_timeout(self.exchange._update_order_status())
        # Execute one more synchronization to ensure the async task that processes the update is finished
        self.async_run_with_timeout(order.wait_until_completely_filled())

        for url in (urls if isinstance(urls, list) else [urls]):
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        self.assertTrue(order.is_filled)
        self.assertTrue(order.is_done)

        if self.is_order_fill_http_update_included_in_status_update:
            if trade_url:
                trades_request = self._all_executed_requests(mock_api, trade_url)[0]
                self.validate_auth_credentials_present(trades_request)
                self.validate_trades_request(
                    order=order,
                    request_call=trades_request)

        self.assertEqual(0, len(self.order_filled_logger.event_log))

        buy_event: BuyOrderCompletedEvent = self.buy_order_completed_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, buy_event.timestamp)
        self.assertEqual(order.client_order_id, buy_event.order_id)
        self.assertEqual(order.base_asset, buy_event.base_asset)
        self.assertEqual(order.quote_asset, buy_event.quote_asset)
        self.assertEqual(Decimal(0), buy_event.base_asset_amount)
        self.assertEqual(Decimal(0), buy_event.quote_asset_amount)
        self.assertEqual(order.order_type, buy_event.order_type)
        self.assertEqual(order.exchange_order_id, buy_event.exchange_order_id)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
        self.assertTrue(
            self.is_logged(
                "INFO",
                f"BUY order {order.client_order_id} completely filled."
            )
        )

    @aioresponses()
    def test_update_order_status_when_canceled(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

        self.exchange._set_current_timestamp(1640780000)

        self.exchange.start_tracking_order(
            order_id=self.client_order_id_prefix + "1",
            exchange_order_id="100234",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders[self.client_order_id_prefix + "1"]

        urls = self.configure_canceled_order_status_response(
            order=order,
            mock_api=mock_api)

        self.async_run_with_timeout(self.exchange._update_order_status())

        for url in (urls if isinstance(urls, list) else [urls]):
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        cancel_event: OrderCancelledEvent = self.order_cancelled_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, cancel_event.timestamp)
        self.assertEqual(order.client_order_id, cancel_event.order_id)
        self.assertEqual(order.exchange_order_id, cancel_event.exchange_order_id)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
        self.assertTrue(
            self.is_logged("INFO", f"Successfully canceled order {order.client_order_id}.")
        )

    @aioresponses()
    def test_update_order_status_when_order_has_not_changed(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

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

        urls = self.configure_open_order_status_response(
            order=order,
            mock_api=mock_api)

        self.assertTrue(order.is_open)

        self.async_run_with_timeout(self.exchange._update_order_status())

        for url in (urls if isinstance(urls, list) else [urls]):
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(order=order, request_call=order_status_request)

        self.assertTrue(order.is_open)
        self.assertFalse(order.is_filled)
        self.assertFalse(order.is_done)

    @aioresponses()
    def test_update_order_status_when_request_fails_marks_order_as_not_found(self, mock_api):
        auth_url = "https://api.bitpin.ir/api/v1/usr/authenticate/"
        mock_api.post(auth_url,
                      status=200,
                      body=json.dumps({
                          "access": "fake_access_token",
                          "refresh": "fake_refresh_token"
                      }))

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

        url = self.configure_http_error_order_status_response(
            order=order,
            mock_api=mock_api)

        self.async_run_with_timeout(self.exchange._update_order_status())

        if url:
            order_status_request = self._all_executed_requests(mock_api, url)[0]
            self.validate_auth_credentials_present(order_status_request)
            self.validate_order_status_request(
                order=order,
                request_call=order_status_request)

        self.assertTrue(order.is_open)
        self.assertFalse(order.is_filled)
        self.assertFalse(order.is_done)

        self.assertEqual(1, self.exchange._order_tracker._order_not_found_records[order.client_order_id])

    def configure_canceled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_canceled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_http_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(url + r"\?.*")
        mock_api.get(regex_url, status=400, callback=callback)
        return url

    def configure_open_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        """
        :return: the URL configured
        """
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_open_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_http_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=401, callback=callback)
        return url

    def configure_partially_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_partially_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_order_not_found_error_order_status_response(
            self, order: InFlightOrder, mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> List[str]:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"code": -2013, "msg": "Order does not exist."}
        mock_api.get(regex_url, body=json.dumps(response), status=400, callback=callback)
        return [url]

    def configure_partial_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(url + r"\?.*")
        response = self._order_fills_request_partial_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_full_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(url + r"\?.*")
        response = self._order_fills_request_full_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def order_event_for_new_order_websocket_update(self, order: InFlightOrder):
        return {
            "e": "executionReport",
            "E": 1499405658658,
            "s": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "c": order.client_order_id,
            "S": order.trade_type.name.upper(),
            "o": order.order_type.name.upper(),
            "f": "GTC",
            "q": str(order.amount),
            "p": str(order.price),
            "P": "0.00000000",
            "F": "0.00000000",
            "g": -1,
            "C": "",
            "x": "NEW",
            "X": "NEW",
            "r": "NONE",
            "i": order.exchange_order_id,
            "l": "0.00000000",
            "z": "0.00000000",
            "L": "0.00000000",
            "n": "0",
            "N": None,
            "T": 1499405658657,
            "t": -1,
            "I": 8641984,
            "w": True,
            "m": False,
            "M": False,
            "O": 1499405658657,
            "Z": "0.00000000",
            "Y": "0.00000000",
            "Q": "0.00000000"
        }

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        return {
            "e": "executionReport",
            "E": 1499405658658,
            "s": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "c": "dummyText",
            "S": order.trade_type.name.upper(),
            "o": order.order_type.name.upper(),
            "f": "GTC",
            "q": str(order.amount),
            "p": str(order.price),
            "P": "0.00000000",
            "F": "0.00000000",
            "g": -1,
            "C": order.client_order_id,
            "x": "CANCELED",
            "X": "CANCELED",
            "r": "NONE",
            "i": order.exchange_order_id,
            "l": "0.00000000",
            "z": "0.00000000",
            "L": "0.00000000",
            "n": "0",
            "N": None,
            "T": 1499405658657,
            "t": -1,
            "I": 8641984,
            "w": True,
            "m": False,
            "M": False,
            "O": 1499405658657,
            "Z": "0.00000000",
            "Y": "0.00000000",
            "Q": "0.00000000"
        }

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return {
            "e": "executionReport",
            "E": 1499405658658,
            "s": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "c": order.client_order_id,
            "S": order.trade_type.name.upper(),
            "o": order.order_type.name.upper(),
            "f": "GTC",
            "q": str(order.amount),
            "p": str(order.price),
            "P": "0.00000000",
            "F": "0.00000000",
            "g": -1,
            "C": "",
            "x": "TRADE",
            "X": "FILLED",
            "r": "NONE",
            "i": order.exchange_order_id,
            "l": str(order.amount),
            "z": str(order.amount),
            "L": str(order.price),
            "n": str(self.expected_fill_fee.flat_fees[0].amount),
            "N": self.expected_fill_fee.flat_fees[0].token,
            "T": 1499405658657,
            "t": 1,
            "I": 8641984,
            "w": True,
            "m": False,
            "M": False,
            "O": 1499405658657,
            "Z": "10050.00000000",
            "Y": "10050.00000000",
            "Q": "10000.00000000"
        }

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return None

    @aioresponses()
    @patch("hummingbot.connector.time_synchronizer.TimeSynchronizer._current_seconds_counter")
    def test_update_time_synchronizer_successfully(self, mock_api, seconds_counter_mock):
        request_sent_event = asyncio.Event()
        seconds_counter_mock.side_effect = [0, 0, 0]

        self.exchange._time_synchronizer.clear_time_offset_ms_samples()
        url = web_utils.private_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {"serverTime": 1640000003000}

        mock_api.get(regex_url,
                     body=json.dumps(response),
                     callback=lambda *args, **kwargs: request_sent_event.set())

        self.async_run_with_timeout(self.exchange._update_time_synchronizer())

        self.assertEqual(response["serverTime"] * 1e-3, self.exchange._time_synchronizer.time())

    @aioresponses()
    def test_update_time_synchronizer_failure_is_logged(self, mock_api):
        request_sent_event = asyncio.Event()

        url = web_utils.private_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        response = {"code": -1121, "msg": "Dummy error"}

        mock_api.get(regex_url,
                     body=json.dumps(response),
                     callback=lambda *args, **kwargs: request_sent_event.set())

        self.async_run_with_timeout(self.exchange._update_time_synchronizer())

        self.assertTrue(self.is_logged("NETWORK", "Error getting server time."))

    @aioresponses()
    def test_update_time_synchronizer_raises_cancelled_error(self, mock_api):
        url = web_utils.private_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        mock_api.get(regex_url,
                     exception=asyncio.CancelledError)

        self.assertRaises(
            asyncio.CancelledError,
            self.async_run_with_timeout, self.exchange._update_time_synchronizer())

    @aioresponses()
    def test_update_order_fills_from_trades_triggers_filled_event(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="1102450298",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("83124"),
            amount=Decimal("2"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        url = web_utils.private_rest_url(CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        trade_fill = {
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "id": '89600603',
            "order_id": str(order.exchange_order_id),
            "price": "81106",
            "base_amount": "2.06",
            "quote_amount": "167078",
            "commission": "584",
            "commission_currency": self.quote_asset,
            "created_at": '2025-04-29T17:12:10.671152+03:30',
            "side": 'sell',
            "identifier": 'null',
        }

        trade_fill_non_tracked_order = {
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "id": '89599933',
            "order_id": '1102428497',
            "price": "81550",
            "base_amount": "2",
            "quote_amount": "163100",
            "commission": "0.0",
            "commission_currency": self.base_asset,
            "created_at": '2025-04-29T16:55:27.461784+03:30',
            "side": 'buy',
            "identifier": 'null',
        }

        mock_response = [trade_fill, trade_fill_non_tracked_order]
        mock_api.get(regex_url, body=json.dumps(mock_response))

        self.exchange.add_exchange_order_ids_from_market_recorder(
            {str(trade_fill_non_tracked_order["order_id"]): "OID99"})

        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades(), 100)

        request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(request)
        # Ignor symbol params. It has a mismatching "-".
        request_params = request.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_params["symbol"])

        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, fill_event.timestamp)
        self.assertEqual(order.client_order_id, fill_event.order_id)
        self.assertEqual(order.trading_pair, fill_event.trading_pair)
        self.assertEqual(order.trade_type, fill_event.trade_type)
        self.assertEqual(order.order_type, fill_event.order_type)
        self.assertEqual(Decimal(trade_fill["price"]), fill_event.price)
        self.assertEqual(Decimal(trade_fill["base_amount"]), fill_event.amount)
        self.assertEqual(0.0, fill_event.trade_fee.percent)
        self.assertEqual([TokenAmount(trade_fill["commission_currency"], Decimal(trade_fill["commission"]))],
                         fill_event.trade_fee.flat_fees)

        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[1]
        self.assertEqual(datetime.fromisoformat(trade_fill_non_tracked_order["created_at"]).timestamp(),
                         fill_event.timestamp)
        self.assertEqual("OID99", fill_event.order_id)
        self.assertEqual(self.trading_pair, fill_event.trading_pair)
        self.assertEqual(TradeType.BUY, fill_event.trade_type)
        self.assertEqual(OrderType.MARKET, fill_event.order_type)
        self.assertEqual(Decimal(trade_fill_non_tracked_order["price"]), fill_event.price)
        self.assertEqual(Decimal(trade_fill_non_tracked_order["base_amount"]), fill_event.amount)
        self.assertEqual(0.0, fill_event.trade_fee.percent)
        self.assertEqual([
            TokenAmount(
                trade_fill_non_tracked_order["commission_currency"],
                Decimal(trade_fill_non_tracked_order["commission"]))],
            fill_event.trade_fee.flat_fees)
        self.assertTrue(self.is_logged(
            "INFO",
            f"Recreating missing trade in TradeFill: {trade_fill_non_tracked_order}"
        ))

    @aioresponses()
    def test_update_order_fills_request_parameters(self, mock_api):
        self.exchange._set_current_timestamp(0)
        self.exchange._last_poll_timestamp = -1

        url = web_utils.private_rest_url(CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        mock_response = []
        mock_api.get(regex_url, body=json.dumps(mock_response))

        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades())

        request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(request)
        request_params = request.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_params["symbol"])
        self.assertNotIn("startTime", request_params)

        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)
        self.exchange._last_trades_poll_bitpin_timestamp = 10
        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades())

        request = self._all_executed_requests(mock_api, url)[1]
        self.validate_auth_credentials_present(request)
        request_params = request.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_params["symbol"])
        # comment the test as bitpin doesn't have startTime
        # self.assertEqual(10 * 1e3, request_params["startTime"])

    @aioresponses()
    def test_update_order_fills_from_trades_with_repeated_fill_triggers_only_one_event(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        url = web_utils.private_rest_url(CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        trade_fill_non_tracked_order = {
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "id": '89599933',
            "order_id": '1102428497',
            "price": "81550",
            "base_amount": "2",
            "quote_amount": "163100",
            "commission": "0.0",
            "commission_currency": self.base_asset,
            "created_at": '2025-04-29T16:55:27.461784+03:30',
            "side": 'buy',
            "identifier": 'null',
        }

        mock_response = [trade_fill_non_tracked_order, trade_fill_non_tracked_order]
        mock_api.get(regex_url, body=json.dumps(mock_response))

        self.exchange.add_exchange_order_ids_from_market_recorder(
            {str(trade_fill_non_tracked_order["order_id"]): "OID99"})

        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades())

        request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(request)
        request_params = request.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_params["symbol"])

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(datetime.fromisoformat(trade_fill_non_tracked_order["created_at"]).timestamp(),
                         fill_event.timestamp)
        self.assertEqual("OID99", fill_event.order_id)
        self.assertEqual(self.trading_pair, fill_event.trading_pair)
        self.assertEqual(TradeType.BUY, fill_event.trade_type)
        self.assertEqual(OrderType.MARKET, fill_event.order_type)
        self.assertEqual(Decimal(trade_fill_non_tracked_order["price"]), fill_event.price)
        self.assertEqual(Decimal(trade_fill_non_tracked_order["base_amount"]), fill_event.amount)
        self.assertEqual(0.0, fill_event.trade_fee.percent)
        self.assertEqual([
            TokenAmount(trade_fill_non_tracked_order["commission_currency"],
                        Decimal(trade_fill_non_tracked_order["commission"]))],
            fill_event.trade_fee.flat_fees)
        self.assertTrue(self.is_logged(
            "INFO",
            f"Recreating missing trade in TradeFill: {trade_fill_non_tracked_order}"
        ))

    @aioresponses()
    def test_update_order_status_when_failed(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="100234",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        order_status = {
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "orderId": int(order.exchange_order_id),
            "orderListId": -1,
            "clientOrderId": order.client_order_id,
            "price": "10000.0",
            "origQty": "1.0",
            "executedQty": "0.0",
            "cummulativeQuoteQty": "0.0",
            "status": "REJECTED",
            "timeInForce": "GTC",
            "type": "LIMIT",
            "side": "BUY",
            "stopPrice": "0.0",
            "icebergQty": "0.0",
            "time": 1499827319559,
            "updateTime": 1499827319559,
            "isWorking": True,
            "origQuoteOrderQty": "10000.000000"
        }

        mock_response = order_status
        mock_api.get(regex_url, body=json.dumps(mock_response))

        self.async_run_with_timeout(self.exchange._update_order_status())

        request = self._all_executed_requests(mock_api, url)[0]
        self.validate_auth_credentials_present(request)
        request_params = request.kwargs["params"]
        self.assertEqual(self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset), request_params["symbol"])
        self.assertEqual(order.client_order_id, request_params["origClientOrderId"])

        failure_event: MarketOrderFailureEvent = self.order_failure_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, failure_event.timestamp)
        self.assertEqual(order.client_order_id, failure_event.order_id)
        self.assertEqual(order.order_type, failure_event.order_type)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
        self.assertTrue(
            self.is_logged(
                "INFO",
                f"Order {order.client_order_id} has failed. Order Update: OrderUpdate(trading_pair='{self.trading_pair}',"
                f" update_timestamp={order_status['updateTime'] * 1e-3}, new_state={repr(OrderState.FAILED)}, "
                f"client_order_id='{order.client_order_id}', exchange_order_id='{order.exchange_order_id}', "
                "misc_updates=None)")
        )

    def test_user_stream_update_for_order_failure(self):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="100234",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        event_message = {
            "e": "executionReport",
            "E": 1499405658658,
            "s": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "c": order.client_order_id,
            "S": "BUY",
            "o": "LIMIT",
            "f": "GTC",
            "q": "1.00000000",
            "p": "1000.00000000",
            "P": "0.00000000",
            "F": "0.00000000",
            "g": -1,
            "C": "",
            "x": "REJECTED",
            "X": "REJECTED",
            "r": "NONE",
            "i": int(order.exchange_order_id),
            "l": "0.00000000",
            "z": "0.00000000",
            "L": "0.00000000",
            "n": "0",
            "N": None,
            "T": 1499405658657,
            "t": 1,
            "I": 8641984,
            "w": True,
            "m": False,
            "M": False,
            "O": 1499405658657,
            "Z": "0.00000000",
            "Y": "0.00000000",
            "Q": "0.00000000"
        }

        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [event_message, asyncio.CancelledError]
        self.exchange._user_stream_tracker._user_stream = mock_queue

        try:
            self.async_run_with_timeout(self.exchange._user_stream_event_listener())
        except asyncio.CancelledError:
            pass

        failure_event: MarketOrderFailureEvent = self.order_failure_logger.event_log[0]
        self.assertEqual(self.exchange.current_timestamp, failure_event.timestamp)
        self.assertEqual(order.client_order_id, failure_event.order_id)
        self.assertEqual(order.order_type, failure_event.order_type)
        self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
        self.assertTrue(order.is_failure)
        self.assertTrue(order.is_done)

    @patch("hummingbot.connector.utils.get_tracking_nonce")
    def test_client_order_id_on_order(self, mocked_nonce):
        mocked_nonce.return_value = 7

        result = self.exchange.buy(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = get_new_client_order_id(
            is_buy=True,
            trading_pair=self.trading_pair,
            hbot_order_id_prefix=CONSTANTS.HBOT_ORDER_ID_PREFIX,
            max_id_len=CONSTANTS.MAX_ORDER_ID_LEN,
        )

        self.assertEqual(result, expected_client_order_id)

        result = self.exchange.sell(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = get_new_client_order_id(
            is_buy=False,
            trading_pair=self.trading_pair,
            hbot_order_id_prefix=CONSTANTS.HBOT_ORDER_ID_PREFIX,
            max_id_len=CONSTANTS.MAX_ORDER_ID_LEN,
        )

        self.assertEqual(result, expected_client_order_id)

    def test_time_synchronizer_related_request_error_detection(self):
        exception = IOError("Error executing request POST https://api.bitpin.com/api/v3/order. HTTP status is 400. "
                            "Error: {'code':-1021,'msg':'Timestamp for this request is outside of the recvWindow.'}")
        self.assertTrue(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

        exception = IOError("Error executing request POST https://api.bitpin.com/api/v3/order. HTTP status is 400. "
                            "Error: {'code':-1021,'msg':'Timestamp for this request was 1000ms ahead of the server's "
                            "time.'}")
        self.assertTrue(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

        exception = IOError("Error executing request POST https://api.bitpin.com/api/v3/order. HTTP status is 400. "
                            "Error: {'code':-1022,'msg':'Timestamp for this request was 1000ms ahead of the server's "
                            "time.'}")
        self.assertFalse(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

        exception = IOError("Error executing request POST https://api.bitpin.com/api/v3/order. HTTP status is 400. "
                            "Error: {'code':-1021,'msg':'Other error.'}")
        self.assertFalse(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

    @aioresponses()
    def test_place_order_manage_server_overloaded_error_unkown_order(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)
        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_response = {"code": -1003, "msg": "Unknown error, please check your request or try again later."}
        mock_api.post(regex_url, body=json.dumps(mock_response), status=503)

        o_id, transact_time = self.async_run_with_timeout(self.exchange._place_order(
            order_id="test_order_id",
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        ))
        self.assertEqual(o_id, "UNKNOWN")

    @aioresponses()
    def test_place_order_manage_server_overloaded_error_failure(self, mock_api):
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_response = {"code": -1003, "msg": "Service Unavailable."}
        mock_api.post(regex_url, body=json.dumps(mock_response), status=503)

        self.assertRaises(
            IOError,
            self.async_run_with_timeout,
            self.exchange._place_order(
                order_id="test_order_id",
                trading_pair=self.trading_pair,
                amount=Decimal("1"),
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("2"),
            ))

        mock_response = {"code": -1003, "msg": "Internal error; unable to process your request. Please try again."}
        mock_api.post(regex_url, body=json.dumps(mock_response), status=503)

        self.assertRaises(
            IOError,
            self.async_run_with_timeout,
            self.exchange._place_order(
                order_id="test_order_id",
                trading_pair=self.trading_pair,
                amount=Decimal("1"),
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("2"),
            ))

    def test_format_trading_rules__min_notional_present(self):
        exchange_info = [
            {
                "symbol": "BTC_IRT",
                "name": "Bitcoin/Toman",
                "base": "BTC",
                "quote": "IRT",
                "tradable": True,
                "price_precision": 0,
                "base_amount_precision": 8,
                "quote_amount_precision": 0
            },
            {
                "symbol": "BTC_USDT",
                "name": "Bitcoin/Tether",
                "base": "BTC",
                "quote": "USDT",
                "tradable": True,
                "price_precision": 2,
                "base_amount_precision": 8,
                "quote_amount_precision": 2
            },
            {
                "symbol": "USDT_IRT",
                "name": "Tether/Toman",
                "base": "USDT",
                "quote": "IRT",
                "tradable": True,
                "price_precision": 0,
                "base_amount_precision": 2,
                "quote_amount_precision": 0
            }]

        result = self.async_run_with_timeout(self.exchange._format_trading_rules(exchange_info), 1400)

        self.assertEqual(result[0].min_notional_size, Decimal("100000"))

    def test_format_trading_rules__notional_but_no_min_notional_present(self):
        exchange_info = [
            {
                "symbol": "BTC_IRT",
                "name": "Bitcoin/Toman",
                "base": "BTC",
                "quote": "IRT",
                "tradable": True,
                "price_precision": 0,
                "base_amount_precision": 8,
                "quote_amount_precision": 0
            },
            {
                "symbol": "BTC_USDT",
                "name": "Bitcoin/Tether",
                "base": "BTC",
                "quote": "USDT",
                "tradable": True,
                "price_precision": 2,
                "base_amount_precision": 8,
                "quote_amount_precision": 2
            },
            {
                "symbol": "USDT_IRT",
                "name": "Tether/Toman",
                "base": "USDT",
                "quote": "IRT",
                "tradable": True,
                "price_precision": 0,
                "base_amount_precision": 2,
                "quote_amount_precision": 0
            }]

        result = self.async_run_with_timeout(self.exchange._format_trading_rules(exchange_info), )

        self.assertEqual(result[0].min_notional_size, Decimal("100000"))

    def _validate_auth_credentials_taking_parameters_from_argument(self,
                                                                   request_call_tuple: RequestCall,
                                                                   params: Dict[str, Any]):
        self.assertIn("Authorization", request_call_tuple.kwargs['headers'])
        self.assertEqual(request_call_tuple.kwargs['headers']['Content-Type'], 'application/json')
        self.assertEqual(request_call_tuple.kwargs["allow_redirects"], True)

    def _order_cancelation_request_successful_mock_response(self, order: InFlightOrder) -> Any:
        return ''

    def _order_status_request_completely_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "type": "limit",
            "side": "buy",
            "price": str(order.price),
            "stop_price": None,
            "oco_target_price": None,
            "base_amount": str(order.amount),
            "quote_amount": str(order.price * order.amount),
            "identifier": order.client_order_id,
            "state": "closed",
            "closed_at": "2025-04-29T17:12:10.767320+03:30",
            "created_at": "2025-04-29T17:12:09.413756+03:30",
            "dealed_base_amount": str(order.amount),
            "dealed_quote_amount": str(order.price * order.amount),
            "req_to_cancel": False,
            "commission": "584.77"
        }

    def _order_status_request_canceled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "type": "limit",
            "side": "sell",
            "price": str(order.price),
            "stop_price": None,
            "oco_target_price": None,
            "base_amount": str(order.amount),
            "quote_amount": str(order.price * order.amount),
            "identifier": order.client_order_id,
            "state": "closed",
            "closed_at": "2025-05-20T08:35:40.714422+03:30",
            "created_at": "2025-05-20T08:35:08.095053+03:30",
            "dealed_base_amount": "0.00",
            "dealed_quote_amount": "0",
            "req_to_cancel": True,
            "commission": "0.00"
        }

    def _order_status_request_open_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "id": order.exchange_order_id,
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "type": "limit",
            "side": order.trade_type.name.lower(),
            "price": str(order.price),
            "stop_price": None,
            "oco_target_price": None,
            "base_amount": str(order.amount),
            "quote_amount": str(order.price * order.amount),
            "identifier": None,
            "state": "active",
            "closed_at": None,
            "created_at": "2025-04-29T17:12:09.413756+03:30",
            "dealed_base_amount": "0.00",
            "dealed_quote_amount": "0",
            "req_to_cancel": False,
            "commission": "0.00"
        }

    def _order_status_request_partially_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "symbol": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
            "orderId": order.exchange_order_id,
            "orderListId": -1,
            "clientOrderId": order.client_order_id,
            "price": str(order.price),
            "origQty": str(order.amount),
            "executedQty": str(order.amount),
            "cummulativeQuoteQty": str(self.expected_partial_fill_amount * order.price),
            "status": "PARTIALLY_FILLED",
            "timeInForce": "GTC",
            "type": order.order_type.name.upper(),
            "side": order.trade_type.name.upper(),
            "stopPrice": "0.0",
            "icebergQty": "0.0",
            "time": 1499827319559,
            "updateTime": 1499827319559,
            "isWorking": True,
            "origQuoteOrderQty": str(order.price * order.amount)
        }

    def _order_fills_request_partial_fill_mock_response(self, order: InFlightOrder):
        return [
            {
                "symbol": self.exchange_symbol_for_tokens(order.base_asset, order.quote_asset),
                "id": self.expected_fill_trade_id,
                "orderId": int(order.exchange_order_id),
                "orderListId": -1,
                "price": str(self.expected_partial_fill_price),
                "qty": str(self.expected_partial_fill_amount),
                "quoteQty": str(self.expected_partial_fill_amount * self.expected_partial_fill_price),
                "commission": str(self.expected_fill_fee.flat_fees[0].amount),
                "commissionAsset": self.expected_fill_fee.flat_fees[0].token,
                "time": 1499865549590,
                "isBuyer": True,
                "isMaker": False,
                "isBestMatch": True
            }
        ]

    def _order_fills_request_full_fill_mock_response(self, order: InFlightOrder):
        return [
            {
                "symbol": self.exchange_symbol_for_tokens(order.base_asset, order.quote_asset),
                "id": self.expected_fill_trade_id,
                "orderId": int(order.exchange_order_id),
                "orderListId": -1,
                "price": str(order.price),
                "qty": str(order.amount),
                "quoteQty": str(order.amount * order.price),
                "commission": str(self.expected_fill_fee.flat_fees[0].amount),
                "commissionAsset": self.expected_fill_fee.flat_fees[0].token,
                "time": 1499865549590,
                "isBuyer": True,
                "isMaker": False,
                "isBestMatch": True
            }
        ]
