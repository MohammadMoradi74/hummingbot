import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS, mofid_web_utils as web_utils
from hummingbot.connector.exchange.mofid.mofid_order_book import MofidOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.mofid.mofid_exchange import MofidExchange


class MofidAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: "MofidExchange",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._domain = domain
        self._api_factory = api_factory

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        isin = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        path_url = f"{CONSTANTS.SNAPSHOT_PATH_URL}/{isin}"
        rest_assistant = await self._api_factory.get_rest_assistant()
        return await rest_assistant.execute_request(
            url=web_utils.private_rest_url(path_url=path_url, domain=self._domain),
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
            is_auth_required=True,
        )

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        return MofidOrderBook.snapshot_message_from_exchange(
            snapshot,
            time.time(),
            metadata={"trading_pair": trading_pair},
        )

    # Slice 2: Lightstreamer bestlimit. Stubs satisfy abstract base for REST-only Slice 1.
    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        return False

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        return False
