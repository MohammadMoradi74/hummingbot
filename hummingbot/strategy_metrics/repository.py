from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import text

from hummingbot.strategy_metrics.config import AssetMark
from hummingbot.strategy_metrics.db import MetricsDatabase


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compute_realized_edge(
    ret_prev: float,
    ret_new: float,
    sell_vwap: float,
    buy_vwap: float,
    mid_prev: float,
    mid_new: float,
) -> Optional[float]:
    """realized = ret[prev]-ret[new] - log(buy/mid_new) + log(sell/mid_prev)."""
    if mid_prev <= 0 or mid_new <= 0 or sell_vwap <= 0 or buy_vwap <= 0:
        return None
    return ret_prev - ret_new - math.log(buy_vwap / mid_new) + math.log(sell_vwap / mid_prev)


def compute_log_slippage(executed: float, intended: float) -> Optional[float]:
    """log(executed / intended). Sell: neg=worse; buy: pos=worse."""
    if executed <= 0 or intended <= 0:
        return None
    return math.log(executed / intended)


def fee_to_quote(
    trade_fee: Any,
    price_quote: Decimal,
    amount: Decimal,
    quote: str,
    fx_rate: Optional[Decimal],
) -> Decimal:
    """Convert a TradeFee (or JSON) to a quote-currency cost.

    Handles Mobin/Bitpin-style percent fees (fee ≈ price * amount * percent in quote)
    plus flat_fees. percent_token in a non-quote asset is converted with fx_rate when given.
    """
    if trade_fee is None:
        return Decimal("0")

    total = Decimal("0")
    fee_obj = trade_fee
    if hasattr(trade_fee, "to_json"):
        fee_obj = trade_fee.to_json()
    elif isinstance(trade_fee, str):
        fee_obj = json.loads(trade_fee)
    elif not isinstance(trade_fee, dict):
        return Decimal("0")

    percent = dec(fee_obj.get("percent") or 0)
    if percent != 0:
        notional_fee = price_quote * amount * percent
        percent_token = fee_obj.get("percent_token")
        # None / quote → already in quote (DeductedFromReturns / Mobin ETF fees)
        if percent_token is None or percent_token == quote:
            total += notional_fee
        elif fx_rate and fx_rate > 0:
            total += notional_fee / fx_rate
        else:
            # Base-token percent fee without FX: treat notional as quote (spot ETFs)
            total += notional_fee

    flat_fees = fee_obj.get("flat_fees") or []
    for item in flat_fees:
        token = item.get("token")
        amt = dec(item.get("amount"))
        if token == quote:
            total += amt
        elif token and fx_rate and fx_rate > 0:
            total += amt / fx_rate
        elif token and token in ("BTC", "ETH"):
            total += amt * price_quote
    return total


@dataclass
class PortfolioSnapshotResult:
    portfolio_value: Decimal
    pnl: Optional[Decimal]
    net_exposure: Dict[str, Decimal]
    marks: Dict[str, Decimal]


