from typing import Any, Dict, Optional

from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class BitpinWSHelper:
    _message_id: int = 0

    @classmethod
    def _next_id(cls) -> int:
        cls._message_id += 1
        return cls._message_id

    @classmethod
    def ws_url(cls, domain: str = "ir") -> str:
        from hummingbot.connector.exchange.bitpin import bitpin_constants as CONSTANTS
        return CONSTANTS.WSS_URL.format(domain)

    @classmethod
    def connect_payload(cls, token: Optional[str] = None) -> Dict[str, Any]:
        connect_params: Dict[str, Any] = {}
        if token:
            connect_params["token"] = token
        return {"id": cls._next_id(), "connect": connect_params}

    @classmethod
    def subscribe_payload(cls, channel: str) -> Dict[str, Any]:
        # Centrifugo native format (what centrifuge-js uses under the hood)
        return {"id": cls._next_id(), "subscribe": {"channel": channel}}

    @classmethod
    def orderbook_channel(cls, symbol: str) -> str:
        return f"orderbook:{symbol}"

    @classmethod
    def matches_channel(cls, symbol: str) -> str:
        return f"matches:{symbol}"

    @classmethod
    def user_orders_channel(cls, user_identifier: str) -> str:
        return f"user:order_info#{user_identifier}"

    @classmethod
    def is_ping(cls, message: Dict[str, Any]) -> bool:
        return message == {}

    @classmethod
    def is_connect_reply(cls, message: Dict[str, Any]) -> bool:
        return "connect" in message and "id" in message

    @classmethod
    def is_error_reply(cls, message: Dict[str, Any]) -> bool:
        return "error" in message

    @classmethod
    def extract_event_data(cls, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Unwrap Centrifugo publication → Bitpin event payload."""
        if cls.is_ping(message) or cls.is_connect_reply(message):
            return None
        if "error" in message:
            return None

        # Centrifugo v4+ push
        push = message.get("push")
        if push is not None:
            pub = push.get("pub", {})
            data = pub.get("data")
            if isinstance(data, dict):
                return data
            return None

        # Direct event (fallback / tests)
        if "event" in message:
            return message

        return None

    @classmethod
    async def send_connect(cls, ws: WSAssistant, token: Optional[str] = None) -> None:
        await ws.send(WSJSONRequest(payload=cls.connect_payload(token=token)))

    @classmethod
    async def wait_for_connect_reply(cls, ws: WSAssistant, timeout: float = 10.0) -> None:
        import asyncio

        async def _wait():
            while True:
                response = await ws.receive()
                if response is None or response.data is None:
                    raise ConnectionError("WS closed before connect reply")
                data = response.data
                if cls.is_ping(data):
                    await ws.send(WSJSONRequest(payload={}))
                    continue
                if cls.is_connect_reply(data):
                    return
                if cls.is_error_reply(data):
                    raise ConnectionError(f"Centrifugo connect error: {data['error']}")
        await asyncio.wait_for(_wait(), timeout=timeout)

    @classmethod
    async def subscribe(cls, ws: WSAssistant, channel: str) -> None:
        await ws.send(WSJSONRequest(payload=cls.subscribe_payload(channel)))
