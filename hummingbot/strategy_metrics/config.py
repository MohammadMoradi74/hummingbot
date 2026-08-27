import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class AssetMark:
    """How to mark an asset in the reporting (quote) currency."""

    trading_pair: str
    exchange: str
    invert: bool = False


@dataclass
class MetricsConfig:
    enabled: bool = True
    strategy_name: str = "bitpin_mexc_mm"
    strategy_type: str = "cross_exchange_mm"
    config_file: str = ""
    quote_currency: str = "USDT"
    snapshot_interval_sec: int = 60

    maker_connector: str = "bitpin"
    hedge_connector: str = "mexc"
    maker_trading_pair: str = "BTC-IRT"
    hedge_trading_pair: str = "BTC-USDT"
    fx_trading_pair: Optional[str] = "USDT-IRT"

    database_url: str = field(default_factory=lambda: os.environ.get("MM_METRICS_DATABASE_URL", ""))

    @property
    def base_asset(self) -> str:
        return self.maker_trading_pair.split("-")[0]

    @property
    def maker_quote_asset(self) -> str:
        return self.maker_trading_pair.split("-")[1]

    def asset_marks(self) -> Dict[str, AssetMark]:
        marks = {
            self.quote_currency: AssetMark(trading_pair="", exchange="", invert=False),
            self.base_asset: AssetMark(
                trading_pair=self.hedge_trading_pair,
                exchange=self.hedge_connector,
            ),
        }
        if self.fx_trading_pair:
            fx_base, fx_quote = self.fx_trading_pair.split("-")
            if fx_quote != self.quote_currency:
                marks[fx_quote] = AssetMark(
                    trading_pair=self.fx_trading_pair,
                    exchange=self.maker_connector,
                    invert=True,
                )
            if fx_base != self.quote_currency and fx_base not in marks:
                marks[fx_base] = AssetMark(
                    trading_pair=self.fx_trading_pair,
                    exchange=self.maker_connector,
                    invert=False,
                )
        maker_quote = self.maker_quote_asset
        if maker_quote not in marks and maker_quote != self.quote_currency:
            if self.fx_trading_pair and maker_quote == self.fx_trading_pair.split("-")[0]:
                marks[maker_quote] = AssetMark(
                    trading_pair=self.fx_trading_pair,
                    exchange=self.maker_connector,
                    invert=False,
                )
        return marks

    def connectors(self) -> Tuple[str, ...]:
        return (self.maker_connector, self.hedge_connector)
