from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "IRT3TVAF0001-IRR"

# Fallback only. HAR shows fees vary by instrument; equity IRO1* is typically
# buyCommission=0.003712, sellCommission=0.0088. Per-symbol fees should later
# come from trading rules / symbol payload.
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.003712"),
    taker_percent_fee_decimal=Decimal("0.0088"),
    buy_percent_fee_deducted_from_returns=True,
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information
    :param exchange_info: one symbol object from /symbols/api/symbols/all
    :return: True if the trading pair is enabled, False otherwise
    """
    return (
        exchange_info.get("isActive") is True
        and exchange_info.get("canBuy") is True
        and exchange_info.get("canSell") is True
    )


class MofidConfigMap(BaseConnectorConfigMap):
    connector: str = "mofid"
    # Placeholder until auth is reverse-engineered (likely session/cookie, not HMAC).
    mofid_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: "Enter your Mofid API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    mofid_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: "Enter your Mofid API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    model_config = ConfigDict(title="mofid")


KEYS = MofidConfigMap.model_construct()
