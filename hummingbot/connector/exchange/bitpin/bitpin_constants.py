from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

#
DEFAULT_DOMAIN = "ir"
#
HBOT_ORDER_ID_PREFIX = "bitpin"
MAX_ORDER_ID_LEN = 32
#
# # Base URL
REST_URL = "https://api.bitpin.{}/api/"

# WEBSOCKET
WSS_URL = "wss://centrifugo.bitpin.{}/connection/websocket"
WS_DOMAIN = "ir"  # docs use bitpin.ir; REST may use self._domain separately
WS_INFO_PATH_URL = "/usr/ws-info/"
WS_ORDERBOOK_CHANNEL_PREFIX = "orderbook"
WS_MATCHES_CHANNEL_PREFIX = "matches"
WS_MARKET_CHANNEL_PREFIX = "market"
WS_USER_ORDER_CHANNEL = "user:order_info#"
# event types unchanged
DIFF_EVENT_TYPE = "market_data"
TRADE_EVENT_TYPE = "matches_update"

#
PUBLIC_API_VERSION = "v1"
PRIVATE_API_VERSION = "v1"
#
# # Public API endpoints or BinanceClient function
TICKER_PRICE_CHANGE_PATH_URL = "/mkt/tickers/"
# TICKER_BOOK_PATH_URL = "/ticker/bookTicker"
# PRICES_PATH_URL = "/ticker/price"
EXCHANGE_INFO_PATH_URL = "/mkt/markets/"
PING_PATH_URL = "/mkt/markets/"           # not working! There's no ping ulr for bitpin. Just keeps other parts working
SNAPSHOT_PATH_URL = "/mth/orderbook/"
SERVER_TIME_PATH_URL = "/mkt/tickers/"
#
# # Private API endpoints or BinanceClient function
ACCOUNTS_PATH_URL = "/wlt/wallets/"
MY_TRADES_PATH_URL = "/odr/fills/"
ORDER_PATH_URL = "/odr/orders/"
BITPIN_USER_STREAM_PATH_URL = "/usr/authenticate/"
BITPIN_USER_STREAM_PATH_URL2 = '/usr/refresh_token/'
#
WS_HEARTBEAT_TIME_INTERVAL = 15
#
# # Binance params
#
SIDE_BUY = "buy"
SIDE_SELL = "sell"
#
# TIME_IN_FORCE_GTC = "GTC"  # Good till cancelled
# TIME_IN_FORCE_IOC = "IOC"  # Immediate or cancel
# TIME_IN_FORCE_FOK = "FOK"  # Fill or kill
#
# Rate Limit Type
REQUEST_WEIGHT = "REQUEST_WEIGHT"
ORDERS = "ORDERS"
ORDERS_24HR = "ORDERS_24HR"
RAW_REQUESTS = "RAW_REQUESTS"
#
# Rate Limit time intervals
ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400
#
MAX_REQUEST = 5000
#

# Order States
# They are driven implicitly from order response. https://api.bitpin.ir/api/v1/odr/orders/1102450298/
# See: BitpinExchange._find_state_from_order_data() function in bitpin_exchange.py
ORDER_STATE = {
    "PENDING_CREATE": OrderState.PENDING_CREATE,
    "PENDING_CANCEL": OrderState.PENDING_CANCEL,
    "OPEN": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "FAILED": OrderState.FAILED,
}
# # Websocket event types
DIFF_EVENT_TYPE = "market_data"
TRADE_EVENT_TYPE = "matches_update"
#
RATE_LIMITS = [
    # Pools
    # RateLimit(limit_id=REQUEST_WEIGHT, limit=6000, time_interval=ONE_MINUTE),
    # RateLimit(limit_id=ORDERS, limit=100, time_interval=10 * ONE_SECOND),
    # RateLimit(limit_id=ORDERS_24HR, limit=200000, time_interval=ONE_DAY),
    # RateLimit(limit_id=RAW_REQUESTS, limit=61000, time_interval=5 * ONE_MINUTE),
    # Weighted Limits
    # RateLimit(limit_id=TICKER_PRICE_CHANGE_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
    #           linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 2),
    #                          LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    # RateLimit(limit_id=TICKER_BOOK_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
    #           linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
    #                          LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    # RateLimit(limit_id=PRICES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
    #           linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
    #                          LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=EXCHANGE_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 100),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=BITPIN_USER_STREAM_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 2),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    # RateLimit(limit_id=PING_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
    #           linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
    #                          LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ACCOUNTS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MY_TRADES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDER_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(ORDERS_24HR, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)])
]
#
# ORDER_NOT_EXIST_ERROR_CODE = -2013
# ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = 406
UNKNOWN_ORDER_MESSAGE = "not allowed"
