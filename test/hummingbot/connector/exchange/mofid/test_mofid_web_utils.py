import unittest

import hummingbot.connector.exchange.mofid.mofid_constants as CONSTANTS
from hummingbot.connector.exchange.mofid import mofid_web_utils as web_utils


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
        path_url = f"{CONSTANTS.SERVER_TIME_PATH_URL}/{client_ms}"
        expected = (
            "https://api-mts.orbis.easytrader.ir"
            "/easy/api/account/server-time/1788592336935"
        )
        self.assertEqual(expected, web_utils.public_rest_url(path_url))
