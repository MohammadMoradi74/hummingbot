from hummingbot.core.api_throttler.data_types import RateLimit

DEFAULT_DOMAIN = "ir"

# Host is fixed in the HAR; keep domain param for binance-shaped signatures, unused in URL.
REST_URL = "https://api-mts.orbis.easytrader.ir"

# No shared version prefix on this API (unlike binance api/v3).
PUBLIC_API_VERSION = ""
PRIVATE_API_VERSION = ""

# Base path only; client ms is appended in get_current_server_time.
SERVER_TIME_PATH_URL = "/easy/api/account/server-time"

# REST order book snapshot: GET {SNAPSHOT_PATH_URL}/{isin}
SNAPSHOT_PATH_URL = "/ms/api/MarketSheet/all"
ORDER_BOOK_DEPTH = 5

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
LS_CREATE_SESSION_LIMIT_ID = "ls_create_session"
LS_CHANNEL_BESTLIMIT = "bestlimit"
LS_CHANNEL_SYMBOL = "symbol"

ONE_MINUTE = 60
MAX_REQUEST = 5000

# Placeholder until we know real limits from docs/traffic.
RATE_LIMITS = [
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
    RateLimit(limit_id=LS_CREATE_SESSION_LIMIT_ID, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
]
