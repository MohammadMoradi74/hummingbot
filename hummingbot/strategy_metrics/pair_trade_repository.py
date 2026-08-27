from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from hummingbot.strategy_metrics.config import AssetMark
from hummingbot.strategy_metrics.db import MetricsDatabase
from hummingbot.strategy_metrics.repository import PortfolioSnapshotResult, dec, utc_now, value_portfolio

SCHEMA = "pair_trade"


class PairTradeMetricsRepository:
    def __init__(self, db: MetricsDatabase):
        self._db = db

    def ensure_strategy(self, name: str, strategy_type: str, config_file: str) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA}.strategy (name, strategy_type, config_file)
                    VALUES (:name, :strategy_type, :config_file)
                    ON CONFLICT (name) DO UPDATE
                        SET strategy_type = EXCLUDED.strategy_type,
                            config_file = COALESCE(EXCLUDED.config_file, {SCHEMA}.strategy.config_file)
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
                    f"""
                    INSERT INTO {SCHEMA}.session (strategy_id, quote_currency, metadata)
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
                            f"""
                            INSERT INTO {SCHEMA}.session_balance (session_id, exchange, asset, amount)
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
            f"UPDATE {SCHEMA}.session SET ended_at = NOW() WHERE id = :session_id",
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
                            f"""
                            INSERT INTO {SCHEMA}.balance_snapshot
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
                    f"""
                    INSERT INTO {SCHEMA}.portfolio_snapshot
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
                    f"""
                    SELECT exchange, asset, amount
                    FROM {SCHEMA}.session_balance
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            ).all()
        balances: Dict[str, Dict[str, Decimal]] = {}
        for exchange, asset, amount in rows:
            balances.setdefault(exchange, {})[asset] = dec(amount)
        return value_portfolio(balances, asset_marks, mark_prices, quote_currency)

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
                    f"""
                    INSERT INTO {SCHEMA}.event_log (session_id, ts, level, event_type, payload)
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

    def insert_signal_rows(
        self,
        session_id: int,
        bin_ts: datetime,
        rows: List[Dict[str, Any]],
    ) -> None:
        if not rows:
            return
        with self._db.session() as session:
            for row in rows:
                session.execute(
                    text(
                        f"""
                        INSERT INTO {SCHEMA}.signal_snapshot (
                            session_id, bin_ts, symbol, ret, ba_spread, mid_price,
                            ret_ask, ret_bid, ask_price, bid_price, ask_volume, bid_volume
                        ) VALUES (
                            :session_id, :bin_ts, :symbol, :ret, :ba_spread, :mid_price,
                            :ret_ask, :ret_bid, :ask_price, :bid_price, :ask_volume, :bid_volume
                        )
                        ON CONFLICT (session_id, bin_ts, symbol) DO UPDATE
                            SET ret = EXCLUDED.ret,
                                ba_spread = EXCLUDED.ba_spread,
                                mid_price = EXCLUDED.mid_price,
                                ret_ask = EXCLUDED.ret_ask,
                                ret_bid = EXCLUDED.ret_bid,
                                ask_price = EXCLUDED.ask_price,
                                bid_price = EXCLUDED.bid_price,
                                ask_volume = EXCLUDED.ask_volume,
                                bid_volume = EXCLUDED.bid_volume
                        """
                    ),
                    {
                        "session_id": session_id,
                        "bin_ts": bin_ts,
                        "symbol": row["symbol"],
                        "ret": _opt_str(row.get("ret")),
                        "ba_spread": _opt_str(row.get("ba_spread")),
                        "mid_price": _opt_str(row.get("mid_price")),
                        "ret_ask": _opt_str(row.get("ret_ask")),
                        "ret_bid": _opt_str(row.get("ret_bid")),
                        "ask_price": _opt_str(row.get("ask_price")),
                        "bid_price": _opt_str(row.get("bid_price")),
                        "ask_volume": _opt_str(row.get("ask_volume")),
                        "bid_volume": _opt_str(row.get("bid_volume")),
                    },
                )

    def insert_basket_snapshot(
        self,
        session_id: int,
        ts: datetime,
        symbols: List[str],
        quote_balance: Optional[Decimal],
        pending_order_count: int,
        lowest_ret_symbol: Optional[str],
    ) -> None:
        """Basket composition only — chart P&L from portfolio_snapshot."""
        with self._db.session() as session:
            session.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA}.basket_snapshot (
                        session_id, ts, symbols, quote_balance,
                        pending_order_count, lowest_ret_symbol
                    ) VALUES (
                        :session_id, :ts, CAST(:symbols AS jsonb), :quote_balance,
                        :pending_order_count, :lowest_ret_symbol
                    )
                    ON CONFLICT (session_id, ts) DO UPDATE
                        SET symbols = EXCLUDED.symbols,
                            quote_balance = EXCLUDED.quote_balance,
                            pending_order_count = EXCLUDED.pending_order_count,
                            lowest_ret_symbol = EXCLUDED.lowest_ret_symbol
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts,
                    "symbols": json.dumps(symbols),
                    "quote_balance": str(quote_balance) if quote_balance is not None else None,
                    "pending_order_count": pending_order_count,
                    "lowest_ret_symbol": lowest_ret_symbol,
                },
            )

    def insert_rotation(
        self,
        session_id: int,
        ts: datetime,
        prev_symbol: str,
        new_symbol: str,
        transition_score: Optional[Decimal],
        transition_value: Optional[Decimal],
        sell_pair: Optional[str],
        buy_pair: Optional[str],
        sell_order_id: Optional[str],
        status: str,
        quote_earned: Decimal,
        quote_spent: Decimal,
        metadata: Optional[dict] = None,
        last_edge: Optional[Decimal] = None,
        intended_sell_price: Optional[Decimal] = None,
        intended_buy_price: Optional[Decimal] = None,
        signal_mid_prev: Optional[Decimal] = None,
        signal_mid_new: Optional[Decimal] = None,
        ret_prev: Optional[Decimal] = None,
        ret_new: Optional[Decimal] = None,
        ba_spread_prev: Optional[Decimal] = None,
        ba_spread_new: Optional[Decimal] = None,
    ) -> int:
        with self._db.session() as session:
            row = session.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA}.rotation (
                        session_id, ts, prev_symbol, new_symbol, transition_score,
                        transition_value, sell_pair, buy_pair, sell_order_id, status,
                        quote_earned, quote_spent, last_edge,
                        intended_sell_price, intended_buy_price,
                        signal_mid_prev, signal_mid_new, ret_prev, ret_new,
                        ba_spread_prev, ba_spread_new, metadata
                    ) VALUES (
                        :session_id, :ts, :prev_symbol, :new_symbol, :transition_score,
                        :transition_value, :sell_pair, :buy_pair, :sell_order_id, :status,
                        :quote_earned, :quote_spent, :last_edge,
                        :intended_sell_price, :intended_buy_price,
                        :signal_mid_prev, :signal_mid_new, :ret_prev, :ret_new,
                        :ba_spread_prev, :ba_spread_new, CAST(:metadata AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts,
                    "prev_symbol": prev_symbol,
                    "new_symbol": new_symbol,
                    "transition_score": _opt_str(transition_score),
                    "transition_value": _opt_str(transition_value),
                    "sell_pair": sell_pair,
                    "buy_pair": buy_pair,
                    "sell_order_id": sell_order_id,
                    "status": status,
                    "quote_earned": str(quote_earned),
                    "quote_spent": str(quote_spent),
                    "last_edge": _opt_str(last_edge),
                    "intended_sell_price": _opt_str(intended_sell_price),
                    "intended_buy_price": _opt_str(intended_buy_price),
                    "signal_mid_prev": _opt_str(signal_mid_prev),
                    "signal_mid_new": _opt_str(signal_mid_new),
                    "ret_prev": _opt_str(ret_prev),
                    "ret_new": _opt_str(ret_new),
                    "ba_spread_prev": _opt_str(ba_spread_prev),
                    "ba_spread_new": _opt_str(ba_spread_new),
                    "metadata": json.dumps(metadata or {}),
                },
            ).one()
            return int(row.id)

    def update_rotation(
        self,
        session_id: int,
        sell_order_id: str,
        status: Optional[str] = None,
        quote_earned: Optional[Decimal] = None,
        quote_spent: Optional[Decimal] = None,
        metadata: Optional[dict] = None,
        realized_edge: Optional[Decimal] = None,
        sell_vwap: Optional[Decimal] = None,
        buy_vwap: Optional[Decimal] = None,
        sell_vwap_net: Optional[Decimal] = None,
        buy_vwap_net: Optional[Decimal] = None,
        total_fee_quote: Optional[Decimal] = None,
        realized_edge_net: Optional[Decimal] = None,
        sell_slippage: Optional[Decimal] = None,
        buy_slippage: Optional[Decimal] = None,
        latency_sec: Optional[Decimal] = None,
    ) -> None:
        updates: List[str] = []
        params: Dict[str, Any] = {"session_id": session_id, "sell_order_id": sell_order_id}
        field_map = {
            "status": status,
            "quote_earned": str(quote_earned) if quote_earned is not None else None,
            "quote_spent": str(quote_spent) if quote_spent is not None else None,
            "realized_edge": _opt_str(realized_edge),
            "sell_vwap": _opt_str(sell_vwap),
            "buy_vwap": _opt_str(buy_vwap),
            "sell_vwap_net": _opt_str(sell_vwap_net),
            "buy_vwap_net": _opt_str(buy_vwap_net),
            "total_fee_quote": _opt_str(total_fee_quote),
            "realized_edge_net": _opt_str(realized_edge_net),
            "sell_slippage": _opt_str(sell_slippage),
            "buy_slippage": _opt_str(buy_slippage),
            "latency_sec": _opt_str(latency_sec),
        }
        for col, val in field_map.items():
            if col == "status":
                if status is not None:
                    updates.append("status = :status")
                    params["status"] = status
                continue
            if val is not None:
                updates.append(f"{col} = :{col}")
                params[col] = val
        if metadata is not None:
            updates.append("metadata = CAST(:metadata AS jsonb)")
            params["metadata"] = json.dumps(metadata)
        if not updates:
            return
        sql = (
            f"UPDATE {SCHEMA}.rotation SET "
            + ", ".join(updates)
            + " WHERE session_id = :session_id AND sell_order_id = :sell_order_id"
        )
        with self._db.session() as session:
            session.execute(text(sql), params)

    def get_rotation_id(self, session_id: int, sell_order_id: str) -> Optional[int]:
        with self._db.session() as session:
            row = session.execute(
                text(
                    f"""
                    SELECT id FROM {SCHEMA}.rotation
                    WHERE session_id = :session_id AND sell_order_id = :sell_order_id
                    """
                ),
                {"session_id": session_id, "sell_order_id": sell_order_id},
            ).one_or_none()
        return int(row.id) if row is not None else None

    def insert_fill(
        self,
        session_id: int,
        ts: datetime,
        rotation_id: Optional[int],
        order_id: str,
        trading_pair: str,
        side: str,
        amount: Decimal,
        price: Decimal,
        fee_quote: Decimal,
        exchange_trade_id: Optional[str],
        fill_key: str,
    ) -> None:
        with self._db.session() as session:
            session.execute(
                text(
                    f"""
                    INSERT INTO {SCHEMA}.fill (
                        session_id, ts, rotation_id, order_id, trading_pair, side,
                        amount, price, fee_quote, exchange_trade_id, fill_key
                    ) VALUES (
                        :session_id, :ts, :rotation_id, :order_id, :trading_pair, :side,
                        :amount, :price, :fee_quote, :exchange_trade_id, :fill_key
                    )
                    ON CONFLICT (fill_key) DO UPDATE
                        SET amount = EXCLUDED.amount,
                            price = EXCLUDED.price,
                            fee_quote = EXCLUDED.fee_quote
                    """
                ),
                {
                    "session_id": session_id,
                    "ts": ts,
                    "rotation_id": rotation_id,
                    "order_id": order_id,
                    "trading_pair": trading_pair,
                    "side": side,
                    "amount": str(amount),
                    "price": str(price),
                    "fee_quote": str(fee_quote),
                    "exchange_trade_id": exchange_trade_id,
                    "fill_key": fill_key,
                },
            )


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)
