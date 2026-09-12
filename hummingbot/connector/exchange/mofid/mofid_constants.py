from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = "ir"

HBOT_ORDER_ID_PREFIX = "mofid"
MAX_ORDER_ID_LEN = 32

# Host is fixed in the HAR; keep domain param for binance-shaped signatures, unused in URL.
REST_URL = "https://api-mts.orbis.easytrader.ir"

# No shared version prefix on this API (unlike binance api/v3).
PUBLIC_API_VERSION = ""
PRIVATE_API_VERSION = ""

# Base path only; client ms is appended in get_current_server_time / network check.
SERVER_TIME_PATH_URL = "/easy/api/account/server-time"
PING_PATH_URL = SERVER_TIME_PATH_URL

# Markets / trading rules (POST body {"hash": ""} forces full catalog)
EXCHANGE_INFO_PATH_URL = "/symbols/api/symbols/all"

# REST order book snapshot: GET {SNAPSHOT_PATH_URL}/{isin}
SNAPSHOT_PATH_URL = "/ms/api/MarketSheet/all"
ORDER_BOOK_DEPTH = 5

# Private trading
ORDER_PATH_URL = "/core/api/v2/order"
CANCEL_ORDER_PATH_URL = "/core/api/v2/delete-order"
OPEN_ORDERS_PATH_URL = "/core/api/order"
ORDER_TRADES_PATH_URL = "/easy/api/orderHistory/trades/"
ORDER_REPORT_PATH_URL = "/easy/api/orderHistory/orderReport"
MONEY_PATH_URL = "/easy/api/money"
PORTFOLIO_PATH_URL = "/assetmodule/api/performance"

# Place-order constants (from EasyTrader HAR)
ORDER_FROM = 34
ORDER_MODEL_TYPE_LIMIT = 1
VALIDITY_TYPE_DAY = 0
SIDE_BUY = 0
SIDE_SELL = 1
QUOTE_ASSET = "IRR"

DIFF_EVENT_TYPE = "bestlimit"
TRADE_EVENT_TYPE = "trade"  # synthetic from symbol last-price + cum-volume delta

WS_HEARTBEAT_TIME_INTERVAL = 30.0

# Lightstreamer (public bestlimit order book + symbol last trade)
WSS_URL = "wss://ls.easytrader.ir/lightstreamer"
# Required on WS handshake (403 without it).
LS_WS_PROTOCOL = "TLCP-2.1.0.lightstreamer.com"
LS_CREATE_SESSION_URL = "https://ls.easytrader.ir/lightstreamer/create_session.txt?LS_protocol=TLCP-2.1.0"
LS_ADAPTER_SET = "lsadapter-conf"
LS_CID = "pcYgxn8m8 feOojyA1U661o3g2.pz47Ag7s"
LS_BESTLIMIT_ADAPTER = "BESTLIMIT_ADAPTER"
LS_SYMBOL_ADAPTER = "RLC_ADAPTER"
LS_SLE_ADAPTER = "SLE_ADAPTER"
LS_CREATE_SESSION_LIMIT_ID = "ls_create_session"
LS_CHANNEL_BESTLIMIT = "bestlimit"
LS_CHANNEL_SYMBOL = "symbol"

# Private user stream (RAW groups on same LS host; no Binance listenKey)
LS_CHANNEL_LOGIN = "login"
LS_CHANNEL_ORDER = "order"
LS_CHANNEL_MONEY = "money"
USER_ORDER_EVENT_TYPE = "order"
USER_MONEY_EVENT_TYPE = "money"
USER_LOGIN_EVENT_TYPE = "login"
LS_ORDER_SCHEMA = ["timestamp", "meta"]
LS_META_ONLY_SCHEMA = ["meta"]

# REST orderState ints (from open orders / orderReport / WS orderStateStr)
ORDER_STATE = {
    2: OrderState.FAILED,
    6: OrderState.OPEN,  # OnBoard
    8: OrderState.PARTIALLY_FILLED,  # PartiallyExecution
    15: OrderState.PARTIALLY_FILLED,  # modified / residual open with fills
    18: OrderState.CANCELED,
    20: OrderState.FILLED,  # OrderExecuted
    36: OrderState.CANCELED,  # PartiallyExecutedAndCanceled
}

# Lightstreamer private order channel string states
WS_ORDER_STATE = {
    "OnBoard": OrderState.OPEN,
    "PartiallyExecution": OrderState.PARTIALLY_FILLED,
    "OrderExecuted": OrderState.FILLED,
    "CancelByBroker": OrderState.CANCELED,
    "PartiallyExecutedAndCanceled": OrderState.CANCELED,
}

ONE_MINUTE = 60
MAX_REQUEST = 5000

# Placeholder until we know real limits from docs/traffic.
RATE_LIMITS = [
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=EXCHANGE_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDER_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=CANCEL_ORDER_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=OPEN_ORDERS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDER_TRADES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDER_REPORT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=MONEY_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=PORTFOLIO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=LS_CREATE_SESSION_LIMIT_ID, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
]
