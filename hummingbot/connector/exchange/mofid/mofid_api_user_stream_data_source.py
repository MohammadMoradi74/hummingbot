import asyncio
import json
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.exchange.mofid import mofid_constants as CONSTANTS
from hummingbot.connector.exchange.mofid.mofid_auth import MofidAuth
from hummingbot.connector.exchange.mofid.mofid_lightstreamer import parse_conok_session_id, parse_u_update_line
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSPlainTextRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.mofid.mofid_exchange import MofidExchange


class MofidAPIUserStreamDataSource(UserStreamTrackerDataSource):
    """
    Private Lightstreamer stream (order + money + login).

    No Binance-style listen key: HTTP create_session (LS_user) → WS bind → RAW SUB.
    """

    HEARTBEAT_TIME_INTERVAL = CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 auth: MofidAuth,
                 trading_pairs: List[str],
                 connector: "MofidExchange",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__()
        self._auth = auth
        self._trading_pairs = trading_pairs
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._ls_session_id: Optional[str] = None
        self._next_sub_id = 1
        self._next_req_id = 1
        self._sub_id_to_channel: Dict[str, str] = {}

    async def _create_lightstreamer_session(self) -> str:
        body = urllib.parse.urlencode({
            "LS_phase": "1",
            "LS_cause": "new.api",
            "LS_polling": "true",
            "LS_polling_millis": "0",
            "LS_idle_millis": "0",
            "LS_cid": CONSTANTS.LS_CID,
            "LS_adapter_set": CONSTANTS.LS_ADAPTER_SET,
            "LS_user": MofidAuth.ls_user,
        })
        rest_assistant = await self._api_factory.get_rest_assistant()
        request = RESTRequest(
            method=RESTMethod.POST,
            url=CONSTANTS.LS_CREATE_SESSION_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            throttler_limit_id=CONSTANTS.LS_CREATE_SESSION_LIMIT_ID,
        )
        async with self._api_factory._throttler.execute_task(limit_id=CONSTANTS.LS_CREATE_SESSION_LIMIT_ID):
            response = await rest_assistant.call(request=request)
            if response.status >= 400:
                text = await response.text()
                raise IOError(
                    f"Error creating Lightstreamer session. HTTP {response.status}. Error: {text}"
                )
            text = await response.text()
        return parse_conok_session_id(text)

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    async def _connected_websocket_assistant(self) -> WSAssistant:
        self._ls_session_id = await self._create_lightstreamer_session()
        ws = await self._get_ws_assistant()
        await ws.connect(
            ws_url=CONSTANTS.WSS_URL,
            ping_timeout=self.HEARTBEAT_TIME_INTERVAL,
            ws_headers=self._auth.ws_connect_headers(),
        )
        bind = (
            f"bind_session\r\n"
            f"LS_session={self._ls_session_id}&LS_phase=2&LS_cause=loop1&"
        )
        await ws.send(WSPlainTextRequest(payload=bind))
        while True:
            response = await ws.receive()
            if response is None:
                raise ConnectionError("Lightstreamer websocket closed during bind_session")
            data = response.data
            if isinstance(data, str) and "CONOK," in data:
                break
        return ws

    def _ls_raw_control(self, group: str, schema: List[str], sub_id: int, req_id: int) -> str:
        params = {
            "LS_mode": "RAW",
            "LS_group": group,
            "LS_schema": " ".join(schema),
            "LS_data_adapter": CONSTANTS.LS_SLE_ADAPTER,
            "LS_snapshot": "false",
            "LS_subId": str(sub_id),
            "LS_op": "add",
            "LS_reqId": str(req_id),
        }
        if self._ls_session_id:
            params["LS_session"] = self._ls_session_id
        return "control\r\n" + urllib.parse.urlencode(params) + "&"

    def _alloc_sub(self, channel: str) -> tuple:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        req_id = self._next_req_id
        self._next_req_id += 1
        self._sub_id_to_channel[str(sub_id)] = channel
        return sub_id, req_id

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        try:
            channels = (
                (CONSTANTS.LS_CHANNEL_LOGIN, CONSTANTS.LS_META_ONLY_SCHEMA),
                (CONSTANTS.LS_CHANNEL_ORDER, CONSTANTS.LS_ORDER_SCHEMA),
                (CONSTANTS.LS_CHANNEL_MONEY, CONSTANTS.LS_META_ONLY_SCHEMA),
            )
            for group, schema in channels:
                sub_id, req_id = self._alloc_sub(group)
                payload = self._ls_raw_control(group=group, schema=schema, sub_id=sub_id, req_id=req_id)
                await websocket_assistant.send(WSPlainTextRequest(payload=payload))
            self.logger().info("Subscribed to Mofid private Lightstreamer login/order/money channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(
                "Unexpected error occurred subscribing to Mofid user streams..."
            )
            raise

    @staticmethod
    def _json_payload_from_fields(fields: List[str]) -> Optional[Dict[str, Any]]:
        for field in fields:
            text = (field or "").strip()
            if text.startswith("{"):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
        return None

    async def _process_event_message(self, event_message: Any, queue: asyncio.Queue):
        if not isinstance(event_message, str) or not event_message:
            return
        for line in event_message.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith(("PROBE", "SYNC", "REQOK", "SUBOK", "CONF", "CONS", "CONOK")):
                continue
            parsed = parse_u_update_line(line)
            if parsed is None:
                continue
            sub_id, fields = parsed
            channel = self._sub_id_to_channel.get(sub_id)
            if channel is None:
                continue
            payload = self._json_payload_from_fields(fields)
            if not payload:
                continue
            if channel == CONSTANTS.LS_CHANNEL_ORDER:
                event = dict(payload)
                event["e"] = CONSTANTS.USER_ORDER_EVENT_TYPE
                if len(fields) >= 1 and fields[0] and not fields[0].startswith("{"):
                    event["ls_timestamp"] = fields[0]
                queue.put_nowait(event)
            elif channel == CONSTANTS.LS_CHANNEL_MONEY:
                event = dict(payload)
                event["e"] = CONSTANTS.USER_MONEY_EVENT_TYPE
                queue.put_nowait(event)
            elif channel == CONSTANTS.LS_CHANNEL_LOGIN:
                event = dict(payload)
                event["e"] = CONSTANTS.USER_LOGIN_EVENT_TYPE
                queue.put_nowait(event)

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        await super()._on_user_stream_interruption(websocket_assistant=websocket_assistant)
        self._ls_session_id = None
        self._sub_id_to_channel.clear()
        self._next_sub_id = 1
        self._next_req_id = 1
        await self._sleep(5)
