from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import PriceType
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy_metrics.db import MetricsDatabase
from hummingbot.strategy_metrics.pair_trade_config import PairTradeMetricsConfig
from hummingbot.strategy_metrics.pair_trade_repository import PairTradeMetricsRepository
from hummingbot.strategy_metrics.repository import PortfolioSnapshotResult, dec, fee_to_quote, utc_now, value_portfolio

logger = logging.getLogger(__name__)


def _opt_dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return dec(value)


class PairTradeMetricsTracker:
    """Logs pair-trade basket strategy metrics to PostgreSQL (pair_trade schema)."""

    def __init__(self, config: PairTradeMetricsConfig, connectors: Dict[str, ConnectorBase]):
        self._config = config
        self._connectors = connectors
        self._db: Optional[MetricsDatabase] = None
        self._repo: Optional[PairTradeMetricsRepository] = None
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="pt-metrics-writer", daemon=True)
        self._session_id: Optional[int] = None
        self._strategy_id: Optional[int] = None
        self._session_start_value: Optional[Decimal] = None
        self._last_snapshot_ts: float = 0.0
        self._started = False
        self._session_init_enqueued = False
        self._last_logical_ts: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.database_url)

    @property
    def session_id(self) -> Optional[int]:
        return self._session_id

    def _ensure_db(self) -> bool:
        if self._db is not None:
            return True
        if not self.enabled:
            return False
        try:
            self._db = MetricsDatabase(self._config.database_url)
            self._repo = PairTradeMetricsRepository(self._db)
            return self._db.ping()
        except Exception as exc:
            logger.warning("Pair-trade metrics database unavailable: %s", exc)
            self._db = None
            self._repo = None
            return False

    def start(self) -> None:
        if not self.enabled:
            logger.info("Pair-trade metrics disabled or MM_METRICS_DATABASE_URL not set.")
            return
        if not self._ensure_db():
            logger.warning("Pair-trade metrics disabled: cannot reach PostgreSQL.")
            return
        if not self._worker.is_alive():
            self._worker.start()
        self._started = True
        logger.info(
            "Pair-trade metrics writer started for %s (session opens when market data is ready).",
            self._config.strategy_name,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._enqueue("end_session")
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=5)
        self._started = False

    def maybe_initialize(self, current_timestamp: float, session_metadata: Optional[dict] = None) -> None:
        if not self._started or self._session_id is not None or self._session_init_enqueued:
            return
        marks = self._collect_marks()
        if not self._required_pairs_ready(marks):
            return
        self._session_init_enqueued = True
        self._logical_ts(current_timestamp)  # prime logical clock
        self._last_snapshot_ts = current_timestamp
        metadata = dict(session_metadata or {})
        metadata.setdefault("exchange", self._config.exchange)
        metadata.setdefault("trading_pairs", self._config.trading_pairs)
        metadata.setdefault("n_slots", self._config.n_slots)
        self._enqueue(
            "start_session",
            balances=self._collect_balances(),
            marks=marks,
            metadata=metadata,
        )

    def maybe_snapshot(
        self,
        current_timestamp: float,
        basket_symbols: List[str],
        quote_balance: Decimal,
        lowest_ret_symbol: Optional[str],
        pending_order_count: int,
    ) -> None:
        if not self._started:
            return
        if self._session_id is None:
            return
        if current_timestamp - self._last_snapshot_ts < self._config.snapshot_interval_sec:
            return
        self._last_snapshot_ts = current_timestamp
        self._enqueue(
            "snapshot",
            balances=self._collect_balances(),
            available_balances=self._collect_available_balances(),
            marks=self._collect_marks(),
            ts=self._logical_ts(current_timestamp),
            basket_symbols=basket_symbols,
            quote_balance=quote_balance,
            lowest_ret_symbol=lowest_ret_symbol,
            pending_order_count=pending_order_count,
        )

    def on_signal_bin(
        self,
        bin_timestamp: pd.Timestamp,
        rets: pd.Series,
        ba_spread: pd.Series,
        mid_price: pd.Series,
        ret_processor: Any,
    ) -> None:
        if not self._started or not self._config.signal_enabled or self._session_id is None:
            return
        rows: List[Dict[str, Any]] = []
        connector = self._connectors.get(self._config.exchange)
        for symbol in self._config.symbols:
            if symbol not in rets.index:
                continue
            pair = self._pair_for_symbol(symbol)
            row: Dict[str, Any] = {
                "symbol": symbol,
                "ret": float(rets[symbol]) if pd.notna(rets[symbol]) else None,
                "ba_spread": float(ba_spread[symbol]) if symbol in ba_spread.index and pd.notna(ba_spread[symbol]) else None,
                "mid_price": float(mid_price[symbol]) if symbol in mid_price.index and pd.notna(mid_price[symbol]) else None,
            }
            if connector is not None and pair is not None:
                ask = self._safe_price(connector, pair, PriceType.BestAsk)
                bid = self._safe_price(connector, pair, PriceType.BestBid)
                ask_vol = self._safe_volume(connector, pair, "ask")
                bid_vol = self._safe_volume(connector, pair, "bid")
                row["ask_price"] = float(ask) if ask is not None else None
                row["bid_price"] = float(bid) if bid is not None else None
                row["ask_volume"] = float(ask_vol) if ask_vol is not None else None
                row["bid_volume"] = float(bid_vol) if bid_vol is not None else None
                mid_ret = ret_processor.mid_rets.get(symbol)
                if mid_ret is not None:
                    if ask is not None and ask > 0:
                        row["ret_ask"] = float(mid_ret.get_ret_for_price(float(ask)))
                    if bid is not None and bid > 0:
                        row["ret_bid"] = float(mid_ret.get_ret_for_price(float(bid)))
            rows.append(row)
        self._enqueue("signal_bin", bin_ts=bin_timestamp.to_pydatetime(), rows=rows)

    def on_rotation_started(
        self,
        sell_order_id: str,
        prev_symbol: str,
        new_symbol: str,
        transition_score: float,
        transition_value: float,
        sell_pair: str,
        buy_pair: str,
        metadata: Optional[dict] = None,
        status: str = "started",
        last_edge: Optional[float] = None,
        intended_sell_price: Optional[float] = None,
        intended_buy_price: Optional[float] = None,
        signal_mid_prev: Optional[float] = None,
        signal_mid_new: Optional[float] = None,
        ret_prev: Optional[float] = None,
        ret_new: Optional[float] = None,
        ba_spread_prev: Optional[float] = None,
        ba_spread_new: Optional[float] = None,
    ) -> None:
        if not self._started or self._session_id is None or not sell_order_id:
            return
        self._enqueue(
            "rotation_started",
            sell_order_id=sell_order_id,
            prev_symbol=prev_symbol,
            new_symbol=new_symbol,
            transition_score=transition_score,
            transition_value=transition_value,
            sell_pair=sell_pair,
            buy_pair=buy_pair,
            metadata=metadata or {},
            status=status,
            last_edge=last_edge,
            intended_sell_price=intended_sell_price,
            intended_buy_price=intended_buy_price,
            signal_mid_prev=signal_mid_prev,
            signal_mid_new=signal_mid_new,
            ret_prev=ret_prev,
            ret_new=ret_new,
            ba_spread_prev=ba_spread_prev,
            ba_spread_new=ba_spread_new,
            ts=self._logical_ts(),
        )

    def on_rotation_update(
        self,
        sell_order_id: str,
        status: Optional[str] = None,
        quote_earned: Optional[Decimal] = None,
        quote_spent: Optional[Decimal] = None,
        metadata: Optional[dict] = None,
        realized_edge: Optional[float] = None,
        sell_vwap: Optional[float] = None,
        buy_vwap: Optional[float] = None,
        sell_vwap_net: Optional[float] = None,
        buy_vwap_net: Optional[float] = None,
        total_fee_quote: Optional[float] = None,
        realized_edge_net: Optional[float] = None,
        sell_slippage: Optional[float] = None,
        buy_slippage: Optional[float] = None,
        latency_sec: Optional[float] = None,
    ) -> None:
        if not self._started or self._session_id is None or not sell_order_id:
            return
        self._enqueue(
            "rotation_update",
            sell_order_id=sell_order_id,
            status=status,
            quote_earned=quote_earned,
            quote_spent=quote_spent,
            metadata=metadata,
            realized_edge=realized_edge,
            sell_vwap=sell_vwap,
            buy_vwap=buy_vwap,
            sell_vwap_net=sell_vwap_net,
            buy_vwap_net=buy_vwap_net,
            total_fee_quote=total_fee_quote,
            realized_edge_net=realized_edge_net,
            sell_slippage=sell_slippage,
            buy_slippage=buy_slippage,
            latency_sec=latency_sec,
        )

    def on_fill(self, event: OrderFilledEvent, sell_order_id: Optional[str] = None) -> None:
        if not self._started or self._session_id is None:
            return
        self._enqueue(
            "fill",
            event=event,
            sell_order_id=sell_order_id,
            ts=self._event_ts(event.timestamp),
        )

    def log_event(self, event_type: str, payload: dict, level: str = "info") -> None:
        if not self._started or self._session_id is None:
            return
        self._enqueue("event_log", event_type=event_type, payload=payload, level=level, ts=self._logical_ts())

    def _enqueue(self, op: str, **payload: Any) -> None:
        try:
            self._queue.put_nowait((op, payload))
        except queue.Full:
            logger.warning("Pair-trade metrics queue full; dropping op=%s", op)

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            op, payload = item
            try:
                handler: Optional[Callable[[dict], None]] = getattr(self, f"_handle_{op}", None)
                if handler is not None:
                    handler(payload)
            except Exception:
                logger.exception("Pair-trade metrics write failed for op=%s", op)

    def _handle_start_session(self, payload: dict) -> None:
        if self._repo is None:
            self._session_init_enqueued = False
            return
        cfg = self._config
        try:
            strategy_id = self._repo.ensure_strategy(cfg.strategy_name, cfg.strategy_type, cfg.config_file)
            session_id = self._repo.start_session(
                strategy_id=strategy_id,
                quote_currency=cfg.quote_currency,
                balances=payload["balances"],
                metadata=payload.get("metadata"),
            )
            self._strategy_id = strategy_id
            self._session_id = session_id
            self._session_start_value = self._repo.get_session_start_value(
                session_id,
                cfg.quote_currency,
                cfg.asset_marks(),
                payload["marks"],
            )
        except Exception:
            self._session_init_enqueued = False
            raise
        logger.info(
            "Pair-trade metrics session started: strategy_id=%s session_id=%s start_value=%s %s",
            self._strategy_id,
            self._session_id,
            self._session_start_value,
            cfg.quote_currency,
        )

    def _handle_end_session(self, _payload: dict) -> None:
        if self._session_id is not None and self._repo is not None:
            self._repo.end_session(self._session_id)

    def _handle_snapshot(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        ts = payload["ts"]
        balances = payload["balances"]
        marks = payload["marks"]
        available = payload.get("available_balances", {})
        cfg = self._config
        portfolio_value = value_portfolio(balances, cfg.asset_marks(), marks, cfg.quote_currency)
        pnl = None
        if self._session_start_value is not None:
            pnl = portfolio_value - self._session_start_value
        # P&L lives only in portfolio_snapshot (not duplicated on basket_snapshot).
        # net_exposure is empty for long-only ETF baskets — use basket symbols + balance_snapshot.
        result = PortfolioSnapshotResult(
            portfolio_value=portfolio_value,
            pnl=pnl,
            net_exposure={},
            marks=marks,
        )
        self._repo.insert_balance_snapshot(self._session_id, ts, balances, available)
        self._repo.insert_portfolio_snapshot(self._session_id, ts, result)
        self._repo.insert_basket_snapshot(
            session_id=self._session_id,
            ts=ts,
            symbols=payload.get("basket_symbols", []),
            quote_balance=dec(payload.get("quote_balance")),
            pending_order_count=int(payload.get("pending_order_count", 0)),
            lowest_ret_symbol=payload.get("lowest_ret_symbol"),
        )

    def _handle_signal_bin(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        self._repo.insert_signal_rows(
            session_id=self._session_id,
            bin_ts=payload["bin_ts"],
            rows=payload["rows"],
        )

    def _handle_rotation_started(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        self._repo.insert_rotation(
            session_id=self._session_id,
            ts=payload["ts"],
            prev_symbol=payload["prev_symbol"],
            new_symbol=payload["new_symbol"],
            transition_score=dec(payload.get("transition_score")) if payload.get("transition_score") is not None else None,
            transition_value=dec(payload.get("transition_value")) if payload.get("transition_value") is not None else None,
            sell_pair=payload.get("sell_pair"),
            buy_pair=payload.get("buy_pair"),
            sell_order_id=payload.get("sell_order_id"),
            status=payload.get("status") or "started",
            quote_earned=Decimal("0"),
            quote_spent=Decimal("0"),
            metadata=payload.get("metadata"),
            last_edge=_opt_dec(payload.get("last_edge")),
            intended_sell_price=_opt_dec(payload.get("intended_sell_price")),
            intended_buy_price=_opt_dec(payload.get("intended_buy_price")),
            signal_mid_prev=_opt_dec(payload.get("signal_mid_prev")),
            signal_mid_new=_opt_dec(payload.get("signal_mid_new")),
            ret_prev=_opt_dec(payload.get("ret_prev")),
            ret_new=_opt_dec(payload.get("ret_new")),
            ba_spread_prev=_opt_dec(payload.get("ba_spread_prev")),
            ba_spread_new=_opt_dec(payload.get("ba_spread_new")),
        )

    def _handle_rotation_update(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        self._repo.update_rotation(
            session_id=self._session_id,
            sell_order_id=payload["sell_order_id"],
            status=payload.get("status"),
            quote_earned=payload.get("quote_earned"),
            quote_spent=payload.get("quote_spent"),
            metadata=payload.get("metadata"),
            realized_edge=_opt_dec(payload.get("realized_edge")),
            sell_vwap=_opt_dec(payload.get("sell_vwap")),
            buy_vwap=_opt_dec(payload.get("buy_vwap")),
            sell_vwap_net=_opt_dec(payload.get("sell_vwap_net")),
            buy_vwap_net=_opt_dec(payload.get("buy_vwap_net")),
            total_fee_quote=_opt_dec(payload.get("total_fee_quote")),
            realized_edge_net=_opt_dec(payload.get("realized_edge_net")),
            sell_slippage=_opt_dec(payload.get("sell_slippage")),
            buy_slippage=_opt_dec(payload.get("buy_slippage")),
            latency_sec=_opt_dec(payload.get("latency_sec")),
        )

    def _handle_fill(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        event: OrderFilledEvent = payload["event"]
        ts: datetime = payload["ts"]
        sell_order_id = payload.get("sell_order_id")
        rotation_id = None
        if sell_order_id:
            rotation_id = self._repo.get_rotation_id(self._session_id, sell_order_id)
        fill_key = event.exchange_trade_id or f"{event.order_id}:{event.timestamp}"
        fee_q = fee_to_quote(event.trade_fee, dec(event.price), dec(event.amount), self._config.quote_currency, None)
        self._repo.insert_fill(
            session_id=self._session_id,
            ts=ts,
            rotation_id=rotation_id,
            order_id=event.order_id,
            trading_pair=event.trading_pair,
            side=event.trade_type.name,
            amount=dec(event.amount),
            price=dec(event.price),
            fee_quote=fee_q,
            exchange_trade_id=event.exchange_trade_id or None,
            fill_key=fill_key,
        )

    def _handle_event_log(self, payload: dict) -> None:
        if self._session_id is None or self._repo is None:
            return
        self._repo.insert_event_log(
            session_id=self._session_id,
            event_type=payload["event_type"],
            payload=payload["payload"],
            level=payload.get("level", "info"),
            ts=payload.get("ts"),
        )

    def _pair_for_symbol(self, symbol: str) -> Optional[str]:
        for pair, strat_symbol in self._config.hb_to_strat_map.items():
            if strat_symbol == symbol:
                return pair
        return None

    def _required_pairs_ready(self, marks: Dict[str, Decimal]) -> bool:
        for pair in self._config.trading_pairs:
            if pair not in marks:
                return False
        return True

    def _collect_balances(self) -> Dict[str, Dict[str, Decimal]]:
        balances: Dict[str, Dict[str, Decimal]] = {}
        connector = self._connectors.get(self._config.exchange)
        if connector is None:
            return balances
        exchange_balances: Dict[str, Decimal] = {}
        for asset in self._config.tracked_assets():
            total = connector.get_balance(asset)
            if total is None:
                continue
            total_dec = dec(total)
            if total_dec != 0 or asset == self._config.quote_currency:
                exchange_balances[asset] = total_dec
        if exchange_balances:
            balances[self._config.exchange] = exchange_balances
        return balances

    def _collect_available_balances(self) -> Dict[str, Dict[str, Decimal]]:
        balances: Dict[str, Dict[str, Decimal]] = {}
        connector = self._connectors.get(self._config.exchange)
        if connector is None:
            return balances
        exchange_balances: Dict[str, Decimal] = {}
        for asset in self._config.tracked_assets():
            available = connector.get_available_balance(asset)
            if available is None:
                continue
            exchange_balances[asset] = dec(available)
        if exchange_balances:
            balances[self._config.exchange] = exchange_balances
        return balances

    def _collect_marks(self) -> Dict[str, Decimal]:
        marks: Dict[str, Decimal] = {}
        connector = self._connectors.get(self._config.exchange)
        if connector is None:
            return marks
        for pair in self._config.trading_pairs:
            mid = self._safe_price(connector, pair, PriceType.MidPrice)
            if mid is not None and mid > 0:
                marks[pair] = mid
        return marks

    @staticmethod
    def _safe_price(connector: ConnectorBase, trading_pair: str, price_type: PriceType) -> Optional[Decimal]:
        try:
            price = connector.get_price_by_type(trading_pair, price_type)
        except (ValueError, KeyError, AttributeError):
            return None
        if price is None:
            return None
        return dec(price)

    @staticmethod
    def _safe_volume(connector: ConnectorBase, trading_pair: str, side: str) -> Optional[Decimal]:
        try:
            book = connector.get_order_book(trading_pair)
            snapshot = book.snapshot
            if side == "ask":
                row = snapshot[1].iloc[0]
            else:
                row = snapshot[0].iloc[0]
            return dec(row.amount)
        except (ValueError, KeyError, AttributeError, IndexError):
            return None

    @staticmethod
    def _event_ts(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    def _logical_ts(self, current_timestamp: Optional[float] = None) -> datetime:
        if current_timestamp is not None:
            self._last_logical_ts = current_timestamp
        ts = getattr(self, "_last_logical_ts", None)
        if ts is None:
            return utc_now()  # live before first tick
        return datetime.fromtimestamp(ts, tz=timezone.utc)
