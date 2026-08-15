from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = "ir"

HBOT_ORDER_ID_PREFIX = "NOB"
MAX_ORDER_ID_LEN = 32

# Base URL (docs use apiv2.nobitex.ir)
REST_URL = "https://apiv2.nobitex.{}/"
WSS_URL = "wss://ws.nobitex.ir/connection/websocket"
USER_AGENT = "TraderBot/hummingbot/1.0"

# Paths include API version where needed (public_rest_url no longer prepends "v3")
PUBLIC_API_VERSION = ""
PRIVATE_API_VERSION = ""

# Public
SNAPSHOT_PATH_URL = "/v3/orderbook"
SERVER_TIME_PATH_URL = "/v3/orderbook/all"
EXCHANGE_INFO_PATH_URL = "/v2/options"
MARKET_STATS_PATH_URL = "/market/stats"
PING_PATH_URL = MARKET_STATS_PATH_URL

# Private
USER_PROFILE_PATH_URL = "/users/profile"
WS_TOKEN_PATH_URL = "/auth/ws/token/"
ACCOUNTS_PATH_URL = "/v2/wallets"
ORDER_PATH_URL = "/market/orders/add"
ORDER_STATUS_PATH_URL = "/market/orders/status"
ORDER_CANCEL_PATH_URL = "/market/orders/update-status"
ORDER_LIST_PATH_URL = "/market/orders/list"
MY_TRADES_PATH_URL = "/market/trades/list"

WS_HEARTBEAT_TIME_INTERVAL = 20
WS_TOKEN_REFRESH_INTERVAL = 15 * 60  # token TTL = 1200s

SIDE_BUY = "buy"
SIDE_SELL = "sell"

EXECUTION_LIMIT = "limit"
EXECUTION_MARKET = "market"

REQUEST_WEIGHT = "REQUEST_WEIGHT"
ORDERS = "ORDERS"
RAW_REQUESTS = "RAW_REQUESTS"

ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400
MAX_REQUEST = 5000

# Nobitex order statuses → HB OrderState
ORDER_STATE = {
    "New": OrderState.OPEN,
    "Active": OrderState.OPEN,
    "Inactive": OrderState.OPEN,
    "Done": OrderState.FILLED,
    "Canceled": OrderState.CANCELED,
}

DIFF_EVENT_TYPE = "depthUpdate"
TRADE_EVENT_TYPE = "trade"
WS_ORDERBOOK_CHANNEL_PREFIX = "public:orderbook-"
WS_TRADES_CHANNEL_PREFIX = "public:trades-"
WS_PRIVATE_ORDERS_CHANNEL_PREFIX = "private:orders#"
WS_PRIVATE_TRADES_CHANNEL_PREFIX = "private:trades#"

# Quote asset mapping: HB uses IRT; Nobitex API uses rls
QUOTE_ASSET_MAP = {
    "IRT": "rls",
    "RLS": "rls",
    "USDT": "usdt",
}
QUOTE_ASSET_MAP_REVERSE = {
    "rls": "IRT",
    "usdt": "USDT",
}

RATE_LIMITS = [
    RateLimit(limit_id=REQUEST_WEIGHT, limit=6000, time_interval=ONE_MINUTE),
    RateLimit(limit_id=RAW_REQUESTS, limit=61000, time_interval=5 * ONE_MINUTE),
    RateLimit(limit_id=ORDERS, limit=300, time_interval=10 * ONE_MINUTE),
    RateLimit(limit_id=SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 100),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=EXCHANGE_INFO_PATH_URL, limit=30, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 5),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MARKET_STATS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=USER_PROFILE_PATH_URL, limit=60, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=WS_TOKEN_PATH_URL, limit=30, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ACCOUNTS_PATH_URL, limit=60, time_interval=2 * ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 5),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDER_PATH_URL, limit=300, time_interval=10 * ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDER_STATUS_PATH_URL, limit=300, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDER_CANCEL_PATH_URL, limit=90, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDER_LIST_PATH_URL, limit=30, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MY_TRADES_PATH_URL, limit=30, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
]
