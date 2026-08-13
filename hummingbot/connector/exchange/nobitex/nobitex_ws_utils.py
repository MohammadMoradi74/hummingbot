import asyncio
import json
from typing import Any, Dict, Optional

from hummingbot.connector.exchange.nobitex import nobitex_constants as CONSTANTS
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class NobitexWSHelper:
    _message_id: int = 0

    @classmethod
    def _next_id(cls) -> int:
        cls._message_id += 1
        return cls._message_id

    @classmethod
    def reset_message_id(cls) -> None:
        cls._message_id = 0

    @classmethod
    def connect_payload(cls, token: Optional[str] = None) -> Dict[str, Any]:
        connect_params: Dict[str, Any] = {}
        if token:
            connect_params["token"] = token
        return {"id": cls._next_id(), "connect": connect_params}

    @classmethod
    def subscribe_payload(cls, channel: str) -> Dict[str, Any]:
        return {"id": cls._next_id(), "subscribe": {"channel": channel}}

    @classmethod
    def unsubscribe_payload(cls, channel: str) -> Dict[str, Any]:
        return {"id": cls._next_id(), "unsubscribe": {"channel": channel}}

    @classmethod
    def orderbook_channel(cls, symbol: str) -> str:
        return f"{CONSTANTS.WS_ORDERBOOK_CHANNEL_PREFIX}{symbol.upper()}"

    @classmethod
    def trades_channel(cls, symbol: str) -> str:
        return f"{CONSTANTS.WS_TRADES_CHANNEL_PREFIX}{symbol.upper()}"

    @classmethod
    def symbol_from_channel(cls, channel: str) -> str:
        return channel.rsplit("-", 1)[-1]

    @classmethod
    def normalize_message(cls, message: Any) -> Optional[Dict[str, Any]]:
        if isinstance(message, dict):
            return message
        if isinstance(message, str):
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @classmethod
    def is_ping(cls, message: Dict[str, Any]) -> bool:
        return message == {}

    @classmethod
    def pong_payload(cls, message: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    @classmethod
    def is_connect_reply(cls, message: Dict[str, Any]) -> bool:
        return "connect" in message

    @classmethod
    def is_error_reply(cls, message: Dict[str, Any]) -> bool:
        return "error" in message

    @classmethod
    def extract_event_data(cls, message: Any) -> Optional[Dict[str, Any]]:
        message = cls.normalize_message(message)
        if message is None:
            return None
        if cls.is_ping(message) or cls.is_connect_reply(message) or "error" in message:
            return None

        push = message.get("push")
        if not isinstance(push, dict):
            return None
        pub = push.get("pub")
        if not isinstance(pub, dict):
            return None
        data = pub.get("data")
        if isinstance(data, str):
            data = cls.normalize_message(data)
        if not isinstance(data, dict):
            return None
        event = dict(data)
        event["_channel"] = push.get("channel", "")
        return event

    @classmethod
    async def send_connect(cls, ws: WSAssistant, token: Optional[str] = None) -> None:
        await ws.send(WSJSONRequest(payload=cls.connect_payload(token=token)))

    @classmethod
    async def wait_for_connect_reply(cls, ws: WSAssistant, timeout: float = 10.0) -> None:
        async def _wait():
            while True:
                response = await ws.receive()
                if response is None or response.data is None:
                    raise ConnectionError("WS closed before connect reply")
                data = cls.normalize_message(response.data)
                if data is None:
                    continue
                if cls.is_ping(data):
                    await ws.send(WSJSONRequest(payload=cls.pong_payload(data)))
                    continue
                if cls.is_connect_reply(data):
                    return
                if cls.is_error_reply(data):
                    raise ConnectionError(f"Centrifugo connect error: {data['error']}")
        await asyncio.wait_for(_wait(), timeout=timeout)

    @classmethod
    async def subscribe(cls, ws: WSAssistant, channel: str) -> None:
        await ws.send(WSJSONRequest(payload=cls.subscribe_payload(channel)))

    @classmethod
    async def unsubscribe(cls, ws: WSAssistant, channel: str) -> None:
        await ws.send(WSJSONRequest(payload=cls.unsubscribe_payload(channel)))
