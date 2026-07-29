from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = "ir"

HBOT_ORDER_ID_PREFIX = "mobinsb"
MAX_ORDER_ID_LEN = 32

# Base URL
REST_URL = "https://qcore.mobinsb.{}/"
WSS_URL = "wss://pusher9.mobinsb.{}/mmtp"

PUBLIC_API_VERSION = "v1"
PRIVATE_API_VERSION = "v1"

# Public API endpoints or BinanceClient function
TICKER_PRICE_CHANGE_PATH_URL = "/Instruments/Information"
TICKER_BOOK_PATH_URL = "/ticker/bookTicker"
PRICES_PATH_URL = "/ticker/price"
EXCHANGE_INFO_PATH_URL = "/Instruments/GetInstrumentsByTypeGroup?instrumentTypeGroup=5"
PING_PATH_URL = "/Index/GetMarketActivities"
SNAPSHOT_PATH_URL = "/Instruments/Information"
SERVER_TIME_PATH_URL = "/Instruments/InformationMinimal?id=IRTKLOTF0001"

# Private API endpoints or BinanceClient function
ACCOUNTS_PATH_URL = "/Accounts/Get"
PORTFOLIO_PATH_URL = "/Portfolios/Get"
MY_TRADES_PATH_URL = "/myTrades"
MY_ORDERS_PATH_URL = "/Orders/Today"
ORDER_PATH_URL = "/Requests/SaveRequest"
CANCEL_ORDER_PATH_URL = "/Requests/CancelRequest"
MOBIN_STREAM_PATH_URL = "https://pusher9.mobinsb.ir/mmtp/negotiate?negotiateVersion=1"

WS_HEARTBEAT_TIME_INTERVAL = 30

# Binance params

SIDE_BUY = 1
SIDE_SELL = 2

TIME_IN_FORCE_GTC = "GTC"  # Good till cancelled
TIME_IN_FORCE_IOC = "IOC"  # Immediate or cancel
TIME_IN_FORCE_FOK = "FOK"  # Fill or kill

# Rate Limit Type
REQUEST_WEIGHT = "REQUEST_WEIGHT"
ORDERS = "ORDERS"
ORDERS_24HR = "ORDERS_24HR"
RAW_REQUESTS = "RAW_REQUESTS"

# Rate Limit time intervals
ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400

MAX_REQUEST = 5000

# Order States
ORDER_STATE = {
    "PENDING": OrderState.PENDING_CREATE,
    "NEW": OrderState.OPEN,
    "FILLED": OrderState.FILLED,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "PENDING_CANCEL": OrderState.OPEN,
    "CANCELED": OrderState.CANCELED,
    "REJECTED": OrderState.FAILED,
    "EXPIRED": OrderState.FAILED,
    "EXPIRED_IN_MATCH": OrderState.FAILED,
}

# Websocket event types
DIFF_EVENT_TYPE = "InstrumentInfo"
TRADE_EVENT_TYPE = "Trade"

# Mobin broker order throttling (place + cancel combined)
MOBIN_ORDER_OPS = "MOBIN_ORDER_OPS"  # max 20 ops / minute (place + cancel)
MOBIN_ORDER_MIN_GAP = "MOBIN_ORDER_MIN_GAP"  # min 5 seconds between any order op
MOBIN_ORDER_OPS_PER_MINUTE = 20
MOBIN_ORDER_MIN_INTERVAL_SECONDS = 2

RATE_LIMITS = [
    RateLimit(limit_id='negotiate', limit=6000, time_interval=ONE_MINUTE),
    # Pools
    RateLimit(limit_id=REQUEST_WEIGHT, limit=6000, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDERS, limit=100, time_interval=10 * ONE_SECOND),
    RateLimit(limit_id=ORDERS_24HR, limit=200000, time_interval=ONE_DAY),
    RateLimit(limit_id=RAW_REQUESTS, limit=61000, time_interval=5 * ONE_MINUTE),
    # Weighted Limits
    RateLimit(limit_id=TICKER_PRICE_CHANGE_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 2),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=TICKER_BOOK_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=PRICES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=EXCHANGE_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 100),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MOBIN_STREAM_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 2),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=PING_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ACCOUNTS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=PORTFOLIO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MY_TRADES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MY_ORDERS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    # Shared pools for SaveRequest + CancelRequest
    RateLimit(limit_id=MOBIN_ORDER_OPS, limit=MOBIN_ORDER_OPS_PER_MINUTE, time_interval=ONE_MINUTE),
    RateLimit(limit_id=MOBIN_ORDER_MIN_GAP, limit=1, time_interval=MOBIN_ORDER_MIN_INTERVAL_SECONDS),
    # Place order
    RateLimit(limit_id=ORDER_PATH_URL, limit=MOBIN_ORDER_OPS_PER_MINUTE, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(MOBIN_ORDER_OPS, 1),
                             LinkedLimitWeightPair(MOBIN_ORDER_MIN_GAP, 1)]),
    # Cancel order
    RateLimit(limit_id=CANCEL_ORDER_PATH_URL, limit=MOBIN_ORDER_OPS_PER_MINUTE, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(MOBIN_ORDER_OPS, 1),
                             LinkedLimitWeightPair(MOBIN_ORDER_MIN_GAP, 1)])
]

ORDER_NOT_EXIST_ERROR_CODE = -2013
ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = -2011
UNKNOWN_ORDER_MESSAGE = "Unknown order sent"
# RequestErrorCodeEnum from Mobin API docs
ORIGINAL_ORDER_IS_NOT_IN_BOOK = 1600  # OriginalOrderIsNotInBook — filled/gone, not a hard fail
