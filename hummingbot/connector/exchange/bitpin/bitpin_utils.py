from decimal import Decimal
from typing import Any, Dict

from pydantic import Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap, ClientFieldData
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "USDT_IRT"

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0035"),
    taker_percent_fee_decimal=Decimal("0.003"),
    buy_percent_fee_deducted_from_returns=True
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information
    :param exchange_info: the exchange information for a trading pair
    :return: True if the trading pair is enabled, False otherwise
    """
    is_spot = True       # only spot market for now
    is_trading = False

    if exchange_info.get("tradable", None) is True:
        is_trading = True

    return is_trading and is_spot


class BitpinConfigMap(BaseConnectorConfigMap):
    connector: str = Field(default="bitpin", const=True, client_data=None)
    bitpin_api_key: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Bitpin API key",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )
    bitpin_api_secret: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Bitpin API secret",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )

    class Config:
        title = "bitpin"


KEYS = BitpinConfigMap.construct()

OTHER_DOMAINS = ["bitpin_org"]
OTHER_DOMAINS_PARAMETER = {"bitpin_org": "org"}
OTHER_DOMAINS_EXAMPLE_PAIR = {"bitpin_org": "BTC-USDT"}
OTHER_DOMAINS_DEFAULT_FEES = {"bitpin_org": DEFAULT_FEES}


class BitpinUSConfigMap(BaseConnectorConfigMap):
    connector: str = Field(default="bitpin_org", const=True, client_data=None)
    bitpin_api_key: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Bitpin US API key",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )
    bitpin_api_secret: SecretStr = Field(
        default=...,
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter your Bitpin US API secret",
            is_secure=True,
            is_connect_key=True,
            prompt_on_new=True,
        )
    )

    class Config:
        title = "bitpin_org"


OTHER_DOMAINS_KEYS = {"bitpin_org": BitpinUSConfigMap.construct()}
