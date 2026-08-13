import asyncio
import base64
import json
import re
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses.core import aioresponses
from bidict import bidict
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS, nobitex_web_utils as web_utils
from hummingbot.connector.exchange.nobitex.nobitex_api_order_book_data_source import NobitexAPIOrderBookDataSource
from hummingbot.connector.exchange.nobitex.nobitex_exchange import NobitexExchange
from hummingbot.connector.exchange.nobitex.nobitex_ws_utils import NobitexWSHelper
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage


class NobitexAPIOrderBookDataSourceUnitTests(IsolatedAsyncioWrapperTestCase):
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "USDT"
        cls.quote_asset = "IRT"
        cls.trading_pair = f"{cls.base_asset}{cls.quote_asset}"
        cls.ex_trading_pair = cls.base_asset + cls.quote_asset
        cls.domain = "ir"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        NobitexWSHelper.reset_message_id()
        self.log_records = []
        self.listening_task = None
        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)
        await self.mocking_assistant.async_init()

        private_key = ed25519.Ed25519PrivateKey.generate()
        seed_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_key_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        api_key = base64.urlsafe_b64encode(public_key_bytes).decode("utf-8")
        secret_key = base64.urlsafe_b64encode(seed_bytes).decode("utf-8")

        self.connector = NobitexExchange(
            nobitex_api_key=api_key,
            nobitex_api_secret=secret_key,
            trading_pairs=[],
            trading_required=False,
            domain=self.domain)
        self.data_source = NobitexAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
            domain=self.domain)
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

        self._original_full_order_book_reset_time = self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS
        self.data_source.FULL_ORDER_BOOK_RESET_DELTA_SECONDS = -1

        self.resume_test_event = asyncio.Event()
        self.connector._set_trading_pair_symbol_map(bidict({self.ex_trading_pair: self.trading_pair}))

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

    def _time_response(self):
        return {
            "status": "ok",
            "BTCIRT": {"lastUpdate": 1234567890, "asks": [], "bids": []},
        }

    def _trade_update_event(self):
        return {
            "_channel": f"{CONSTANTS.WS_TRADES_CHANNEL_PREFIX}{self.ex_trading_pair}",
            "price": "120000000000",
            "time": 1762781164192,
            "type": "sell",
            "volume": "0.000003",
        }

    def _order_diff_event(self):
        return {
            "_channel": f"{CONSTANTS.WS_ORDERBOOK_CHANNEL_PREFIX}{self.ex_trading_pair}",
            "asks": [["634100", "108.33"], ["634110", "1604.21"]],
            "bids": [["634090", "2748.24"], ["634080", "197.2"]],
            "lastTradePrice": "634090",
            "lastUpdate": 1729324655222,
        }

    def _snapshot_response(self):
        return {
            "status": "ok",
            "lastUpdate": 1729324655222,
            "lastTradePrice": "634090",
            "bids": [
                ["634090", "2748.24"], ["634080", "197.2"], ["634070", "577.37"],
                ["634010", "815.96"], ["634000", "475.83"], ["633610", "124.54"],
                ["633600", "3000"], ["633520", "150"], ["633510", "125"],
                ["633500", "3363.91"], ["633350", "367.11"], ["633300", "789.69"],
                ["633150", "236.61"], ["633130", "394.94"], ["633110", "2453.68"],
                ["633100", "2378.7"], ["633090", "85.06"], ["633080", "24134.98"],
                ["633060", "1600.02"], ["633050", "397.88"], ["633030", "6082.45"],
                ["633020", "496.08"], ["633010", "6384.02"], ["633000", "7476.99"],
            ],
            "asks": [
                ["634100", "108.33"], ["634110", "1604.21"], ["634120", "458.01"],
                ["634440", "50.16"], ["634450", "259.44"], ["634460", "946.71"],
                ["634500", "1780"], ["634580", "21.79"], ["634590", "1917.61"],
                ["634680", "100"], ["634690", "244.09"], ["634780", "733.42"],
                ["634790", "265.46"], ["634800", "226.1"], ["634810", "150"],
                ["634890", "20"], ["634900", "3039.36"], ["634930", "7888.39"],
                ["634960", "40"], ["634980", "7888.37"], ["634990", "225.98"],
                ["635000", "24401.54"], ["635010", "50"], ["635480", "79"],
            ],
        }

    @aioresponses()
    async def test_get_new_order_book_successful(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        time_url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL, domain=self.domain)
        mock_api.get(re.compile(f"^{time_url}"), body=json.dumps(self._time_response()))

        resp = self._snapshot_response()
        mock_api.get(regex_url, body=json.dumps(resp))

        order_book: OrderBook = await self.data_source.get_new_order_book(self.trading_pair)

        expected_update_id = resp["lastUpdate"]
        self.assertEqual(expected_update_id, order_book.snapshot_uid)
        bids = list(order_book.bid_entries())
        asks = list(order_book.ask_entries())
        self.assertEqual(24, len(bids))
        self.assertEqual(634090, bids[0].price)
        self.assertEqual(2748.24, bids[0].amount)
        self.assertEqual(expected_update_id, bids[0].update_id)
        self.assertEqual(24, len(asks))
        self.assertEqual(634100, asks[0].price)
        self.assertEqual(108.33, asks[0].amount)
        self.assertEqual(expected_update_id, asks[0].update_id)

    @aioresponses()
    async def test_get_new_order_book_raises_exception(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        time_url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL, domain=self.domain)
        mock_api.get(re.compile(f"^{time_url}"), body=json.dumps(self._time_response()))

        mock_api.get(regex_url, status=400)
        with self.assertRaises(IOError):
            await self.data_source.get_new_order_book(self.trading_pair)

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_subscriptions_subscribes_to_trades_and_order_diffs(self, ws_connect_mock):
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()

        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message=json.dumps({"id": 1, "connect": {}}),
        )

        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())
        await self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)

        sent_subscription_messages = self.mocking_assistant.json_messages_sent_through_websocket(
            websocket_mock=ws_connect_mock.return_value)

        self.assertEqual(3, len(sent_subscription_messages))
        self.assertEqual({"id": 1, "connect": {}}, sent_subscription_messages[0])
        self.assertEqual(
            {"id": 2, "subscribe": {"channel": f"public:orderbook-{self.ex_trading_pair}"}},
            sent_subscription_messages[1],
        )
        self.assertEqual(
            {"id": 3, "subscribe": {"channel": f"public:trades-{self.ex_trading_pair}"}},
            sent_subscription_messages[2],
        )
        self.assertTrue(self._is_logged(
            "INFO",
            "Subscribed to public order book and trade channels..."
        ))

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch("aiohttp.ClientSession.ws_connect")
    async def test_listen_for_subscriptions_raises_cancel_exception(self, mock_ws, _: AsyncMock):
        mock_ws.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_subscriptions()

    @patch("hummingbot.core.data_type.order_book_tracker_data_source.OrderBookTrackerDataSource._sleep")
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_subscriptions_logs_exception_details(self, mock_ws, sleep_mock):
        mock_ws.side_effect = Exception("TEST ERROR.")
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())

        self.listening_task = self.local_event_loop.create_task(self.data_source.listen_for_subscriptions())
        await self.resume_test_event.wait()

        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error occurred when listening to order book streams. Retrying in 5 seconds..."))

    async def test_subscribe_channels_raises_cancel_exception(self):
        mock_ws = MagicMock()
        mock_ws.send.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source._subscribe_channels(mock_ws)

    async def test_subscribe_channels_raises_exception_and_logs_error(self):
        mock_ws = MagicMock()
        mock_ws.send.side_effect = Exception("Test Error")

        with self.assertRaises(Exception):
            await self.data_source._subscribe_channels(mock_ws)

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error occurred subscribing to order book trading and delta streams...")
        )

    async def test_listen_for_trades_cancelled_when_listening(self):
        mock_queue = MagicMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_trades(self.local_event_loop, msg_queue)

    async def test_listen_for_trades_logs_exception(self):
        incomplete_resp = {"m": 1, "i": 2}
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [incomplete_resp, asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        try:
            await self.data_source.listen_for_trades(self.local_event_loop, msg_queue)
        except asyncio.CancelledError:
            pass

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public trade updates from exchange"))

    async def test_listen_for_trades_successful(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [self._trade_update_event(), asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.TRADE_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_trades(self.local_event_loop, msg_queue))
        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(1762781164192, msg.trade_id)

    async def test_listen_for_order_book_diffs_cancelled(self):
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = asyncio.CancelledError()
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue)

    async def test_listen_for_order_book_diffs_logs_exception(self):
        incomplete_resp = {"m": 1, "i": 2}
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = [incomplete_resp, asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        try:
            await self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue)
        except asyncio.CancelledError:
            pass

        self.assertTrue(
            self._is_logged("ERROR", "Unexpected error when processing public order book updates from exchange"))

    async def test_listen_for_order_book_diffs_successful(self):
        mock_queue = AsyncMock()
        diff_event = self._order_diff_event()
        mock_queue.get.side_effect = [diff_event, asyncio.CancelledError()]
        self.data_source._message_queue[CONSTANTS.DIFF_EVENT_TYPE] = mock_queue
        msg_queue: asyncio.Queue = asyncio.Queue()

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_diffs(self.local_event_loop, msg_queue))
        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(diff_event["lastUpdate"], msg.update_id)
        self.assertEqual(2, len(msg.asks))
        self.assertEqual(2, len(msg.bids))

    @aioresponses()
    async def test_listen_for_order_book_snapshots_cancelled_when_fetching_snapshot(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, exception=asyncio.CancelledError, repeat=True)

        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_order_book_snapshots(self.local_event_loop, asyncio.Queue())

    @aioresponses()
    @patch("hummingbot.connector.exchange.nobitex.nobitex_api_order_book_data_source"
           ".NobitexAPIOrderBookDataSource._sleep")
    async def test_listen_for_order_book_snapshots_log_exception(self, mock_api, sleep_mock):
        msg_queue: asyncio.Queue = asyncio.Queue()
        sleep_mock.side_effect = lambda _: self._create_exception_and_unlock_test_with_event(asyncio.CancelledError())

        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, exception=Exception, repeat=True)

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )
        await self.resume_test_event.wait()

        self.assertTrue(
            self._is_logged("ERROR", f"Unexpected error fetching order book snapshot for {self.trading_pair}."))

    @aioresponses()
    async def test_listen_for_order_book_snapshots_successful(self, mock_api):
        msg_queue: asyncio.Queue = asyncio.Queue()
        url = web_utils.public_rest_url(path_url=CONSTANTS.SNAPSHOT_PATH_URL, domain=self.domain)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        resp = self._snapshot_response()
        time_url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL, domain=self.domain)
        mock_api.get(re.compile(f"^{time_url}"), body=json.dumps(self._time_response()))
        mock_api.get(regex_url, body=json.dumps(resp), repeat=True)

        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_order_book_snapshots(self.local_event_loop, msg_queue)
        )
        msg: OrderBookMessage = await msg_queue.get()

        self.assertEqual(resp["lastUpdate"], msg.update_id)
        self.assertEqual(24, len(msg.asks))
