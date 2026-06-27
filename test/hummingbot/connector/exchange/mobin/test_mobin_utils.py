import unittest

from hummingbot.connector.exchange.mobin import mobin_utils as utils


class MobinUtilTestCases(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "IRTKMOFD0001"
        cls.quote_asset = "IRR"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.hb_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        # TODO: Find the ex_trading_pair, guess it is IRTKMOFD0001 as there is only one quote asset
        cls.ex_trading_pair = f"{cls.base_asset}{cls.quote_asset}"

    # TODO: For now ignoring the test. This is the api:
    #  https://qcore.mobinsb.ir/v1/Instruments/Information?id=IRTKMOFD0001

    def test_is_exchange_information_valid(self):
        invalid_info_1 = {
            "status": "BREAK",
            "permissionSets": [["MARGIN"]],
        }

        self.assertFalse(utils.is_exchange_information_valid(invalid_info_1))

        invalid_info_2 = {
            "status": "BREAK",
            "permissionSets": [["SPOT"]],
        }

        self.assertFalse(utils.is_exchange_information_valid(invalid_info_2))

        invalid_info_3 = {
            "status": "TRADING",
            "permissionSets": [["MARGIN"]],
        }

        self.assertFalse(utils.is_exchange_information_valid(invalid_info_3))

        invalid_info_4 = {
            "status": "TRADING",
            "permissionSets": [["SPOT"]],
        }

        self.assertTrue(utils.is_exchange_information_valid(invalid_info_4))
