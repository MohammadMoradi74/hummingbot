import os
from dataclasses import dataclass, field
from typing import Dict, List

from hummingbot.strategy_metrics.config import AssetMark


@dataclass
class PairTradeMetricsConfig:
    enabled: bool = True
    strategy_name: str = "aras_pair_trade_partial_mock_2"
    strategy_type: str = "pair_trade_basket"
    config_file: str = ""
    quote_currency: str = "IRR"
    snapshot_interval_sec: int = 60
    signal_enabled: bool = True

    exchange: str = "mock"
    trading_pairs: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    hb_to_strat_map: Dict[str, str] = field(default_factory=dict)

    n_slots: int = 3
    database_url: str = field(default_factory=lambda: os.environ.get("MM_METRICS_DATABASE_URL", ""))

    def asset_marks(self) -> Dict[str, AssetMark]:
        marks: Dict[str, AssetMark] = {
            self.quote_currency: AssetMark(trading_pair="", exchange="", invert=False),
        }
        for pair in self.trading_pairs:
            base, quote = pair.split("-")
            marks[base] = AssetMark(trading_pair=pair, exchange=self.exchange)
            if quote not in marks and quote != self.quote_currency:
                marks[quote] = AssetMark(trading_pair="", exchange="", invert=False)
        return marks

    def tracked_assets(self) -> set[str]:
        assets = {self.quote_currency}
        for pair in self.trading_pairs:
            base, quote = pair.split("-")
            assets.add(base)
            assets.add(quote)
        return assets
