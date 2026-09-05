from typing import Dict, List, Optional, Tuple

# Lightstreamer MERGE bestlimit schema (field order must match SUB control).
BESTLIMIT_SCHEMA: List[str] = [
    "timestamp",
    "buy-volume-1", "sell-volume-1", "buy-order-count-1", "sell-order-count-1", "buy-price-1", "sell-price-1",
    "buy-volume-2", "sell-volume-2", "buy-order-count-2", "sell-order-count-2", "buy-price-2", "sell-price-2",
    "buy-volume-3", "sell-volume-3", "buy-order-count-3", "sell-order-count-3", "buy-price-3", "sell-price-3",
    "buy-volume-4", "sell-volume-4", "buy-order-count-4", "sell-order-count-4", "buy-price-4", "sell-price-4",
    "buy-volume-5", "sell-volume-5", "buy-order-count-5", "sell-order-count-5", "buy-price-5", "sell-price-5",
]

# Minimal symbol schema for last price + cumulative day volume (synthetic trades).
SYMBOL_TRADE_SCHEMA: List[str] = [
    "last-trade-price",
    "total-number-of-shares-traded",
]


def expand_tlcp_fields(raw_fields: List[str]) -> List[str]:
    """Expand Lightstreamer ^N compression (N unchanged empty fields)."""
    out: List[str] = []
    for field in raw_fields:
        if field.startswith("^") and field[1:].isdigit():
            out.extend([""] * int(field[1:]))
        else:
            out.append(field)
    return out


def merge_bestlimit_fields(
        previous: Dict[str, Optional[str]],
        raw_fields: List[str],
        schema: Optional[List[str]] = None,
) -> Dict[str, Optional[str]]:
    schema = schema or BESTLIMIT_SCHEMA
    merged = dict(previous)
    expanded = expand_tlcp_fields(raw_fields)
    for i, name in enumerate(schema):
        if i >= len(expanded):
            break
        value = expanded[i]
        if value == "":
            continue  # unchanged
        if value == "#":
            merged[name] = None
        else:
            merged[name] = value
    return merged


def bestlimit_state_to_bids_asks(
        state: Dict[str, Optional[str]],
        depth: int = 5,
) -> Tuple[List[List[str]], List[List[str]]]:
    bids: List[List[str]] = []
    asks: List[List[str]] = []
    for level in range(1, depth + 1):
        buy_price = state.get(f"buy-price-{level}")
        buy_volume = state.get(f"buy-volume-{level}")
        sell_price = state.get(f"sell-price-{level}")
        sell_volume = state.get(f"sell-volume-{level}")
        if buy_price and buy_volume and float(buy_price) > 0 and float(buy_volume) > 0:
            bids.append([str(buy_price), str(buy_volume)])
        if sell_price and sell_volume and float(sell_price) > 0 and float(sell_volume) > 0:
            asks.append([str(sell_price), str(sell_volume)])
    return bids, asks


def parse_conok_session_id(create_session_body: str) -> str:
    for line in create_session_body.replace("\r\n", "\n").split("\n"):
        if line.startswith("CONOK,"):
            return line.split(",")[1]
    raise ValueError(f"CONOK not found in Lightstreamer create_session response: {create_session_body[:200]}")


def parse_u_update_line(line: str) -> Optional[Tuple[str, List[str]]]:
    """Return (sub_id, raw_fields) for a TLCP U line, else None."""
    if not line.startswith("U,"):
        return None
    parts = line.split(",", 3)
    if len(parts) < 4:
        return None
    sub_id = parts[1]
    fields = parts[3].split("|")
    return sub_id, fields
