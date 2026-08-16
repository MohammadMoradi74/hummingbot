import base64
import json
import re
from decimal import Decimal
from typing import Any, Callable, List, Optional, Tuple

from aioresponses import aioresponses
from aioresponses.core import RequestCall
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from hummingbot.connector.exchange.nobitex import (
    nobitex_constants as CONSTANTS,
    nobitex_utils,
    nobitex_web_utils as web_utils,
)
from hummingbot.connector.exchange.nobitex.nobitex_exchange import NobitexExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase


class NobitexExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)

    @property
    def latest_prices_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.MARKET_STATS_PATH_URL, domain=self.exchange._domain)

    @property
    def network_status_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.PING_PATH_URL, domain=self.exchange._domain)

    @property
    def trading_rules_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=self.exchange._domain)

    @property
    def order_creation_url(self):
        return web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL, domain=self.exchange._domain)

    @property
    def balance_url(self):
        return web_utils.private_rest_url(CONSTANTS.ACCOUNTS_PATH_URL, domain=self.exchange._domain)

    @property
    def all_symbols_request_mock_response(self):
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        return {
            "nobitex": {
                "amountPrecisions": {symbol: "0.01"},
                "pricePrecisions": {symbol: "0.0001"},
                "minOrders": {"usdt": "10"},
            }
        }

    @property
    def latest_prices_request_mock_response(self):
        key = nobitex_utils.stats_market_key(self.trading_pair)
        return {
            "stats": {
                key: {"latest": str(self.expected_latest_price)},
            }
        }

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        response = {
            "nobitex": {
                "amountPrecisions": {
                    symbol: "0.01",
                    "INVALIDPAIR": "0.01",
                },
                "pricePrecisions": {
                    symbol: "0.0001",
                    "INVALIDPAIR": "0.0001",
                },
                "minOrders": {"usdt": "10"},
            }
        }
        return "INVALID-PAIR", response

    @property
    def network_status_request_successful_mock_response(self):
        return {"stats": {}}

    @property
    def trading_rules_request_mock_response(self):
        return self.all_symbols_request_mock_response

    @property
    def trading_rules_request_erroneous_mock_response(self):
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        return {
            "nobitex": {
                "amountPrecisions": {symbol: None},
                "pricePrecisions": {symbol: "0.0001"},
                "minOrders": {"usdt": "10"},
            }
        }

    @property
    def order_creation_request_successful_mock_response(self):
        return {
            "status": "ok",
            "order": {
                "id": self.expected_exchange_order_id,
                "clientOrderId": "OID1",
                "status": "Active",
                "created_at": "2025-05-15T16:30:01.879340+03:30",
                "matchedAmount": "0",
                "unmatchedAmount": "1",
            },
        }

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        return {
            "wallets": {
                self.base_asset: {"balance": "10", "blocked": "5"},
                self.quote_asset: {"balance": "2000", "blocked": "0"},
            }
        }

    @property
    def balance_request_mock_response_only_base(self):
        return {
            "wallets": {
                self.base_asset: {"balance": "10", "blocked": "5"},
            }
        }

    @property
    def balance_event_websocket_update(self):
        # Nobitex has no balance WS channel; abstract test skips when real_time_balance_update is False.
        return {}

    @property
    def expected_latest_price(self):
        return 9999.9

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    @property
    def expected_trading_rule(self):
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        nobitex = self.trading_rules_request_mock_response["nobitex"]
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal(str(nobitex["amountPrecisions"][symbol])),
            min_price_increment=Decimal(str(nobitex["pricePrecisions"][symbol])),
            min_base_amount_increment=Decimal(str(nobitex["amountPrecisions"][symbol])),
            min_notional_size=Decimal(str(nobitex["minOrders"]["usdt"])),
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        symbol = self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)
        return f"Error parsing trading pair rule {symbol}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return 28

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return True

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return True

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
            flat_fees=[TokenAmount(token=self.quote_asset, amount=Decimal("30"))],
        )

    @property
    def expected_fill_trade_id(self) -> str:
        return str(30000)

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        return nobitex_utils.exchange_symbol_for_tokens(base_token, quote_token)

    def create_exchange_instance(self):
        self.base_asset = "COINALPHA"
        self.quote_asset = "USDT"
        self.trading_pair = f"{self.base_asset}-{self.quote_asset}"

        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        seed_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        api_secret = base64.urlsafe_b64encode(seed_bytes).decode("utf-8")
        api_key = base64.urlsafe_b64encode(public_key_bytes).decode("utf-8")

        exchange = NobitexExchange(
            nobitex_api_key=api_key,
            nobitex_api_secret=api_secret,
            trading_pairs=[self.trading_pair],
        )
        # No balance channel on private WS
        exchange.real_time_balance_update = False
        return exchange

    def validate_auth_credentials_present(self, request_call: RequestCall):
        headers = request_call.kwargs["headers"]
        self.assertIn("Nobitex-Key", headers)
        self.assertIn("Nobitex-Signature", headers)
        self.assertIn("Nobitex-Timestamp", headers)
        self.assertIn("User-Agent", headers)
        self.assertEqual(CONSTANTS.USER_AGENT, headers["User-Agent"])

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        src, dst = nobitex_utils.currencies_from_trading_pair(order.trading_pair)
        self.assertEqual(CONSTANTS.SIDE_BUY if order.trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL,
                         request_data["type"])
        self.assertEqual(src, request_data["srcCurrency"])
        self.assertEqual(dst, request_data["dstCurrency"])
        self.assertEqual(Decimal("100"), Decimal(request_data["amount"]))
        self.assertEqual(CONSTANTS.EXECUTION_LIMIT, request_data["execution"])
        self.assertEqual(order.client_order_id, request_data["clientOrderId"])
        self.assertEqual(Decimal("10000"), Decimal(request_data["price"]))

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        self.assertEqual("canceled", request_data["status"])
        self.assertEqual(int(order.exchange_order_id), request_data["order"])

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        request_data = json.loads(request_call.kwargs["data"])
        self.assertEqual(int(order.exchange_order_id), request_data["id"])

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        request_params = request_call.kwargs["params"]
        src, dst = nobitex_utils.currencies_from_trading_pair(order.trading_pair)
        self.assertEqual(src, request_params["srcCurrency"])
        self.assertEqual(dst, request_params["dstCurrency"])

    def configure_successful_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_CANCEL_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_cancelation_request_successful_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_CANCEL_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=400, callback=callback)
        return url

    def configure_order_not_found_error_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_CANCEL_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"message": "No Order matches the given query.", "error": "NotFound"}
        mock_api.post(regex_url, status=404, body=json.dumps(response), callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
            self,
            successful_order: InFlightOrder,
            erroneous_order: InFlightOrder,
            mock_api: aioresponses) -> List[str]:
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
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_completely_filled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_canceled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_canceled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_open_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_open_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_http_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.post(regex_url, status=401, callback=callback)
        return url

    def configure_partially_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_partially_filled_mock_response(order=order)
        mock_api.post(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_order_not_found_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> List[str]:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_STATUS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"message": "No Order matches the given query.", "error": "NotFound"}
        mock_api.post(regex_url, status=404, body=json.dumps(response), callback=callback)
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

    def configure_erroneous_http_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(path_url=CONSTANTS.MY_TRADES_PATH_URL)
        regex_url = re.compile(url + r"\?.*")
        mock_api.get(regex_url, status=400, callback=callback)
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
            "_channel": f"{CONSTANTS.WS_PRIVATE_ORDERS_CHANNEL_PREFIX}{self.base_asset.lower()}",
            "orderId": int(order.exchange_order_id),
            "clientOrderId": order.client_order_id,
            "status": "Active",
            "matchedAmount": "0",
            "unmatchedAmount": str(order.amount),
            "created_at": "2025-05-15T16:30:01.879340+03:30",
        }

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        return {
            "_channel": f"{CONSTANTS.WS_PRIVATE_ORDERS_CHANNEL_PREFIX}{self.base_asset.lower()}",
            "orderId": int(order.exchange_order_id),
            "clientOrderId": order.client_order_id,
            "status": "Canceled",
            "matchedAmount": "0",
            "unmatchedAmount": str(order.amount),
            "created_at": "2025-05-15T16:30:01.879340+03:30",
        }

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return {
            "_channel": f"{CONSTANTS.WS_PRIVATE_ORDERS_CHANNEL_PREFIX}{self.base_asset.lower()}",
            "orderId": int(order.exchange_order_id),
            "clientOrderId": order.client_order_id,
            "status": "Done",
            "matchedAmount": str(order.amount),
            "unmatchedAmount": "0",
            "created_at": "2025-05-15T16:30:01.879340+03:30",
        }

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        return {
            "_channel": f"{CONSTANTS.WS_PRIVATE_TRADES_CHANNEL_PREFIX}{self.base_asset.lower()}",
            "id": self.expected_fill_trade_id,
            "orderId": int(order.exchange_order_id),
            "amount": str(order.amount),
            "price": str(order.price),
            "fee": str(self.expected_fill_fee.flat_fees[0].amount),
            "total": str(order.amount * order.price),
            "timestamp": "2025-05-15T16:30:02.000000+03:30",
            "type": "buy",
        }

    def _order_cancelation_request_successful_mock_response(self, order: InFlightOrder) -> Any:
        return {"status": "ok", "updatedStatus": "Canceled"}

    def _order_status_request_completely_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "status": "ok",
            "order": {
                "id": int(order.exchange_order_id),
                "clientOrderId": order.client_order_id,
                "status": "Done",
                "created_at": "2025-05-15T16:30:01.879340+03:30",
                "matchedAmount": str(order.amount),
                "unmatchedAmount": "0",
            },
        }

    def _order_status_request_canceled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "status": "ok",
            "order": {
                "id": int(order.exchange_order_id),
                "clientOrderId": order.client_order_id,
                "status": "Canceled",
                "created_at": "2025-05-15T16:30:01.879340+03:30",
                "matchedAmount": "0",
                "unmatchedAmount": str(order.amount),
            },
        }

    def _order_status_request_open_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "status": "ok",
            "order": {
                "id": int(order.exchange_order_id),
                "clientOrderId": order.client_order_id,
                "status": "Active",
                "created_at": "2025-05-15T16:30:01.879340+03:30",
                "matchedAmount": "0",
                "unmatchedAmount": str(order.amount),
            },
        }

    def _order_status_request_partially_filled_mock_response(self, order: InFlightOrder) -> Any:
        return {
            "status": "ok",
            "order": {
                "id": int(order.exchange_order_id),
                "clientOrderId": order.client_order_id,
                "status": "Active",
                "created_at": "2025-05-15T16:30:01.879340+03:30",
                "matchedAmount": str(self.expected_partial_fill_amount),
                "unmatchedAmount": str(order.amount - self.expected_partial_fill_amount),
            },
        }

    def _order_fills_request_partial_fill_mock_response(self, order: InFlightOrder):
        return {
            "trades": [
                {
                    "id": self.expected_fill_trade_id,
                    "orderId": int(order.exchange_order_id),
                    "amount": str(self.expected_partial_fill_amount),
                    "price": str(self.expected_partial_fill_price),
                    "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                    "total": str(self.expected_partial_fill_amount * self.expected_partial_fill_price),
                    "timestamp": "2025-05-15T16:30:02.000000+03:30",
                    "type": order.trade_type.name.lower(),
                }
            ]
        }

    def _order_fills_request_full_fill_mock_response(self, order: InFlightOrder):
        return {
            "trades": [
                {
                    "id": self.expected_fill_trade_id,
                    "orderId": int(order.exchange_order_id),
                    "amount": str(order.amount),
                    "price": str(order.price),
                    "fee": str(self.expected_fill_fee.flat_fees[0].amount),
                    "total": str(order.amount * order.price),
                    "timestamp": "2025-05-15T16:30:02.000000+03:30",
                    "type": order.trade_type.name.lower(),
                }
            ]
        }
