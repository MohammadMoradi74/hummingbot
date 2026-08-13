import json
import re
import unittest

from aioresponses import aioresponses

import hummingbot.connector.exchange.nobitex.nobitex_constants as CONSTANTS
from hummingbot.connector.exchange.nobitex import nobitex_web_utils as web_utils


class NobitexUtilTestCases(unittest.IsolatedAsyncioTestCase):

    def test_public_rest_url(self):
        path_url = "/TEST_PATH"
        domain = "ir"
        expected_url = CONSTANTS.REST_URL.format(domain) + CONSTANTS.PUBLIC_API_VERSION + path_url
        self.assertEqual(expected_url, web_utils.public_rest_url(path_url, domain))
        self.assertEqual("https://api.nobitex.ir/v3/TEST_PATH", web_utils.public_rest_url(path_url, domain))

    def test_private_rest_url(self):
        path_url = "/TEST_PATH"
        domain = "ir"
        expected_url = CONSTANTS.REST_URL.format(domain) + CONSTANTS.PRIVATE_API_VERSION + path_url
        self.assertEqual(expected_url, web_utils.private_rest_url(path_url, domain))

    @aioresponses()
    async def test_get_current_server_time(self, mock_api):
        url = web_utils.public_rest_url(path_url=CONSTANTS.SERVER_TIME_PATH_URL, domain="ir")
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, body=json.dumps({
            "status": "ok",
            "BTCIRT": {"lastUpdate": 1644991756704, "asks": [], "bids": []},
            "USDTIRT": {"lastUpdate": 1644991767392, "asks": [], "bids": []},
        }))

        server_time = await web_utils.get_current_server_time()

        self.assertEqual(1644991767392.0, server_time)
