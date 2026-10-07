import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_EMPTY_INFO = pd.DataFrame({"instrument_code_12": []}, index=pd.Index([], dtype=object))


def _import_gold_etf_module():
    name = "gold_etf_statistical_arbitrage"
    if name in sys.modules:
        del sys.modules[name]
    with patch("pair_trade.historic_data.get_gold_etf_symbols", return_value=[]):
        with patch("pair_trade.historic_data.get_gold_etfs_info", return_value=_EMPTY_INFO):
            import gold_etf_statistical_arbitrage as ge

            return ge
    raise RuntimeError("unreachable")


ge = _import_gold_etf_module()
MOFID_MIN_NOTIONAL = ge.MOFID_MIN_NOTIONAL
mofid_sell_allowed = ge.mofid_sell_allowed
GoldEtfStatisticalArbitrage = ge.GoldEtfStatisticalArbitrage


class TestMofidMinNotional(unittest.TestCase):
    def test_floor_is_five_million_irr(self):
        self.assertEqual(MOFID_MIN_NOTIONAL, Decimal("5000000"))

    def test_deploy_floor_matches_min_notional(self):
        deploy_floor = max(Decimal("0"), MOFID_MIN_NOTIONAL)
        self.assertLess(Decimal("86055"), deploy_floor)
        self.assertGreaterEqual(Decimal("6000000"), deploy_floor)


class TestMofidSellAllowed(unittest.TestCase):
    lot = Decimal("1")

    def test_sub_min_line_rejects_partial_ganj_style(self):
        px = Decimal("223000")
        self.assertFalse(mofid_sell_allowed(Decimal("18"), px, Decimal("19"), MOFID_MIN_NOTIONAL, self.lot))
        self.assertTrue(mofid_sell_allowed(Decimal("19"), px, Decimal("19"), MOFID_MIN_NOTIONAL, self.lot))

    def test_min_line_requires_five_million_per_clip(self):
        px = Decimal("100000")
        self.assertFalse(mofid_sell_allowed(Decimal("40"), px, Decimal("80"), MOFID_MIN_NOTIONAL, self.lot))
        self.assertTrue(mofid_sell_allowed(Decimal("50"), px, Decimal("80"), MOFID_MIN_NOTIONAL, self.lot))

    def test_rejects_non_positive_inputs(self):
        px = Decimal("1000")
        self.assertFalse(mofid_sell_allowed(Decimal("0"), px, Decimal("10"), MOFID_MIN_NOTIONAL, self.lot))
        self.assertFalse(mofid_sell_allowed(Decimal("10"), Decimal("0"), Decimal("10"), MOFID_MIN_NOTIONAL, self.lot))
        self.assertFalse(mofid_sell_allowed(Decimal("10"), px, Decimal("0"), MOFID_MIN_NOTIONAL, self.lot))

    def test_sub_min_line_allows_quantization_slack_under_one_lot(self):
        px = Decimal("223000")
        self.assertTrue(
            mofid_sell_allowed(Decimal("18.999"), px, Decimal("19"), MOFID_MIN_NOTIONAL, Decimal("1"))
        )


class TestMeetsOrderConstraints(unittest.TestCase):
    def _strategy_with_connector(self, total_base: Decimal, lot_step: Decimal = Decimal("1")):
        strategy = GoldEtfStatisticalArbitrage.__new__(GoldEtfStatisticalArbitrage)
        rule = MagicMock(min_order_size=Decimal("1"), min_base_amount_increment=lot_step)
        connector = MagicMock()
        connector.trading_rules = {"IRTKGANJ0001-IRR": rule}
        connector.get_balance.return_value = total_base
        strategy.connectors = {"mofid": connector}
        strategy.config = MagicMock(exchange="mofid")
        return strategy

    def test_buy_requires_min_notional(self):
        strategy = self._strategy_with_connector(Decimal("100"))
        self.assertFalse(
            strategy.meets_order_constraints("IRTKGANJ0001-IRR", Decimal("10"), Decimal("100000"), side="buy")
        )
        self.assertTrue(
            strategy.meets_order_constraints("IRTKGANJ0001-IRR", Decimal("50"), Decimal("100000"), side="buy")
        )

    def test_sell_uses_total_balance_not_available(self):
        strategy = self._strategy_with_connector(Decimal("19"))
        px = Decimal("223000")
        self.assertFalse(strategy.meets_order_constraints("IRTKGANJ0001-IRR", Decimal("18"), px, side="sell"))
        self.assertTrue(strategy.meets_order_constraints("IRTKGANJ0001-IRR", Decimal("19"), px, side="sell"))


if __name__ == "__main__":
    unittest.main()
