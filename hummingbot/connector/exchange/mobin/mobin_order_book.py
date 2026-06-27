from datetime import datetime
from typing import Dict, Optional

from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class MobinOrderBook(OrderBook):

    @classmethod
    def snapshot_message_from_exchange(cls,
                                       msg: Dict[str, any],
                                       timestamp: float,
                                       metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a snapshot message with the order book snapshot message
        :param msg: the response from the exchange when requesting the order book snapshot
        :param timestamp: the snapshot timestamp
        :param metadata: a dictionary with extra information to add to the snapshot data
        :return: a snapshot message with the snapshot information received from the exchange
        """
        if metadata:
            msg.update(metadata)

        bids = []
        asks = []

        for level in msg.get("bestLimits", []):
            # Only add levels that have both price and quantity
            if "buyPrice" in level and "buyQuantity" in level:
                bids.append([float(level["buyPrice"]), float(level["buyQuantity"])])
            if "sellPrice" in level and "sellQuantity" in level:
                asks.append([float(level["sellPrice"]), float(level["sellQuantity"])])

        # Use timestamp as unique identifier, there's no unique identifier in the response
        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["instrumentId"],
            "update_id": int(timestamp),
            "bids": bids,
            "asks": asks
        }, timestamp=timestamp)

    @classmethod
    def diff_message_from_exchange(cls,
                                   msg: Dict[str, any],
                                   timestamp: Optional[float] = None,
                                   metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a diff message with the changes in the order book received from the exchange.
        msg is the decoded SignalR message containing BestLimits array.
        """
        if metadata:
            msg.update(metadata)

        bids = []
        asks = []

        # Extract order book levels from BestLimits
        best_limits = msg.get("BestLimits", [])
        for level in best_limits:
            # Add bid side (buy orders)
            if "BuyPrice" in level and "BuyQuantity" in level:
                buy_price = float(level["BuyPrice"])
                buy_quantity = float(level["BuyQuantity"])
                if buy_quantity > 0:  # Only add non-zero quantities
                    bids.append([buy_price, buy_quantity])

            # Add ask side (sell orders)
            if "SellPrice" in level and "SellQuantity" in level:
                sell_price = float(level["SellPrice"])
                sell_quantity = float(level["SellQuantity"])
                if sell_quantity > 0:  # Only add non-zero quantities
                    asks.append([sell_price, sell_quantity])

        # Use timestamp as update_id
        update_id = int(timestamp)

        return OrderBookMessage(OrderBookMessageType.DIFF, {
            "trading_pair": msg["trading_pair"],
            "first_update_id": update_id,  # SignalR doesn't provide first_update_id, use same as update_id
            "update_id": update_id,
            "bids": bids,
            "asks": asks
        }, timestamp=timestamp)

    @classmethod
    def trade_message_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None):
        """
        Creates a trade message with the information from the trade event sent by the exchange
        :param msg: the trade event details sent by the exchange
        :param metadata: a dictionary with extra information to add to trade message
        :return: a trade message with the details of the trade as provided by the exchange
        """
        if metadata:
            msg.update(metadata)

        # Parse the datetime string
        ts = (datetime.strptime(msg.get("TradeDateTime", ""), "%d %B %Y %H:%M:%S.%f")).timestamp() * 1000

        return OrderBookMessage(OrderBookMessageType.TRADE, {
            "trading_pair": msg["trading_pair"],
            "trade_type": TradeType.BUY.value,  # Mobin does not provide trade_type. Default to BUY, not ideal!
            "trade_id": str(msg.get("TradeNumber", 0)),
            "update_id": msg.get("TradeNumber", int(ts)),
            "price": float(msg.get("TradePrice", 0)),
            "amount": float(msg.get("TradedQuantity", 0))
        }, timestamp=ts * 1e-3)  # Convert milliseconds to seconds
