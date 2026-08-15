from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-IRT"

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0025"),
    taker_percent_fee_decimal=Decimal("0.0025"),
    buy_percent_fee_deducted_from_returns=True,
)


def hb_quote_to_api(quote: str) -> str:
    """HB quote (IRT/USDT) → Nobitex API currency (rls/usdt)."""
    return CONSTANTS.QUOTE_ASSET_MAP.get(quote.upper(), quote.lower())


def api_quote_to_hb(quote: str) -> str:
    """Nobitex API currency → HB quote asset."""
    return CONSTANTS.QUOTE_ASSET_MAP_REVERSE.get(quote.lower(), quote.upper())


def exchange_symbol_for_tokens(base: str, quote: str) -> str:
    """HB tokens → orderbook/options symbol, e.g. BTC + IRT → BTCIRT."""
    api_quote = hb_quote_to_api(quote).upper()
    if api_quote == "RLS":
        api_quote = "IRT"  # options/orderbook keys use IRT suffix
    return f"{base.upper()}{api_quote}"


def split_exchange_symbol(symbol: str) -> Tuple[str, str]:
    """BTCIRT / BTCUSDT → (BTC, IRT) HB-style quote."""
    symbol = symbol.upper()
    for quote in ("USDT", "IRT"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)], quote
    raise ValueError(f"Unrecognized Nobitex symbol: {symbol}")


def currencies_from_trading_pair(trading_pair: str) -> Tuple[str, str]:
    """BTC-IRT → (btc, rls) for order API src/dst."""
    base, quote = trading_pair.split("-")
    return base.lower(), hb_quote_to_api(quote)


def stats_market_key(trading_pair: str) -> str:
    """BTC-IRT → btc-rls for /market/stats."""
    src, dst = currencies_from_trading_pair(trading_pair)
    return f"{src}-{dst}"


def is_exchange_information_valid(symbol: str, exchange_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Symbol keys in amountPrecisions (e.g. BTCIRT) are tradable markets.
    """
    if not symbol or not isinstance(symbol, str):
        return False
    symbol = symbol.upper()
    return symbol.endswith("IRT") or symbol.endswith("USDT")


class NobitexConfigMap(BaseConnectorConfigMap):
    connector: str = "nobitex"
    nobitex_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: "Enter your Nobitex API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    nobitex_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: "Enter your Nobitex API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    model_config = ConfigDict(title="nobitex")


KEYS = NobitexConfigMap.model_construct()