class MetricsRepository:
    def __init__(self, db: MetricsDatabase):
        self._db = db

    def ensure_strategy(self, name: str, strategy_type: str, config_file: str) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO mm_strategy (name, strategy_type, config_file)
                    VALUES (:name, :strategy_type, :config_file)
                    ON CONFLICT (name) DO UPDATE
                        SET strategy_type = EXCLUDED.strategy_type,
                            config_file = COALESCE(EXCLUDED.config_file, mm_strategy.config_file)
                    RETURNING id
                    """
                ),
                {"name": name, "strategy_type": strategy_type, "config_file": config_file or None},
            ).one()
            return int(row.id)

    def start_session(
        self,
        strategy_id: int,
        quote_currency: str,
        balances: Dict[str, Dict[str, Decimal]],
        metadata: Optional[dict] = None,
    ) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO mm_session (strategy_id, quote_currency, metadata)
                    VALUES (:strategy_id, :quote_currency, CAST(:metadata AS jsonb))
                    RETURNING id
                    """
                ),
                {
                    "strategy_id": strategy_id,
                    "quote_currency": quote_currency,
                    "metadata": json.dumps(metadata or {}),
                },
            ).one()
            session_id = int(row.id)
            for exchange, assets in balances.items():
                for asset, amount in assets.items():
                    session.execute(
                        text(
                            """
                            INSERT INTO mm_session_balance (session_id, exchange, asset, amount)
                            VALUES (:session_id, :exchange, :asset, :amount)
                            ON CONFLICT (session_id, exchange, asset) DO UPDATE
                                SET amount = EXCLUDED.amount
                            """
                        ),
                        {
                            "session_id": session_id,
                            "exchange": exchange,
                            "asset": asset,
                            "amount": str(amount),
                        },
                    )
            return session_id

    def end_session(self, session_id: int) -> None:
        self._db.execute(
            "UPDATE mm_session SET ended_at = NOW() WHERE id = :session_id",
            {"session_id": session_id},
        )

    def insert_balance_snapshot(
        self,
        session_id: int,
        ts: datetime,
        balances: Dict[str, Dict[str, Decimal]],
        available_balances: Optional[Dict[str, Dict[str, Decimal]]] = None,
    ) -> None:
        available_balances = available_balances or {}
        with self._db.session() as session:
            for exchange, assets in balances.items():
                for asset, total in assets.items():
                    available = available_balances.get(exchange, {}).get(asset)
                    session.execute(
                        text(
                            """
                            INSERT INTO mm_balance_snapshot
                                (ts, session_id, exchange, asset, total, available)
                            VALUES
                                (:ts, :session_id, :exchange, :asset, :total, :available)
                            ON CONFLICT (session_id, ts, exchange, asset) DO UPDATE
                                SET total = EXCLUDED.total,
                                    available = EXCLUDED.available
                            """
                        ),
                        {
                            "ts": ts,
                            "session_id": session_id,
                            "exchange": exchange,
                            "asset": asset,
                            "total": str(total),
                            "available": str(available) if available is not None else None,
                        },
                    )

    def insert_portfolio_snapshot(
        self,
        session_id: int,
        ts: datetime,
        result: PortfolioSnapshotResult,
    ) -> None:
        with self._db.session() as session:
            session.execute(
                text(
                    """
                    INSERT INTO mm_portfolio_snapshot
                        (ts, session_id, portfolio_value, pnl, net_exposure, marks)
                    VALUES
                        (:ts, :session_id, :portfolio_value, :pnl,
                         CAST(:net_exposure AS jsonb), CAST(:marks AS jsonb))
                    ON CONFLICT (session_id, ts) DO UPDATE
                        SET portfolio_value = EXCLUDED.portfolio_value,
                            pnl = EXCLUDED.pnl,
                            net_exposure = EXCLUDED.net_exposure,
                            marks = EXCLUDED.marks
                    """
                ),
                {
                    "ts": ts,
                    "session_id": session_id,
                    "portfolio_value": str(result.portfolio_value),
                    "pnl": str(result.pnl) if result.pnl is not None else None,
                    "net_exposure": json.dumps({k: str(v) for k, v in result.net_exposure.items()}),
                    "marks": json.dumps({k: str(v) for k, v in result.marks.items()}),
                },
            )

    def insert_maker_fill(
        self,
        session_id: int,
        ts: datetime,
        exchange: str,
        trading_pair: str,
        base_asset: str,
        quote_asset: str,
        side: str,
        amount: Decimal,
        price: Decimal,
        price_quote: Optional[Decimal],
        fx_rate: Optional[Decimal],
        fair_price: Optional[Decimal],
        fee_quote: Decimal,
        exchange_trade_id: Optional[str],
        order_id: Optional[str],
        maker_fill_key: str,
    ) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO mm_maker_fill (
                        session_id, ts, exchange, trading_pair, base_asset, quote_asset,
                        side, amount, price, price_quote, fx_rate, fair_price, fee_quote,
                        exchange_trade_id, order_id, maker_fill_key
                    ) VALUES (
                        :session_id, :ts, :exchange, :trading_pair, :base_asset, :quote_asset,
                        :side, :amount, :price, :price_quote, :fx_rate, :fair_price, :fee_quote,
                        :exchange_trade_id, :order_id, :maker_fill_key
                    )
                    ON CONFLICT (maker_fill_key) DO UPDATE
                        SET amount = EXCLUDED.amount,
                            price = EXCLUDED.price,
                            price_quote = EXCLUDED.price_quote
                    RETURNING id
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts,
                    "exchange": exchange,
                    "trading_pair": trading_pair,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "side": side,
                    "amount": str(amount),
                    "price": str(price),
                    "price_quote": str(price_quote) if price_quote is not None else None,
                    "fx_rate": str(fx_rate) if fx_rate is not None else None,
                    "fair_price": str(fair_price) if fair_price is not None else None,
                    "fee_quote": str(fee_quote),
                    "exchange_trade_id": exchange_trade_id,
                    "order_id": order_id,
                    "maker_fill_key": maker_fill_key,
                },
            ).one()
            return int(row.id)

    def insert_hedge_event(
        self,
        session_id: int,
        maker_fill_id: Optional[int],
        ts: datetime,
        exchange: str,
        trading_pair: str,
        base_asset: str,
        side: str,
        requested_amount: Decimal,
        filled_amount: Decimal,
        limit_price: Optional[Decimal],
        fill_price: Optional[Decimal],
        order_id: Optional[str],
        status: str,
        fail_reason: Optional[str],
        latency_ms: Optional[int],
        order_id_key: Optional[str],
    ) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO mm_hedge_event (
                        session_id, maker_fill_id, ts, exchange, trading_pair, base_asset,
                        side, requested_amount, filled_amount, limit_price, fill_price,
                        order_id, status, fail_reason, latency_ms, order_id_key
                    ) VALUES (
                        :session_id, :maker_fill_id, :ts, :exchange, :trading_pair, :base_asset,
                        :side, :requested_amount, :filled_amount, :limit_price, :fill_price,
                        :order_id, :status, :fail_reason, :latency_ms, :order_id_key
                    )
                    ON CONFLICT (order_id_key) DO UPDATE
                        SET status = EXCLUDED.status,
                            filled_amount = EXCLUDED.filled_amount,
                            fill_price = EXCLUDED.fill_price,
                            fail_reason = EXCLUDED.fail_reason,
                            latency_ms = EXCLUDED.latency_ms
                    RETURNING id
                    """
                ),
                {
                    "session_id": session_id,
                    "maker_fill_id": maker_fill_id,
                    "ts": ts,
                    "exchange": exchange,
                    "trading_pair": trading_pair,
                    "base_asset": base_asset,
                    "side": side,
                    "requested_amount": str(requested_amount),
                    "filled_amount": str(filled_amount),
                    "limit_price": str(limit_price) if limit_price is not None else None,
                    "fill_price": str(fill_price) if fill_price is not None else None,
                    "order_id": order_id,
                    "status": status,
                    "fail_reason": fail_reason,
                    "latency_ms": latency_ms,
                    "order_id_key": order_id_key,
                },
            ).one()
            return int(row.id)

    def insert_round_trip(
        self,
        session_id: int,
        ts: datetime,
        base_asset: str,
        maker_fill_id: Optional[int],
        hedge_event_id: Optional[int],
        amount: Decimal,
        maker_price_quote: Decimal,
        hedge_price_quote: Decimal,
        spread_bps: Optional[Decimal],
        gross_pnl_quote: Decimal,
        fees_quote: Decimal,
        net_pnl_quote: Decimal,
        status: str,
    ) -> None:
        with self._db.session() as session:
            session.execute(
                text(
                    """
                    INSERT INTO mm_round_trip (
                        session_id, ts, base_asset, maker_fill_id, hedge_event_id, amount,
                        maker_price_quote, hedge_price_quote, spread_bps, gross_pnl_quote,
                        fees_quote, net_pnl_quote, status
                    ) VALUES (
                        :session_id, :ts, :base_asset, :maker_fill_id, :hedge_event_id, :amount,
                        :maker_price_quote, :hedge_price_quote, :spread_bps, :gross_pnl_quote,
                        :fees_quote, :net_pnl_quote, :status
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts,
                    "base_asset": base_asset,
                    "maker_fill_id": maker_fill_id,
                    "hedge_event_id": hedge_event_id,
                    "amount": str(amount),
                    "maker_price_quote": str(maker_price_quote),
                    "hedge_price_quote": str(hedge_price_quote),
                    "spread_bps": str(spread_bps) if spread_bps is not None else None,
                    "gross_pnl_quote": str(gross_pnl_quote),
                    "fees_quote": str(fees_quote),
                    "net_pnl_quote": str(net_pnl_quote),
                    "status": status,
                },
            )

    def insert_event_log(
        self,
        session_id: int,
        event_type: str,
        payload: dict,
        level: str = "info",
        ts: Optional[datetime] = None,
    ) -> None:
        with self._db.session() as session:
            session.execute(
                text(
                    """
                    INSERT INTO mm_event_log (session_id, ts, level, event_type, payload)
                    VALUES (:session_id, :ts, :level, :event_type, CAST(:payload AS jsonb))
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts or utc_now(),
                    "level": level,
                    "event_type": event_type,
                    "payload": json.dumps(payload),
                },
            )

    def get_session_start_value(
        self,
        session_id: int,
        quote_currency: str,
        asset_marks: Dict[str, AssetMark],
        mark_prices: Dict[str, Decimal],
    ) -> Decimal:
        with self._db.session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT exchange, asset, amount
                    FROM mm_session_balance
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            ).all()
        balances: Dict[str, Dict[str, Decimal]] = {}
        for exchange, asset, amount in rows:
            balances.setdefault(exchange, {})[asset] = dec(amount)
        return value_portfolio(balances, asset_marks, mark_prices, quote_currency)


def resolve_mark(asset: str, asset_marks: Dict[str, AssetMark], mark_prices: Dict[str, Decimal], quote: str) -> Decimal:
    if asset == quote:
        return Decimal("1")
    spec = asset_marks.get(asset)
    if spec is None or not spec.trading_pair:
        return Decimal("0")
    raw = mark_prices.get(spec.trading_pair)
    if raw is None or raw <= 0:
        return Decimal("0")
    return (Decimal("1") / raw) if spec.invert else raw


def value_portfolio(
    balances: Dict[str, Dict[str, Decimal]],
    asset_marks: Dict[str, AssetMark],
    mark_prices: Dict[str, Decimal],
    quote: str,
) -> Decimal:
    total = Decimal("0")
    for assets in balances.values():
        for asset, amount in assets.items():
            mark = resolve_mark(asset, asset_marks, mark_prices, quote)
            total += amount * mark
    return total


def net_exposure(balances: Dict[str, Dict[str, Decimal]], assets: Iterable[str]) -> Dict[str, Decimal]:
    exposure: Dict[str, Decimal] = {asset: Decimal("0") for asset in assets}
    for exchange_assets in balances.values():
        for asset, amount in exchange_assets.items():
            if asset in exposure:
                exposure[asset] += amount
    return exposure


def compute_round_trip(
    maker_side: str,
    amount: Decimal,
    maker_price_quote: Decimal,
    hedge_price_quote: Decimal,
    fees_quote: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if maker_side.upper() == "SELL":
        gross = (maker_price_quote - hedge_price_quote) * amount
        spread_bps = (maker_price_quote / hedge_price_quote - Decimal("1")) * Decimal("10000")
    else:
        gross = (hedge_price_quote - maker_price_quote) * amount
        spread_bps = (hedge_price_quote / maker_price_quote - Decimal("1")) * Decimal("10000")
    net = gross - fees_quote
    return gross, spread_bps, net
