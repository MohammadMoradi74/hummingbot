import asyncio
import json
import urllib.parse
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source import MofidAPIUserStreamDataSource
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class MofidAPIUserStreamDataSourceUnitTests(IsolatedAsyncioWrapperTestCase):
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.trading_pair = "IRTKGOLD0001-IRR"
        cls.domain = "ir"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.log_records = []
        self.listening_task: Optional[asyncio.Task] = None
        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)
        self.auth = MofidAuth("testAPIKey", "testLsUser", TimeSynchronizer())
        api_factory = WebAssistantsFactory(
            throttler=web_utils.create_throttler(),
            auth=self.auth,
        )
        self.connector = MagicMock()
        self.connector._web_assistants_factory = api_factory
        self.data_source = MofidAPIUserStreamDataSource(
            auth=self.auth,
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=api_factory,
            domain=self.domain,
        )
        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)
        self.resume_test_event = asyncio.Event()

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message
                   for record in self.log_records)

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def _order_update_line(self) -> str:
        payload = {
            "isr": "1121DMbrkZ>zbVBa",
            "side": 1,
            "date": "20260905175452",
            "price": 20605,
            "quantity": 500,
            "symbolIsin": "IRTKGOLD0001",
            "type": "OrderResult",
            "state": "OnBoard",
        }
        return f"U,2,1,639242150|{json.dumps(payload, separators=(',', ':'))}\r\n"

    def _money_update_line(self) -> str:
        payload = {
            "customerIsin": "11293241406857",
            "block": "0",
            "T0": "11910",
            "T1": "6724",
            "T2": "6724",
            "buyPower": "6724",
        }
        return f"U,6,1,{json.dumps(payload, separators=(',', ':'))}\r\n"

    async def test_process_event_message_queues_order_update(self):
        self.data_source._sub_id_to_channel["2"] = CONSTANTS.LS_CHANNEL_ORDER
        queue = asyncio.Queue()
        await self.data_source._process_event_message(self._order_update_line(), queue)
        self.assertEqual(1, queue.qsize())
        msg = queue.get_nowait()
        self.assertEqual(CONSTANTS.USER_ORDER_EVENT_TYPE, msg["e"])
        self.assertEqual("OrderResult", msg["type"])
        self.assertEqual("OnBoard", msg["state"])
        self.assertEqual("1121DMbrkZ>zbVBa", msg["isr"])
        self.assertEqual("639242150", msg["ls_timestamp"])

    async def test_process_event_message_queues_money_update(self):
        self.data_source._sub_id_to_channel["6"] = CONSTANTS.LS_CHANNEL_MONEY
        queue = asyncio.Queue()
        await self.data_source._process_event_message(self._money_update_line(), queue)
        self.assertEqual(1, queue.qsize())
        msg = queue.get_nowait()
        self.assertEqual(CONSTANTS.USER_MONEY_EVENT_TYPE, msg["e"])
        self.assertEqual("6724", msg["buyPower"])

    async def test_process_event_message_does_not_queue_empty_payload(self):
        self.data_source._sub_id_to_channel["6"] = CONSTANTS.LS_CHANNEL_MONEY
        queue = asyncio.Queue()
        await self.data_source._process_event_message("", queue)
        await self.data_source._process_event_message("U,6,1,\r\n", queue)
        await self.data_source._process_event_message("PROBE\r\n", queue)
        self.assertEqual(0, queue.qsize())

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_listen_for_user_stream_get_session_successful_with_user_update_event(
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
            message="REQOK,1\r\nSUBOK,1,1,1\r\n",
        )
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message=self._order_update_line(),
        )

        msg_queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )
        msg = await msg_queue.get()
        self.assertEqual(CONSTANTS.USER_ORDER_EVENT_TYPE, msg["e"])
        self.assertEqual("OnBoard", msg["state"])

        sent_text = self.mocking_assistant.text_messages_sent_through_websocket(
            websocket_mock=ws_connect_mock.return_value
        )
        self.assertTrue(any(msg.startswith("bind_session") for msg in sent_text))
        decoded = [urllib.parse.unquote(m) for m in sent_text]
        self.assertTrue(any("LS_group=login" in m for m in decoded))
        self.assertTrue(any("LS_group=order" in m for m in decoded))
        self.assertTrue(any("LS_group=money" in m for m in decoded))
        self.assertTrue(
            self._is_logged(
                "INFO",
                "Subscribed to Mofid private Lightstreamer login/order/money channels...",
            )
        )

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_listen_for_user_stream_does_not_queue_empty_payload(
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
            message="",
        )
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message="U,2,1,\r\n",
        )

        msg_queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )
        await self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)
        self.assertEqual(0, msg_queue.qsize())
        self.listening_task.cancel()
        try:
            await self.listening_task
        except asyncio.CancelledError:
            pass

    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._sleep",
        new_callable=AsyncMock,
    )
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_listen_for_user_stream_connection_failed(
            self, create_session_mock, ws_connect_mock, sleep_mock
    ):
        create_session_mock.return_value = "SessABC"
        ws_connect_mock.side_effect = lambda *args, **kwargs: self._create_exception_and_unlock_test_with_event(
            Exception("TEST ERROR.")
        )
        sleep_mock.side_effect = lambda *_: self._create_exception_and_unlock_test_with_event(
            asyncio.CancelledError()
        )
        msg_queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )
        await self.resume_test_event.wait()
        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error while listening to user stream. Retrying after 5 seconds...",
            )
        )

    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._sleep",
        new_callable=AsyncMock,
    )
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_listen_for_user_stream_iter_message_throws_exception(
            self, create_session_mock, ws_connect_mock, sleep_mock
    ):
        create_session_mock.return_value = "SessABC"
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message="CONOK,SessABC,50000,5000,*\r\n",
        )
        # After bind CONOK is consumed, next receive fails while iterating.
        calls = {"n": 0}
        real_receive = ws_connect_mock.return_value.receive

        async def receive_side_effect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_receive(*args, **kwargs)
            return self._create_exception_and_unlock_test_with_event(Exception("TEST ERROR"))

        ws_connect_mock.return_value.receive.side_effect = receive_side_effect
        sleep_mock.side_effect = lambda *_: (_ for _ in ()).throw(asyncio.CancelledError())

        msg_queue = asyncio.Queue()
        self.listening_task = self.local_event_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )
        await self.resume_test_event.wait()
        with self.assertRaises(asyncio.CancelledError):
            await self.listening_task
        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error while listening to user stream. Retrying after 5 seconds...",
            )
        )

    @patch(
        "hummingbot.connector.exchange.mofid.mofid_api_user_stream_data_source"
        ".MofidAPIUserStreamDataSource._create_lightstreamer_session",
        new_callable=AsyncMock,
    )
    async def test_create_session_failure_raises(self, create_session_mock):
        create_session_mock.side_effect = IOError("Error creating Lightstreamer session. HTTP 500. Error: x")
        with self.assertRaises(IOError):
            await self.data_source._connected_websocket_assistant()
