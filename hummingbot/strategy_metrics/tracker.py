from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.event.events import MarketOrderFailureEvent, OrderFilledEvent
from hummingbot.strategy_metrics.config import MetricsConfig
from hummingbot.strategy_metrics.db import MetricsDatabase
from hummingbot.strategy_metrics.repository import (
    MetricsRepository,
    PortfolioSnapshotResult,
    compute_round_trip,
    dec,
    fee_to_quote,
    net_exposure,
    utc_now,
    value_portfolio,
)

logger = logging.getLogger(__name__)


@dataclass
class PendingMakerFill:
    maker_fill_id: int
    maker_fill_key: str
    ts: datetime
    side: str
    amount: Decimal
    price_quote: Decimal
    fee_quote: Decimal


@dataclass
class PendingHedge:
    hedge_event_id: int
    maker_fill_id: int
    maker_side: str
    maker_amount: Decimal
    maker_price_quote: Decimal
    maker_fee_quote: Decimal
    maker_fill_ts: datetime
    requested_amount: Decimal


class CrossExchangeMmMetricsTracker:
    """
    Logs cross-exchange market making metrics to PostgreSQL.

    Writes are queued on a background thread so the strategy tick loop stays fast.
    """

    def __init__(self, config: MetricsConfig, connectors: Dict[str, ConnectorBase]):
        self._config = config
        self._connectors = connectors
        self._db: Optional[MetricsDatabase] = None
        self._repo: Optional[MetricsRepository] = None
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="mm-metrics-writer", daemon=True)
        self._session_id: Optional[int] = None
        self._strategy_id: Optional[int] = None
        self._session_start_value: Optional[Decimal] = None
        self._last_snapshot_ts: float = 0.0
        self._pending_maker: Optional[PendingMakerFill] = None
        self._pending_hedges: Dict[str, PendingHedge] = {}
        self._started = False
        self._session_init_enqueued = False

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
            self._repo = MetricsRepository(self._db)
            return self._db.ping()
        except Exception as exc:
            logger.warning("Metrics database unavailable: %s", exc)
            self._db = None
            self._repo = None
            return False

    def start(self) -> None:
        if not self.enabled:
            logger.info("Strategy metrics disabled or MM_METRICS_DATABASE_URL not set.")
            return
        if not self._ensure_db():
            logger.warning("Strategy metrics disabled: cannot reach PostgreSQL.")
            return
        if not self._worker.is_alive():
            self._worker.start()
        self._started = True
        logger.info(
            "Strategy metrics writer started for %s (session opens when market data is ready).",
            self._config.strategy_name,
        )

    def maybe_initialize(self, current_timestamp: float) -> None:
        """Open a metrics session once connectors have order book data."""
        if not self._started or self._session_id is not None or self._session_init_enqueued:
            return
        marks = self._collect_marks()
        required_pairs = {self._config.hedge_trading_pair}
        if self._config.fx_trading_pair:
            required_pairs.add(self._config.fx_trading_pair)
        if not required_pairs.issubset(marks.keys()):
            return
        self._session_init_enqueued = True
        self._last_snapshot_ts = current_timestamp
        metadata = {
            "maker_connector": self._config.maker_connector,
            "hedge_connector": self._config.hedge_connector,
            "maker_trading_pair": self._config.maker_trading_pair,
            "hedge_trading_pair": self._config.hedge_trading_pair,
            "fx_trading_pair": self._config.fx_trading_pair,
        }
        self._enqueue(
            "start_session",
            balances=self._collect_balances(),
            marks=marks,
            metadata=metadata,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._enqueue("end_session")
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=5)
        self._started = False

    def maybe_snapshot(self, current_timestamp: float) -> None:
        if not self._started:
            return
        self.maybe_initialize(current_timestamp)
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
            ts=utc_now(),
        )

    def on_maker_fill(
        self,
        event: OrderFilledEvent,
        fair_price: Optional[Decimal] = None,
        fx_rate: Optional[Decimal] = None,
        price_quote: Optional[Decimal] = None,
    ) -> None:
        if not self._started:
            return
        self._enqueue(
            "maker_fill",
            event=event,
            fair_price=fair_price,
            fx_rate=fx_rate,
            price_quote=price_quote,
            ts=self._event_ts(event.timestamp),
        )

    def on_hedge_placed(
        self,
        *,
        order_id: Optional[str],
        side: str,
        requested_amount: Decimal,
        limit_price: Optional[Decimal],
        fail_reason: Optional[str] = None,
    ) -> None:
        if not self._started:
            return
        status = "failed" if fail_reason else "placed"
        self._enqueue(
            "hedge_placed",
            order_id=order_id,
            side=side,
            requested_amount=requested_amount,
            limit_price=limit_price,
            status=status,
            fail_reason=fail_reason,
            ts=utc_now(),
        )

    def on_hedge_fill(self, event: OrderFilledEvent) -> None:
        if not self._started:
            return
        self._enqueue("hedge_fill", event=event, ts=self._event_ts(event.timestamp))

    def on_hedge_failed(self, event: MarketOrderFailureEvent) -> None:
        if not self._started:
            return
        self._enqueue(
            "hedge_failed",
            order_id=event.order_id,
            fail_reason=event.error_message or event.error_type or "unknown",
            ts=self._event_ts(event.timestamp),
        )

    def log_event(self, event_type: str, payload: dict, level: str = "info") -> None:
        if not self._started or self._session_id is None:
            return
        self._enqueue("event_log", event_type=event_type, payload=payload, level=level, ts=utc_now())

    def _enqueue(self, op: str, **payload: Any) -> None:
        try:
            self._queue.put_nowait((op, payload))
        except queue.Full:
            logger.warning("Metrics queue full; dropping op=%s", op)

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
                logger.exception("Metrics write failed for op=%s", op)

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
            "Metrics session started: strategy_id=%s session_id=%s start_value=%s %s",
            self._strategy_id,
            self._session_id,
            self._session_start_value,
            cfg.quote_currency,
        )

    def _handle_end_session(self, _payload: dict) -> None:
        if self._session_id is not None:
            self._repo.end_session(self._session_id)

    def _handle_snapshot(self, payload: dict) -> None:
        if self._session_id is None:
            return
        ts = payload["ts"]
        balances = payload["balances"]
        marks = payload["marks"]
        available = payload.get("available_balances", {})
        asset_marks = self._config.asset_marks()
        portfolio_value = value_portfolio(balances, asset_marks, marks, self._config.quote_currency)
        pnl = None
        if self._session_start_value is not None:
            pnl = portfolio_value - self._session_start_value
        tracked_assets = {self._config.base_asset, self._config.quote_currency, self._config.maker_quote_asset}
        result = PortfolioSnapshotResult(
            portfolio_value=portfolio_value,
            pnl=pnl,
            net_exposure=net_exposure(balances, tracked_assets),
            marks=marks,
        )
        self._repo.insert_balance_snapshot(self._session_id, ts, balances, available)
        self._repo.insert_portfolio_snapshot(self._session_id, ts, result)

    def _handle_maker_fill(self, payload: dict) -> None:
        if self._session_id is None:
            return
        event: OrderFilledEvent = payload["event"]
        ts: datetime = payload["ts"]
        fx_rate = payload.get("fx_rate")
        price_quote = payload.get("price_quote")
        fair_price = payload.get("fair_price")
        cfg = self._config
        base, quote = cfg.maker_trading_pair.split("-")
        maker_fill_key = event.exchange_trade_id or f"{event.order_id}:{event.timestamp}"
        fee_q = fee_to_quote(
            event.trade_fee,
            dec(price_quote),
            dec(event.amount),
            cfg.quote_currency,
            dec(fx_rate) if fx_rate is not None else None,
        )
        maker_fill_id = self._repo.insert_maker_fill(
            session_id=self._session_id,
            ts=ts,
            exchange=cfg.maker_connector,
            trading_pair=cfg.maker_trading_pair,
            base_asset=base,
            quote_asset=quote,
            side=event.trade_type.name,
            amount=dec(event.amount),
            price=dec(event.price),
            price_quote=dec(price_quote) if price_quote is not None else None,
            fx_rate=dec(fx_rate) if fx_rate is not None else None,
            fair_price=dec(fair_price) if fair_price is not None else None,
            fee_quote=fee_q,
            exchange_trade_id=event.exchange_trade_id or None,
            order_id=event.order_id,
            maker_fill_key=maker_fill_key,
        )
        self._pending_maker = PendingMakerFill(
            maker_fill_id=maker_fill_id,
            maker_fill_key=maker_fill_key,
            ts=ts,
            side=event.trade_type.name,
            amount=dec(event.amount),
            price_quote=dec(price_quote) if price_quote is not None else Decimal("0"),
            fee_quote=fee_q,
        )

    def _handle_hedge_placed(self, payload: dict) -> None:
        if self._session_id is None:
            return
        pending_maker = self._pending_maker
        cfg = self._config
        order_id = payload.get("order_id")
        order_id_key = f"{order_id}:placed" if order_id else f"failed:{payload['ts'].timestamp()}"
        hedge_event_id = self._repo.insert_hedge_event(
            session_id=self._session_id,
            maker_fill_id=pending_maker.maker_fill_id if pending_maker else None,
            ts=payload["ts"],
            exchange=cfg.hedge_connector,
            trading_pair=cfg.hedge_trading_pair,
            base_asset=cfg.base_asset,
            side=payload["side"],
            requested_amount=dec(payload["requested_amount"]),
            filled_amount=Decimal("0"),
            limit_price=dec(payload["limit_price"]) if payload.get("limit_price") is not None else None,
            fill_price=None,
            order_id=order_id,
            status=payload["status"],
            fail_reason=payload.get("fail_reason"),
            latency_ms=None,
            order_id_key=order_id_key,
        )
        if order_id and pending_maker and payload["status"] == "placed":
            self._pending_hedges[order_id] = PendingHedge(
                hedge_event_id=hedge_event_id,
                maker_fill_id=pending_maker.maker_fill_id,
                maker_side=pending_maker.side,
                maker_amount=pending_maker.amount,
                maker_price_quote=pending_maker.price_quote,
                maker_fee_quote=pending_maker.fee_quote,
                maker_fill_ts=pending_maker.ts,
                requested_amount=dec(payload["requested_amount"]),
            )
        if payload["status"] == "failed" and pending_maker:
            self._insert_unhedged_round_trip(pending_maker, payload.get("fail_reason") or "hedge_not_placed")

    def _handle_hedge_fill(self, payload: dict) -> None:
        if self._session_id is None:
            return
        event: OrderFilledEvent = payload["event"]
        ts: datetime = payload["ts"]
        cfg = self._config
        pending = self._pending_hedges.get(event.order_id)
        fill_price = dec(event.price)
        filled_amount = dec(event.amount)
        hedge_fee = fee_to_quote(event.trade_fee, fill_price, filled_amount, cfg.quote_currency, None)
        latency_ms = None
        if pending is not None:
            latency_ms = int((ts - pending.maker_fill_ts).total_seconds() * 1000)

        hedge_event_id = self._repo.insert_hedge_event(
            session_id=self._session_id,
            maker_fill_id=pending.maker_fill_id if pending else None,
            ts=ts,
            exchange=cfg.hedge_connector,
            trading_pair=cfg.hedge_trading_pair,
            base_asset=cfg.base_asset,
            side=event.trade_type.name,
            requested_amount=pending.requested_amount if pending else filled_amount,
            filled_amount=filled_amount,
            limit_price=None,
            fill_price=fill_price,
            order_id=event.order_id,
            status="filled",
            fail_reason=None,
            latency_ms=latency_ms,
            order_id_key=f"{event.order_id}:filled",
        )

        if pending is None or pending.maker_price_quote <= 0:
            return

        matched_amount = min(pending.maker_amount, filled_amount)
        total_fees = pending.maker_fee_quote + hedge_fee
        gross, spread_bps, net = compute_round_trip(
            pending.maker_side,
            matched_amount,
            pending.maker_price_quote,
            fill_price,
            total_fees,
        )
        if filled_amount + Decimal("1e-12") < pending.maker_amount:
            status = "partial"
        else:
            status = "matched"

        self._repo.insert_round_trip(
            session_id=self._session_id,
            ts=ts,
            base_asset=cfg.base_asset,
            maker_fill_id=pending.maker_fill_id,
            hedge_event_id=hedge_event_id,
            amount=matched_amount,
            maker_price_quote=pending.maker_price_quote,
            hedge_price_quote=fill_price,
            spread_bps=spread_bps,
            gross_pnl_quote=gross,
            fees_quote=total_fees,
            net_pnl_quote=net,
            status=status,
        )
        self._pending_hedges.pop(event.order_id, None)
        if status == "partial":
            remaining = pending.maker_amount - filled_amount
            self._repo.insert_round_trip(
                session_id=self._session_id,
                ts=ts,
                base_asset=cfg.base_asset,
                maker_fill_id=pending.maker_fill_id,
                hedge_event_id=hedge_event_id,
                amount=remaining,
                maker_price_quote=pending.maker_price_quote,
                hedge_price_quote=fill_price,
                spread_bps=None,
                gross_pnl_quote=Decimal("0"),
                fees_quote=Decimal("0"),
                net_pnl_quote=Decimal("0"),
                status="unhedged",
            )

    def _handle_hedge_failed(self, payload: dict) -> None:
        if self._session_id is None:
            return
        order_id = payload["order_id"]
        pending = self._pending_hedges.pop(order_id, None)
        cfg = self._config
        self._repo.insert_hedge_event(
            session_id=self._session_id,
            maker_fill_id=pending.maker_fill_id if pending else None,
            ts=payload["ts"],
            exchange=cfg.hedge_connector,
            trading_pair=cfg.hedge_trading_pair,
            base_asset=cfg.base_asset,
            side="UNKNOWN",
            requested_amount=pending.requested_amount if pending else Decimal("0"),
            filled_amount=Decimal("0"),
            limit_price=None,
            fill_price=None,
            order_id=order_id,
            status="failed",
            fail_reason=payload.get("fail_reason"),
            latency_ms=None,
            order_id_key=f"{order_id}:failed",
        )
        if pending is not None and self._pending_maker is not None:
            self._insert_unhedged_round_trip(self._pending_maker, payload.get("fail_reason") or "hedge_failed")

    def _handle_event_log(self, payload: dict) -> None:
        if self._session_id is None:
            return
        self._repo.insert_event_log(
            session_id=self._session_id,
            event_type=payload["event_type"],
            payload=payload["payload"],
            level=payload.get("level", "info"),
            ts=payload.get("ts"),
        )

    def _insert_unhedged_round_trip(self, pending_maker: PendingMakerFill, reason: str) -> None:
        if self._session_id is None:
            return
        self._repo.insert_round_trip(
            session_id=self._session_id,
            ts=utc_now(),
            base_asset=self._config.base_asset,
            maker_fill_id=pending_maker.maker_fill_id,
            hedge_event_id=None,
            amount=pending_maker.amount,
            maker_price_quote=pending_maker.price_quote,
            hedge_price_quote=Decimal("0"),
            spread_bps=None,
            gross_pnl_quote=Decimal("0"),
            fees_quote=pending_maker.fee_quote,
            net_pnl_quote=Decimal("0"),
            status="unhedged",
        )
        self._repo.insert_event_log(
            session_id=self._session_id,
            event_type="unhedged_maker_fill",
            payload={"maker_fill_id": pending_maker.maker_fill_id, "reason": reason},
            level="warning",
        )

    def _collect_balances(self) -> Dict[str, Dict[str, Decimal]]:
        balances: Dict[str, Dict[str, Decimal]] = {}
        for connector_name in self._config.connectors():
            connector = self._connectors.get(connector_name)
            if connector is None:
                continue
            exchange_balances: Dict[str, Decimal] = {}
            for asset in self._tracked_assets():
                total = connector.get_balance(asset)
                if total is None:
                    continue
                total_dec = dec(total)
                if total_dec != 0 or asset in (self._config.base_asset, self._config.quote_currency, self._config.maker_quote_asset):
                    exchange_balances[asset] = total_dec
            if exchange_balances:
                balances[connector_name] = exchange_balances
        return balances

    def _collect_available_balances(self) -> Dict[str, Dict[str, Decimal]]:
        balances: Dict[str, Dict[str, Decimal]] = {}
        for connector_name in self._config.connectors():
            connector = self._connectors.get(connector_name)
            if connector is None:
                continue
            exchange_balances: Dict[str, Decimal] = {}
            for asset in self._tracked_assets():
                available = connector.get_available_balance(asset)
                if available is None:
                    continue
                exchange_balances[asset] = dec(available)
            if exchange_balances:
                balances[connector_name] = exchange_balances
        return balances

    def _collect_marks(self) -> Dict[str, Decimal]:
        marks: Dict[str, Decimal] = {}
        pair_sources = {
            self._config.hedge_trading_pair: self._config.hedge_connector,
        }
        if self._config.fx_trading_pair:
            pair_sources[self._config.fx_trading_pair] = self._config.maker_connector
        for pair, connector_name in pair_sources.items():
            connector = self._connectors.get(connector_name)
            if connector is None:
                continue
            mid = self._safe_mid_price(connector, pair)
            if mid is not None and mid > 0:
                marks[pair] = mid
        return marks

    @staticmethod
    def _safe_mid_price(connector: ConnectorBase, trading_pair: str) -> Optional[Decimal]:
        order_books = getattr(getattr(connector, "order_book_tracker", None), "order_books", None)
        if order_books is not None and trading_pair not in order_books:
            return None
        try:
            mid = connector.get_mid_price(trading_pair)
        except (ValueError, KeyError, AttributeError):
            return None
        if mid is None:
            return None
        return dec(mid)

    def _tracked_assets(self) -> set[str]:
        assets = {
            self._config.base_asset,
            self._config.quote_currency,
            self._config.maker_quote_asset,
        }
        if self._config.fx_trading_pair:
            fx_base, fx_quote = self._config.fx_trading_pair.split("-")
            assets.add(fx_base)
            assets.add(fx_quote)
        return assets

    @staticmethod
    def _event_ts(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
