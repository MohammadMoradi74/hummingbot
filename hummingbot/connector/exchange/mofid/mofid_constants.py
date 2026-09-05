from hummingbot.core.api_throttler.data_types import RateLimit

DEFAULT_DOMAIN = "ir"

# Host is fixed in the HAR; keep domain param for binance-shaped signatures, unused in URL.
REST_URL = "https://api-mts.orbis.easytrader.ir"

# No shared version prefix on this API (unlike binance api/v3).
PUBLIC_API_VERSION = ""
PRIVATE_API_VERSION = ""

# Base path only; client ms is appended in get_current_server_time.
SERVER_TIME_PATH_URL = "/easy/api/account/server-time"

ONE_MINUTE = 60
MAX_REQUEST = 5000

# Placeholder until we know real limits from docs/traffic.
RATE_LIMITS = [
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE),
]
