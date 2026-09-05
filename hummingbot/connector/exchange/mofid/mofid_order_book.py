import time
from typing import Any, Dict, List, Optional, Tuple

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class MofidOrderBook(OrderBook):

    @staticmethod
    def _sheets_to_entries(sheets: List[Dict[str, Any]], depth: int) -> List[Tuple[str, str]]:
        entries: List[Tuple[str, str]] = []
        for row in sheets[:depth]:
            price = row.get("price")
            volume = row.get("volume")
            if price is None or volume is None:
                continue
            if float(price) <= 0 or float(volume) <= 0:
                continue
            entries.append((str(price), str(volume)))
        return entries

    @classmethod
    def snapshot_message_from_exchange(cls,
                                       msg: Dict[str, Any],
                                       timestamp: float,
                                       metadata: Optional[Dict] = None) -> OrderBookMessage:
        if metadata:
            msg.update(metadata)
        depth = CONSTANTS.ORDER_BOOK_DEPTH
        update_id = int(timestamp * 1e3)
        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["trading_pair"],
            "update_id": update_id,
            "bids": cls._sheets_to_entries(msg.get("buySheets", []), depth),
            "asks": cls._sheets_to_entries(msg.get("sellSheets", []), depth),
        }, timestamp=timestamp)

    @classmethod
    def diff_message_from_exchange(cls,
                                   msg: Dict[str, Any],
                                   timestamp: Optional[float] = None,
                                   metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Lightstreamer bestlimit updates replace the full top-N book (not incremental diffs).
        Emit SNAPSHOT so the tracker rebuilds levels 1..N cleanly.
        """
        if metadata:
            msg.update(metadata)
        update_id = int((timestamp or 0) * 1e3)
        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["trading_pair"],
            "update_id": update_id,
            "bids": msg.get("bids", []),
            "asks": msg.get("asks", []),
        }, timestamp=timestamp)

    @classmethod
    def trade_message_from_exchange(cls,
                                    msg: Dict[str, Any],
                                    metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Synthetic trade from symbol stream: last-trade-price + cum-volume delta.
        Side is unknown on the public feed — default BUY.
        """
        if metadata:
            msg.update(metadata)
        ts = float(msg.get("timestamp", time.time()))
        trade_id = int(msg.get("trade_id", ts * 1e3))
        return OrderBookMessage(OrderBookMessageType.TRADE, {
            "trading_pair": msg["trading_pair"],
            "trade_type": float(TradeType.BUY.value),
            "trade_id": trade_id,
            "update_id": trade_id,
            "price": msg["price"],
            "amount": msg["amount"],
        }, timestamp=ts)
