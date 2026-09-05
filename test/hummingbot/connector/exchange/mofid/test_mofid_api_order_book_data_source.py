import asyncio
import json
import re
import urllib.parse
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses.core import aioresponses

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source import MofidAPIOrderBookDataSource
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.exchange.mofid.mofid_lightstreamer import (
    bestlimit_state_to_bids_asks,
    expand_tlcp_fields,
    merge_bestlimit_fields,
)
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class MofidAPIOrderBookDataSourceUnitTests(IsolatedAsyncioWrapperTestCase):
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "IRTKJAVA0001"
        cls.quote_asset = "IRR"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = cls.base_asset
        cls.domain = "ir"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.log_records = []
        self.listening_task = None
        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)
        api_factory = WebAssistantsFactory(
            throttler=web_utils.create_throttler(),
            auth=MofidAuth("", "", TimeSynchronizer()),
        )
        self.connector = MagicMock()
        self.connector._web_assistants_factory = api_factory
        self.connector.exchange_symbol_associated_to_pair = AsyncMock(return_value=self.ex_trading_pair)
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value=self.trading_pair)
        self.data_source = MofidAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=api_factory,
            domain=self.domain,
        )
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)
        self._original_full_order_book_reset_time = self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = -1
        self.resume_test_event = asyncio.Event()

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = self._original_full_order_book_reset_time
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message
                   for record in self.log_records)

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def _snapshot_response(self):
        return {
            "isin": "IRTKJAVA0001",
            "buySheets": [
                {"price": 85400, "amount": 5, "volume": 10027, "marketMaker": 0},
                {"price": 85390, "amount": 2, "volume": 16671, "marketMaker": 0},
                {"price": 85387, "amount": 1, "volume": 17337, "marketMaker": 0},
                {"price": 85386, "amount": 2, "volume": 1700, "marketMaker": 0},
                {"price": 85385, "amount": 1, "volume": 9979, "marketMaker": 0},
                {"price": 85383, "amount": 1, "volume": 100, "marketMaker": 0},
            ],
            "sellSheets": [
                {"price": 85401, "amount": 3, "volume": 5550, "marketMaker": 0},
                {"price": 85507, "amount": 1, "volume": 870, "marketMaker": 0},
                {"price": 85508, "amount": 22, "volume": 2200, "marketMaker": 0},
                {"price": 85509, "amount": 1, "volume": 1000, "marketMaker": 0},
                {"price": 85510, "amount": 7, "volume": 50988, "marketMaker": 0},
                {"price": 85520, "amount": 1, "volume": 10, "marketMaker": 0},
            ],
        }

    def _bestlimit_diff_event(self):
        return {
            "e": CONSTANTS.DIFF_EVENT_TYPE,
            "isin": self.ex_trading_pair,
            "bids": [["85400", "10027"], ["85390", "16671"]],
            "asks": [["85401", "5550"], ["85507", "870"]],
        }

    @aioresponses()
    async def test_get_new_order_book_successful(self, mock_api):
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{self.ex_trading_pair}"
        url = web_utils.private_rest_url(path_url=path_url, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps(self._snapshot_response()))

        order_book: OrderBook = await self.data_source.get_new_order_book(self.trading_pair)
        bids = list(order_book.bid_entries())
        asks = list(order_book.ask_entries())

        self.assertEqual(5, len(bids))
        self.assertEqual(85400, bids[0].price)
        self.assertEqual(10027, bids[0].amount)
        self.assertEqual(5, len(asks))
        self.assertEqual(85401, asks[0].price)
        self.assertEqual(5550, asks[0].amount)

    @aioresponses()
    async def test_get_new_order_book_raises_exception(self, mock_api):
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{self.ex_trading_pair}"
        url = web_utils.private_rest_url(path_url=path_url, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=400)
        with self.assertRaises(IOError):
            await self.data_source.get_new_order_book(self.trading_pair)

    def test_expand_and_merge_bestlimit_fields(self):
        expanded = expand_tlcp_fields(["a", "^2", "b"])
        self.assertEqual(["a", "", "", "b"], expanded)

        merged = merge_bestlimit_fields(
            {"timestamp": "1", "buy-price-1": "100", "buy-volume-1": "10"},
            ["", "20", "", "", "", "101", ""],
        )
        self.assertEqual("1", merged["timestamp"])
        self.assertEqual("20", merged["buy-volume-1"])
        self.assertEqual("101", merged["buy-price-1"])
        bids, asks = bestlimit_state_to_bids_asks({
            "buy-price-1": "101", "buy-volume-1": "20",
            "sell-price-1": "102", "sell-volume-1": "5",
        })
        self.assertEqual([["101", "20"]], bids)
        self.assertEqual([["102", "5"]], asks)

    async def test_subscribe_channels_sends_bestlimit_control(self):
        mock_ws = AsyncMock()
        self.data_source._ls_session_id = "Sess123"
        await self.data_source._subscribe_channels(mock_ws)
        self.assertEqual(2, mock_ws.send.call_count)
        payloads = [call.args[0].payload for call in mock_ws.send.call_args_list]
        decoded = "\n".join(urllib.parse.unquote(p) for p in payloads)
        self.assertIn(f"bestlimit:{self.ex_trading_pair}", decoded)
        self.assertIn("BESTLIMIT_ADAPTER", decoded)
        self.assertIn(f"symbol:{self.ex_trading_pair}", decoded)
        self.assertIn("RLC_ADAPTER", decoded)
        self.assertTrue(
            self._is_logged("INFO", "Subscribed to Mofid Lightstreamer bestlimit and symbol channels...")
        )

    async def test_subscribe_channels_raises_cancel_exception(self):
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source._subscribe_channels(mock_ws)

    async def test_subscribe_channels_raises_exception_and_logs_error(self):
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = Exception("Test Error")
        with self.assertRaises(Exception):
            await self.data_source._subscribe_channels(mock_ws)
        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error occurred subscribing to order book trading and delta streams...",
            )
        )

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source"
        ".MofidAPIOrderBookDataSource._connected_websocket_assistant",
        new_callable=AsyncMock,
    )
    async def test_listen_for_subscriptions_raises_cancel_exception(self, mock_connected, _):
        mock_connected.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_subscriptions()

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source"
        ".MofidAPIOrderBookDataSource._connected_websocket_assistant",
        new_callable=AsyncMock,
    )
    async def test_listen_for_subscriptions_logs_exception_details(self, mock_connected, sleep_mock):
        mock_connected.side_effect = Exception("TEST ERROR.")
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())
        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())
        await self.resume_test_event.wait()
        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error occurred when listening to order book streams. Retrying in 5 seconds...",
            )
        )

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source"
        ".MofidAPIOrderBookDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_listen_for_subscriptions_subscribes_to_bestlimit(
            self, create_session_mock, ws_connect_mock
    ):
        create_session_mock.return_value = "SessABC"
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message="CONOK,SessABC,50000,5000,*\r\n",
        )
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message="REQOK,1\r\nSUBOK,1,1,31\r\n",
        )

        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())
        await self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)

        sent_text = self.mocking_assistant.text_messages_sent_through_websocket(
            websocket_mock=ws_connect_mock.return_value
        )
        self.assertTrue(any(msg.startswith("bind_session") for msg in sent_text))
        self.assertTrue(any("bestlimit:" in urllib.parse.unquote(msg) for msg in sent_text))
        self.assertTrue(any("symbol:" in urllib.parse.unquote(msg) for msg in sent_text))
        self.assertTrue(
            self._is_logged("INFO", "Subscribed to Mofid Lightstreamer bestlimit and symbol channels...")
        )

    async def test_listen_for_order_book_diffs_cancelled(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, asyncio.Queue())

    async def test_listen_for_order_book_diffs_logs_exception(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._bestlimit_diff_event(), asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(
            side_effect=Exception("parse fail")
        )
        try:
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, asyncio.Queue())
        except asyncio.CancelledError:
            pass
        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public order book updates from exchange")
        )

    async def test_listen_for_order_book_diffs_successful(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._bestlimit_diff_event(), asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue)
        )
        msg: OrderBookMessage = await msg_queue.get()
        self.assertEqual(self.trading_pair, msg.trading_pair)
        self.assertEqual(85400, msg.bids[0].price)
        self.assertEqual(10027, msg.bids[0].amount)
        self.assertEqual(85401, msg.asks[0].price)

    async def test_listen_for_trades_cancelled_when_listening(self):
        mock_queue = MagicMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_trades(self.local_event_loop, asyncio.Queue())

    def _trade_event(self):
        return {
            "e": CONSTANTS.TRADE_EVENT_TYPE,
            "isin": self.ex_trading_pair,
            "price": "85400",
            "amount": "150",
            "timestamp": 1700000000.0,
            "trade_id": 1700000000000,
        }

    async def test_listen_for_trades_successful(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._trade_event(), asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_trades(self.local_event_loop, msg_queue)
        )
        msg: OrderBookMessage = await msg_queue.get()
        self.assertEqual(self.trading_pair, msg.trading_pair)
        self.assertEqual(1700000000000, msg.trade_id)
        self.assertEqual(85400.0, float(msg.content["price"]))
        self.assertEqual(150.0, float(msg.content["amount"]))

    async def test_listen_for_trades_logs_exception(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._trade_event(), asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(
            side_effect=Exception("parse fail")
        )
        try:
            await self.data_source.listen_for_trades(self.local_event_loop, asyncio.Queue())
        except asyncio.CancelledError:
            pass
        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public trade updates from exchange")
        )

    async def test_symbol_snapshot_does_not_emit_trade(self):
        isin = self.ex_trading_pair
        self.data_source._symbol_state[isin] = {}
        await self.data_source._handle_symbol_update(isin, ["85400", "1000000"])
        self.assertEqual(0, self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE].qsize())
        self.assertEqual(85400.0, self.data_source._last_traded_prices[self.trading_pair])
        self.assertEqual("1000000", self.data_source._symbol_state[isin]["total-number-of-shares-traded"])

    async def test_symbol_volume_delta_emits_trade(self):
        isin = self.ex_trading_pair
        self.data_source._symbol_state[isin] = {
            "last-trade-price": "85400",
            "total-number-of-shares-traded": "1000000",
        }
        await self.data_source._handle_symbol_update(isin, ["85500", "1000150"])
        self.assertEqual(1, self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE].qsize())
        event = self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE].get_nowait()
        self.assertEqual("85500", event["price"])
        self.assertEqual("150", event["amount"])
        self.assertEqual(85500.0, self.data_source._last_traded_prices[self.trading_pair])

    async def test_get_last_traded_prices_from_cache(self):
        self.data_source._last_traded_prices[self.trading_pair] = 85400.0
        prices = await self.data_source.get_last_traded_prices([self.trading_pair, "OTHER-IRR"])
        self.assertEqual({self.trading_pair: 85400.0}, prices)

    @aioresponses()
    async def test_listen_for_order_book_snapshots_cancelled_when_fetching_snapshot(self, mock_api):
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{self.ex_trading_pair}"
        url = web_utils.private_rest_url(path_url=path_url, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, exception=asyncio.CancelledError, repeat=True)
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_snapshots(self.local_event_loop, asyncio.Queue())

    @aioresponses()
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source"
        ".MofidAPIOrderBookDataSource._sleep"
    )
    async def test_listen_for_order_book_snapshots_log_exception(self, mock_api, sleep_mock):
        msg_queue: asyncio.Queue = asyncio.Queue()
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{self.ex_trading_pair}"
        url = web_utils.private_rest_url(path_url=path_url, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, exception=Exception, repeat=True)
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )
        await self.resume_test_event.wait()
        self.assertTrue(
            self._is_logged("ERROR", f"Unexpected error fetching order book snapshot for {self.trading_pair}.")
        )

    @aioresponses()
    async def test_listen_for_order_book_snapshots_successful(self, mock_api):
        msg_queue: asyncio.Queue = asyncio.Queue()
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{self.ex_trading_pair}"
        url = web_utils.private_rest_url(path_url=path_url, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps(self._snapshot_response()))
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )
        msg: OrderBookMessage = await msg_queue.get()
        self.assertEqual(self.trading_pair, msg.trading_pair)
        self.assertEqual(85400, msg.bids[0].price)
