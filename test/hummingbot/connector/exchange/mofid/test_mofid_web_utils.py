import json
import re
import unittest
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase

from aioresponses import aioresponses

import hummingbot.connector.exchange.mofid.mofid_constants as CONSTANTS
from hummingbot.connector.exchange.mofid import mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.time_synchronizer import TimeSynchronizer


class MofidWebUtilsTestCases(unittest.TestCase):

    def test_public_rest_url(self):
        path_url = "/TEST_PATH"
        domain = "ir"
        expected_url = CONSTANTS.REST_URL + CONSTANTS.PUBLIC_API_VERSION + path_url
        self.assertEqual(expected_url, web_utils.public_rest_url(path_url, domain))

    def test_private_rest_url(self):
        path_url = "/TEST_PATH"
        domain = "ir"
        expected_url = CONSTANTS.REST_URL + CONSTANTS.PRIVATE_API_VERSION + path_url
        self.assertEqual(expected_url, web_utils.private_rest_url(path_url, domain))

    def test_server_time_url_shape(self):
        client_ms = 1788592336935
        path_url = web_utils.server_time_path(client_ms)
        expected = (
            "https://api-mts.orbis.easytrader.ir"
            "/easy/api/account/server-time/1788592336935"
        )
        self.assertEqual(expected, web_utils.private_rest_url(path_url))

    def test_parse_server_timestamp_ms(self):
        # Live EasyTrader body (tmp3): diff = client skew hint; serverTimestamp = epoch ms.
        raw = {"diff": 1237, "serverTimestamp": 1789209510125}
        self.assertEqual(1789209510125.0, web_utils.parse_server_timestamp_ms(raw))

    def test_parse_server_timestamp_ms_missing_raises(self):
        with self.assertRaises(KeyError):
            web_utils.parse_server_timestamp_ms({"diff": 0})


class MofidServerTimeRequestTests(IsolatedAsyncioWrapperTestCase):

    @aioresponses()
    async def test_get_current_server_time_parses_server_timestamp(self, mock_api):
        auth = MofidAuth(api_key="testAPIKey", secret_key="testLsUser", time_provider=TimeSynchronizer())
        url_prefix = web_utils.private_rest_url(CONSTANTS.SERVER_TIME_PATH_URL)
        mock_api.get(
            re.compile(f"^{re.escape(url_prefix)}"),
            body=json.dumps({"diff": 100, "serverTimestamp": 1789209510125}),
        )
        ts = await web_utils.get_current_server_time(auth=auth)
        self.assertEqual(1789209510125.0, ts)
