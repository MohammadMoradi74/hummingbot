import json
import re
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from aioresponses.core import aioresponses

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_api_order_book_data_source import MofidAPIOrderBookDataSource
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class MofidAPIOrderBookDataSourceUnitTests(IsolatedAsyncioWrapperTestCase):

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
        api_factory = WebAssistantsFactory(
            throttler=web_utils.create_throttler(),
            auth=MofidAuth("", "", TimeSynchronizer()),
        )
        self.connector = MagicMock()
        self.connector._web_assistants_factory = api_factory
        self.connector.exchange_symbol_associated_to_pair = AsyncMock(return_value=self.ex_trading_pair)
        self.data_source = MofidAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=api_factory,
            domain=self.domain,
        )

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
