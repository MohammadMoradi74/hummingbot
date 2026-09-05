import unittest

from hummingbot.connector.exchange.mofid import mofid_utils as utils


class MofidUtilTestCases(unittest.TestCase):

    def test_is_exchange_information_valid(self):
        self.assertFalse(utils.is_exchange_information_valid({
            "isActive": False,
            "canBuy": True,
            "canSell": True,
        }))
        self.assertFalse(utils.is_exchange_information_valid({
            "isActive": True,
            "canBuy": False,
            "canSell": True,
        }))
        self.assertFalse(utils.is_exchange_information_valid({
            "isActive": True,
            "canBuy": True,
            "canSell": False,
        }))
        self.assertTrue(utils.is_exchange_information_valid({
            "isActive": True,
            "canBuy": True,
            "canSell": True,
        }))
