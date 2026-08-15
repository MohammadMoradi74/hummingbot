import unittest

from hummingbot.connector.exchange.nobitex import nobitex_utils as utils


class NobitexUtilTestCases(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "IRT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"

    def test_is_exchange_information_valid(self):
        self.assertTrue(utils.is_exchange_information_valid("BTCIRT"))
        self.assertFalse(utils.is_exchange_information_valid(""))
        self.assertFalse(utils.is_exchange_information_valid(None))

    def test_exchange_symbol_helpers(self):
        self.assertEqual("BTCIRT", utils.exchange_symbol_for_tokens("BTC", "IRT"))
        self.assertEqual(("BTC", "IRT"), utils.split_exchange_symbol("BTCIRT"))
        self.assertEqual(("btc", "rls"), utils.currencies_from_trading_pair("BTC-IRT"))
        self.assertEqual("btc-rls", utils.stats_market_key("BTC-IRT"))
