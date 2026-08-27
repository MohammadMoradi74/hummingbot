import json
import os
import shutil
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd
from pair_trade.historic_data import download_daily, get_gold_etf_symbols, get_gold_etfs_info, load_orderbook
from pair_trade.portfo_calculator import PortfolioCalculator  # noqa: F401 — kept for parity with original
from pair_trade.return_calculator import MultiAssetMidRetProcessor
from pair_trade.transition_tracker import TransitionTracker
from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction, PriceType
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderExpiredEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    TradeType,
)
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase, s_decimal_nan
from hummingbot.strategy_metrics import PairTradeMetricsConfig, PairTradeMetricsTracker
from hummingbot.strategy_metrics.repository import compute_log_slippage, compute_realized_edge, fee_to_quote


def _build_gold_etf_config():
    symbols = get_gold_etf_symbols()
    info = get_gold_etfs_info().loc[symbols]
    hb_to_strat = {f"{info.loc[s, 'instrument_code_12']}-IRR": s for s in symbols}
    return symbols, list(hb_to_strat.keys()), hb_to_strat


_GOLD_SYMBOLS, _DEFAULT_TRADING_PAIRS, _HB_TO_STRAT_MAP = _build_gold_etf_config()
MOBIN_MIN_NOTIONAL = Decimal("1000000")  # real exchange floor; connector keeps 1 for dust sells


class GoldEtfStatisticalArbitrageConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = []

    exchange: str = Field(
        default="mock",
        json_schema_extra={
            "prompt": "Exchange where the bot will trade",
            "prompt_on_new": True,
        },
    )

    trading_pairs: List[str] = Field(
        default_factory=lambda: list(_DEFAULT_TRADING_PAIRS),
        json_schema_extra={
            "prompt": "Trading pairs in which the bot will place orders (comma-separated)",
            "prompt_on_new": True,
        },
    )

    current_date: str = Field(
        default="2025-02-11",
        json_schema_extra={
            "prompt": "Current date for the return calculator",
            "prompt_on_new": True,
        },
    )

    n: int = Field(
        default=30,
        json_schema_extra={
            "prompt": "Number of days to get data for",
            "prompt_on_new": True,
        },
    )

    half_life: str = Field(
        default="5d",
        json_schema_extra={
            "prompt": "Halflife for the return calculator",
            "prompt_on_new": True,
        },
    )

    freq: str = Field(
        default="5s",
        json_schema_extra={
            "prompt": "Frequency for the return calculator",
            "prompt_on_new": True,
        },
    )

    start_time: str = Field(
        default="08:32:00",
        json_schema_extra={
            "prompt": "Start time for the return calculator",
            "prompt_on_new": True,
        },
    )

    end_time: str = Field(
        default="14:28:00",
        json_schema_extra={
            "prompt": "End time for the return calculator",
            "prompt_on_new": True,
        },
    )

    compute_level: int = Field(
        default=1,
        json_schema_extra={
            "prompt": "Compute level for the return calculator",
            "prompt_on_new": True,
        },
    )

    n_slots: int = Field(
        default=3,
        json_schema_extra={
            "prompt": "Number of slots for the portfolio calculator",
            "prompt_on_new": True,
        },
    )

    base_threshold: float = Field(
        default=0.003,
        json_schema_extra={
            "prompt": "Minimum transition score margin to trigger a rotation",
            "prompt_on_new": True,
        },
    )

    order_timeout_s: float = Field(
        default=30.0,
        json_schema_extra={
            "prompt": "Timeout (seconds) before cancelling open orders",
            "prompt_on_new": True,
        },
    )

    buy_slippage_bps: float = Field(
        default=30.0,
        json_schema_extra={
            "prompt": "Initial slippage for aggressive buy limit orders (bps)",
            "prompt_on_new": True,
        },
    )

    buy_slippage_step_bps: float = Field(
        default=20.0,
        json_schema_extra={
            "prompt": "Extra slippage per buy retry (bps)",
            "prompt_on_new": True,
        },
    )

    max_buy_retries: int = Field(
        default=10,
        json_schema_extra={
            "prompt": "Max retries for buy after sell",
            "prompt_on_new": True,
        },
    )

    min_quote_to_spend: float = Field(
        default=0.0,
        json_schema_extra={
            "prompt": "Minimum quote amount to attempt buying (set > 0 to avoid dust)",
            "prompt_on_new": True,
        },
    )

    spend_factor: float = Field(
        default=0.995,
        json_schema_extra={
            "prompt": "Spend factor on proceeds (e.g. 0.995 for fees/slippage buffer)",
            "prompt_on_new": True,
        },
    )

    state_dir: str = Field(
        default="",  # empty → auto: data/gold_etf_statistical_arbitrage/states/{trading_day}
        json_schema_extra={
            "prompt": "Checkpoint directory (empty = data/gold_etf_statistical_arbitrage/states/YYYY-MM-DD)",
            "prompt_on_new": False,
        },
    )

    save_every_ticks: int = Field(
        default=1000,
        json_schema_extra={
            "prompt": "Save checkpoint every N on_tick calls (0 = disable periodic)",
            "prompt_on_new": False,
        },
    )

    resume: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "If meta.json exists for today, resume instead of cold warm-up",
            "prompt_on_new": False,
        },
    )

    metrics_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Log performance metrics to PostgreSQL",
            "prompt_on_new": True,
        },
    )

    metrics_strategy_name: str = Field(
        default="gold_etf_statistical_arbitrage",
        json_schema_extra={
            "prompt": "Strategy name in metrics DB (unique per instance)",
            "prompt_on_new": True,
        },
    )

    metrics_snapshot_interval_sec: int = Field(
        default=60,
        json_schema_extra={
            "prompt": "Balance snapshot interval (seconds)",
            "prompt_on_new": True,
        },
    )

    metrics_signal_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Log per-symbol signal rows every freq bin",
            "prompt_on_new": False,
        },
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.exchange] = markets.get(self.exchange, set()) | set(self.trading_pairs)
        # Mock connector playback needs dataset dates, and those dates are currently encoded inside
        # `conf/connectors/mock.yml`. To keep "one change" behavior, sync that connector config from
        # this strategy's `current_date` at init time (runs before connectors are instantiated).
        if self.exchange == "mock":
            try:
                import logging
                import re

                import yaml

                from hummingbot.client.settings import CONNECTORS_CONF_DIR_PATH

                mock_conf_path = CONNECTORS_CONF_DIR_PATH / "mock.yml"
                if mock_conf_path.exists():
                    with open(mock_conf_path, "r") as f:
                        mock_cfg = yaml.safe_load(f) or {}

                    desired_date = str(self.current_date)
                    original_cfg = dict(mock_cfg)

                    # Paths like: /.../2026-08-18/live_orderbook.parquet → /.../{current_date}/...
                    for path_key in ("orderbook_path", "trade_path"):
                        path_value = mock_cfg.get(path_key)
                        if isinstance(path_value, str) and path_value:
                            mock_cfg[path_key] = re.sub(
                                r"/\d{4}-\d{2}-\d{2}/",
                                f"/{desired_date}/",
                                path_value,
                                count=1,
                            )

                    # Timestamps like: 2026-08-18 08:31:30 → {current_date} 08:31:30
                    for ts_key in ("playback_start", "playback_end"):
                        ts_value = mock_cfg.get(ts_key)
                        if isinstance(ts_value, str) and ts_value:
                            mock_cfg[ts_key] = re.sub(
                                r"\d{4}-\d{2}-\d{2}",
                                desired_date,
                                ts_value,
                                count=1,
                            )

                    if mock_cfg != original_cfg:
                        with open(mock_conf_path, "w") as f:
                            yaml.safe_dump(mock_cfg, f, sort_keys=False, allow_unicode=True)
            except Exception:
                logging.getLogger(__name__).warning("Failed to sync mock.yml playback date from strategy.")

        return markets


def save_checkpoint(strategy, state_dir: str, last_ts_ms: int):
    """Atomically write processor JSON + resume cursor."""
    staging = f"{state_dir}.staging"
    backup = f"{state_dir}.bak"

    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    strategy.ret_processor.save(staging)
    meta = {
        "last_ts_ms": int(last_ts_ms),
        "previous_freq_ts": strategy.ret_processor._previous_freq_timestamp,
    }
    meta_tmp = os.path.join(staging, "meta.json.tmp")
    meta_path = os.path.join(staging, "meta.json")
    with open(meta_tmp, "w") as f:
        json.dump(meta, f)
    os.replace(meta_tmp, meta_path)

    if os.path.isdir(backup):
        shutil.rmtree(backup)
    if os.path.isdir(state_dir):
        os.rename(state_dir, backup)
    os.rename(staging, state_dir)
    if os.path.isdir(backup):
        shutil.rmtree(backup)


class GoldEtfStatisticalArbitrage(StrategyV2Base):
    markets = {}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: GoldEtfStatisticalArbitrageConfig):
        super().__init__(connectors, config)
        self.config = config

        self.hb_to_strat_map = dict(_HB_TO_STRAT_MAP)
        self.strat_to_hb_map = {v: k for k, v in self.hb_to_strat_map.items()}
        self.symbols = list(self.hb_to_strat_map.values())

        if self.config.exchange not in self.connectors:
            raise ValueError(
                f"Connector '{self.config.exchange}' not found in connectors."
                f"Available: {list(self.connectors.keys())}"
            )

        if self.config.exchange in {"mock", "mobin_mock"}:
            trading_day = self.config.current_date
        else:
            trading_day = date.today().strftime("%Y-%m-%d")

        self.state_dir = (
            self.config.state_dir
            or f"data/gold_etf_statistical_arbitrage/states/{trading_day}"
        )
        meta_path = os.path.join(self.state_dir, "meta.json")
        self._tick_counter = 0
        self._resume_after_ts_ms: Optional[int] = None

        resuming = bool(self.config.resume) and os.path.exists(meta_path)

        if resuming:
            self.logger().info(f"Resuming from {meta_path}")
            with open(meta_path) as f:
                meta = json.load(f)

            self.ret_processor = MultiAssetMidRetProcessor(
                self.symbols,
                None,
                None,
                config.half_life,
                config.freq,
                config.start_time,
                config.end_time,
                config.compute_level,
            )
            self.ret_processor.load(self.state_dir)
            if meta.get("previous_freq_ts") is not None:
                self.ret_processor._previous_freq_timestamp = meta["previous_freq_ts"]

            self._resume_after_ts_ms = int(meta["last_ts_ms"])
        else:
            self.logger().info(f"Cold start for {trading_day}")
            if self.config.exchange in {"mock", "mobin_mock"}:
                data_dir = f"data/live-test/{self.config.current_date}/"
                archive_orderbook = pd.read_parquet(os.path.join(data_dir, "archive_orderbook.parquet"))
                archive_orderbook = archive_orderbook.loc[
                    archive_orderbook.index.to_frame()["symbol"].isin(self.symbols)
                ]
                daily = pd.read_parquet(os.path.join(data_dir, "archive_daily.parquet"))
                daily = daily.loc[daily.index.to_frame()["symbol"].isin(self.symbols)]
            else:
                current_date = date.today().strftime("%Y-%m-%d")
                date_30_days_ago = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
                daily = download_daily(self.symbols, date_30_days_ago, current_date)
                archive_orderbook = load_orderbook(self.symbols, date_30_days_ago, current_date)

            self.ret_processor = MultiAssetMidRetProcessor(
                self.symbols,
                archive_orderbook,
                daily,
                config.half_life,
                config.freq,
                config.start_time,
                config.end_time,
                config.compute_level,
            )
            save_checkpoint(self, self.state_dir, self._ts_ms_now())

        print("time bound:", self.ret_processor.start_time, self.ret_processor.end_time)

        self.transition_tracker = TransitionTracker(self.symbols, config.n_slots, config.base_threshold)

        self._rotations_by_sell_id = {}
        self._buy_to_sell = {}

        self.pending_order_ids = set()
        self.all_rotations_sent = False

        self.lowest_ret_symbol = None

        self._order_meta: Dict[str, Dict] = {}
        # buy_order_id -> synthetic sell_order_id for cash-collector fill linkage
        self._metrics_cc_by_buy: Dict[str, str] = {}

        self._metrics: Optional[PairTradeMetricsTracker] = None
        self._metrics_session_meta: Dict = {}
        if config.metrics_enabled:
            try:
                metrics_config = PairTradeMetricsConfig(
                    enabled=True,
                    strategy_name=config.metrics_strategy_name,
                    config_file=os.path.basename(__file__),
                    snapshot_interval_sec=config.metrics_snapshot_interval_sec,
                    signal_enabled=config.metrics_signal_enabled,
                    exchange=config.exchange,
                    trading_pairs=list(config.trading_pairs),
                    symbols=list(self.symbols),
                    hb_to_strat_map=dict(self.hb_to_strat_map),
                    n_slots=config.n_slots,
                )
                self._metrics = PairTradeMetricsTracker(metrics_config, connectors)
            except Exception as exc:
                self.logger().warning("Strategy metrics disabled: %s", exc)

        if resuming:
            self._metrics_session_meta = {
                "session_event": "session_resume",
                "trading_day": trading_day,
                "state_dir": self.state_dir,
            }
        else:
            self._metrics_session_meta = {
                "session_event": "session_cold_start",
                "trading_day": trading_day,
                "state_dir": self.state_dir,
            }

    def start(self, clock, timestamp: float):
        super().start(clock, timestamp)
        if self._metrics is not None:
            self._metrics.start()

    def stop(self, clock):
        if self._metrics is not None:
            self._metrics.stop()
        super().stop(clock)

    def _basket_symbols(self, positions: pd.Series) -> List[str]:
        return [s for s in positions.index if float(positions[s]) > 0]

    def _metrics_quote_balance(self) -> Decimal:
        _, quote = list(self.hb_to_strat_map.keys())[0].split("-")
        return Decimal(str(self.connectors[self.config.exchange].get_available_balance(quote)))

    def _metrics_maybe_init(self):
        if self._metrics is not None and self.ready_to_trade:
            self._metrics.maybe_initialize(self._now_s(), self._metrics_session_meta)
            event = self._metrics_session_meta.get("session_event")
            if event and self._metrics.session_id is not None:
                self._metrics.log_event(
                    event,
                    {
                        "trading_day": self._metrics_session_meta.get("trading_day"),
                        "state_dir": self._metrics_session_meta.get("state_dir"),
                    },
                )
                self._metrics_session_meta.pop("session_event", None)

    def _refresh_lowest_ret_symbol(self) -> None:
        """Recompute cash-collector target from latest processor state (not only on freq bins)."""
        try:
            ts = pd.Timestamp(self.processor_timestamp, unit="s")
            rets = self.ret_processor.get_ret(ts)
            ba_spread = self.ret_processor.get_ba_spread(ts)
        except Exception:
            return
        if rets is None or ba_spread is None or len(rets) == 0:
            return
        cost = rets + ba_spread
        if not cost.notna().any():
            return
        self.lowest_ret_symbol = cost.index[cost.argmin()]

    def _metrics_snapshot(self):
        if self._metrics is None:
            return
        self._refresh_lowest_ret_symbol()
        positions = self.get_positions()
        self._metrics.maybe_snapshot(
            self._now_s(),
            basket_symbols=self._basket_symbols(positions),
            quote_balance=self._metrics_quote_balance(),
            lowest_ret_symbol=self.lowest_ret_symbol,
            pending_order_count=len(self.pending_order_ids),
        )

    def _metrics_rotation_sell_id(self, order_id: Optional[str]) -> Optional[str]:
        if order_id is None:
            return None
        if order_id in self._rotations_by_sell_id:
            return order_id
        if order_id in self._metrics_cc_by_buy:
            return self._metrics_cc_by_buy[order_id]
        return self._buy_to_sell.get(order_id)

    @property
    def processor_timestamp(self):
        if self.config.exchange in {"mock", "mobin_mock"}:
            mock_exchange = self.connectors[self.config.exchange]
            return mock_exchange.current_playback_timestamp
        return self.current_timestamp

    def _now_s(self) -> float:
        ts = self.processor_timestamp
        if isinstance(ts, pd.Timestamp):
            return float(ts.timestamp())
        return float(ts)

    def _ts_ms_now(self) -> int:
        ts = self.processor_timestamp
        if isinstance(ts, pd.Timestamp):
            return int(ts.value // 1_000_000)

        value = float(ts)
        if pd.isna(value):
            return int(pd.Timestamp.utcnow().value // 1_000_000)
        return int(value * 1000)

    def _track_order(self, order_id: str, trading_pair: str, side: str):
        self._order_meta[order_id] = {
            "pair": trading_pair,
            "side": side,
            "created_ts": self._now_s(),
            "cancel_requested": False,
        }

    def _request_cancel(self, order_id: str, reason: str):
        meta = self._order_meta.get(order_id)
        if meta is None:
            return
        if meta["cancel_requested"]:
            return
        meta["cancel_requested"] = True
        if self._metrics is not None:
            self._metrics.log_event(
                "order_timeout",
                {"order_id": order_id, "pair": meta["pair"], "side": meta["side"], "reason": reason},
                level="warning",
            )
        try:
            self.logger().warning(
                f"Order timeout/cancel: oid={order_id} pair={meta['pair']} side={meta['side']} reason={reason}"
            )
            self.cancel(self.config.exchange, meta["pair"], order_id)
        except Exception as e:
            self.logger().warning(f"Cancel request failed for {order_id}: {e}")

    def _check_timeouts(self):
        timeout_s = float(self.config.order_timeout_s)
        if timeout_s <= 0:
            return
        now_s = self._now_s()
        for oid in list(self.pending_order_ids):
            meta = self._order_meta.get(oid)
            if meta is None:
                continue
            age = now_s - float(meta["created_ts"])
            if age >= timeout_s:
                self._request_cancel(oid, reason=f"age={age:.1f}s>=timeout={timeout_s:.1f}s")

    def meets_order_constraints(self, trading_pair: str, amount: Decimal, price: Decimal, side: str) -> bool:
        connector = self.connectors[self.config.exchange]
        rule = connector.trading_rules.get(trading_pair)
        if rule is None or amount <= 0 or price <= 0:
            return False

        if amount < rule.min_order_size:
            return False

        notional = amount * price

        if side == "buy":
            return notional >= MOBIN_MIN_NOTIONAL

        if side != "sell":
            return False

        if notional >= MOBIN_MIN_NOTIONAL:
            return True

        base, _ = trading_pair.split("-")
        available = Decimal(str(connector.get_available_balance(base)))
        if available <= 0:
            return False

        step = Decimal(str(rule.min_base_amount_increment or "0"))
        epsilon = step if step > 0 else Decimal("0")
        is_sell_all = amount >= (available - epsilon)
        available_notional = available * price

        return is_sell_all and available_notional < MOBIN_MIN_NOTIONAL

    def buy(
        self,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price=s_decimal_nan,
        position_action=PositionAction.OPEN,
    ) -> str:
        if not self.meets_order_constraints(trading_pair, amount, price, side="buy"):
            self.logger().info(f"Skip buy {trading_pair}: amount={amount} price={price} below min notional")
            if self._metrics is not None:
                self._metrics.log_event(
                    "skip_min_notional",
                    {"side": "buy", "pair": trading_pair, "amount": str(amount), "price": str(price)},
                )
            return ""
        return super().buy(connector_name, trading_pair, amount, order_type, price, position_action)

    def sell(
        self,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price=s_decimal_nan,
        position_action=PositionAction.OPEN,
    ) -> str:
        if not self.meets_order_constraints(trading_pair, amount, price, side="sell"):
            self.logger().info(f"Skip sell {trading_pair}: amount={amount} price={price} below min notional")
            if self._metrics is not None:
                self._metrics.log_event(
                    "skip_min_notional",
                    {"side": "sell", "pair": trading_pair, "amount": str(amount), "price": str(price)},
                )
            return ""
        return super().sell(connector_name, trading_pair, amount, order_type, price, position_action)

    def _aggressive_limit_price(self, trading_pair: str, attempt: int) -> Decimal:
        connector = self.connectors[self.config.exchange]
        best_ask = Decimal(str(connector.get_price_by_type(trading_pair, PriceType.BestAsk)))
        if best_ask <= Decimal("0"):
            return Decimal("0")
        bps = Decimal(str(self.config.buy_slippage_bps)) + Decimal(str(attempt)) * Decimal(
            str(self.config.buy_slippage_step_bps)
        )
        return best_ask * (Decimal("1") + bps / Decimal("10000"))

    def _rotation_remaining_quote(self, rotation: Dict) -> Decimal:
        earned = Decimal(str(rotation.get("quote_earned", "0")))
        spent = Decimal(str(rotation.get("quote_spent", "0")))
        target = earned * Decimal(str(self.config.spend_factor))
        remaining = target - spent
        if remaining < Decimal("0"):
            remaining = Decimal("0")
        return remaining

    def _place_buy_retry_for_sell(self, sell_order_id: str) -> Optional[str]:
        rotation = self._rotations_by_sell_id.get(sell_order_id)
        if rotation is None:
            return None

        if rotation.get("active_buy_id") is not None:
            return None

        remaining_quote = self._rotation_remaining_quote(rotation)
        min_quote = Decimal(str(self.config.min_quote_to_spend))
        if remaining_quote <= min_quote:
            return None

        attempt = int(rotation.get("buy_attempts", 0))
        if attempt >= int(self.config.max_buy_retries):
            self.logger().warning(
                f"Max buy retries reached for sell_id={sell_order_id}. remaining_quote={remaining_quote}"
            )
            if self._metrics is not None:
                self._metrics.on_rotation_update(
                    sell_order_id,
                    status="max_buy_retries",
                    metadata={"remaining_quote": str(remaining_quote), "attempts": attempt},
                )
                self._metrics.log_event(
                    "max_buy_retries",
                    {"sell_order_id": sell_order_id, "remaining_quote": str(remaining_quote)},
                    level="warning",
                )
            return None

        buy_pair = rotation["buy_pair"]
        px = self._aggressive_limit_price(buy_pair, attempt)
        if px <= Decimal("0"):
            self.logger().warning(f"Invalid buy price for {buy_pair}; cannot buy remaining={remaining_quote}")
            return None

        buy_amount = remaining_quote / px
        if buy_amount <= Decimal("0"):
            return None

        self.logger().info(
            f"BUY_AFTER_SELL: sell_id={sell_order_id} attempt={attempt + 1}/{int(self.config.max_buy_retries)} "
            f"pair={buy_pair} px={px} quote_remaining~{remaining_quote} amount={buy_amount}"
        )

        buy_order_id = self.buy(self.config.exchange, buy_pair, buy_amount, OrderType.LIMIT, price=px)
        if not buy_order_id:
            return None

        self.pending_order_ids.add(buy_order_id)
        self._buy_to_sell[buy_order_id] = sell_order_id
        self._track_order(buy_order_id, buy_pair, "buy")
        rotation["active_buy_id"] = buy_order_id
        rotation["buy_attempts"] = attempt + 1
        return buy_order_id

    def _maybe_cash_collect(self):
        if len(self.pending_order_ids) != 0:
            return
        if not self.all_rotations_sent:
            return
        min_quote = Decimal(str(self.config.min_quote_to_spend))
        for rot in self._rotations_by_sell_id.values():
            if rot.get("active_buy_id") is not None:
                return
            if self._rotation_remaining_quote(rot) > min_quote:
                return
        self.all_rotations_sent = False
        self.cash_collector()

    def feed_next_data(self):
        current_ts = pd.Timestamp(self.processor_timestamp, unit="s")
        connector = self.connectors[self.config.exchange]

        for pair in self.config.trading_pairs:
            symbol = self.hb_to_strat_map[pair]
            mid_price = connector.get_price_by_type(pair, PriceType.MidPrice)
            best_ask_price = connector.get_price_by_type(pair, PriceType.BestAsk)
            best_bid_price = connector.get_price_by_type(pair, PriceType.BestBid)
            self.ret_processor.mid_rets[symbol].feed_next_mid_price(
                current_ts,
                float(mid_price),
                float(best_ask_price),
                float(best_bid_price),
            )

    def get_positions(self):
        connector = self.connectors[self.config.exchange]
        positions = pd.Series(index=self.symbols, dtype=float)
        for pair in self.config.trading_pairs:
            symbol = self.hb_to_strat_map[pair]
            base, _ = pair.split("-")
            positions[symbol] = float(Decimal(str(connector.get_available_balance(base))))
        return positions

    def get_mid_price(self):
        connector = self.connectors[self.config.exchange]
        mid_price = pd.Series(index=self.symbols, dtype=float)
        for pair in self.config.trading_pairs:
            symbol = self.hb_to_strat_map[pair]
            mid_price[symbol] = float(connector.get_price_by_type(pair, PriceType.MidPrice))
        return mid_price

    def on_tick(self):
        if self._resume_after_ts_ms is not None:
            if self._ts_ms_now() <= self._resume_after_ts_ms:
                return
            self._resume_after_ts_ms = None

        current_ts = pd.Timestamp(self.processor_timestamp, unit="s")
        self.feed_next_data()

        self._metrics_maybe_init()
        self._metrics_snapshot()

        self._tick_counter += 1
        n = int(self.config.save_every_ticks)
        if n > 0 and self._tick_counter % n == 0:
            try:
                save_checkpoint(self, self.state_dir, self._ts_ms_now())
                if self._metrics is not None:
                    self._metrics.log_event("checkpoint_saved", {"state_dir": self.state_dir})
            except Exception as e:
                self.logger().warning(f"Checkpoint save failed: {e}")
                if self._metrics is not None:
                    self._metrics.log_event("checkpoint_failed", {"error": str(e)}, level="warning")

        self._check_timeouts()
        if len(self.pending_order_ids) > 0:
            return

        freq_result = self.ret_processor.on_freq(current_ts)
        if freq_result is not None:
            timestamp, rets, ba_spread, mid_price = freq_result
            self.lowest_ret_symbol = rets.index[(rets + ba_spread).argmin()]
            if self._metrics is not None:
                self._metrics.on_signal_bin(timestamp, rets, ba_spread, mid_price, self.ret_processor)
            self.check_and_execute(current_ts, timestamp, rets, ba_spread, mid_price)
        else:
            self._refresh_lowest_ret_symbol()

    def get_bid_ask(self, pair: str):
        bids, asks = self.connectors[self.config.exchange].get_order_book(pair).snapshot
        best_ask = asks.iloc[0].price
        ask_volume = asks.iloc[0].amount
        best_bid = bids.iloc[0].price
        bid_volume = bids.iloc[0].amount
        return {
            "bid_price": best_bid,
            "ask_price": best_ask,
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
        }

    def check_and_execute(
        self,
        clock_timstamp: pd.Timestamp,
        timestamp: pd.Timestamp,
        rets: pd.Series,
        ba_spread: pd.Series,
        mid_price: pd.Series,
    ):
        positions = self.get_positions()
        _, quote = list(self.hb_to_strat_map.keys())[0].split("-")
        quote_balance = Decimal(str(self.connectors[self.config.exchange].get_available_balance(quote)))
        self.logger().info(
            f"**position_log** clock_timestamp: {clock_timstamp}, proc_timestamp: {timestamp}, "
            f"positions: {positions.to_dict()}, quote_balance: {quote_balance}"
        )
        self.logger().info(
            f"**returns** timestamp: {timestamp}, rets: {rets.to_dict()}, ba_spread: {ba_spread.to_dict()}"
        )
        signal_mid = mid_price  # freq-bin mid for realized_edge
        live_mid = self.get_mid_price()
        transition_df = self.transition_tracker.calculate_transitions(positions, live_mid, rets, ba_spread)
        self.execute_rotation(transition_df, rets=rets, ba_spread=ba_spread, signal_mid=signal_mid)

    def execute_rotation(
        self,
        transition_df: pd.DataFrame,
        rets: Optional[pd.Series] = None,
        ba_spread: Optional[pd.Series] = None,
        signal_mid: Optional[pd.Series] = None,
    ):
        connector = self.connectors[self.config.exchange]
        self.all_rotations_sent = False

        for i, row in transition_df.iterrows():
            pre_symbol = row["prev_symbol"]
            new_symbol = row["new_symbol"]

            sell_pair = self.strat_to_hb_map[pre_symbol]
            buy_pair = self.strat_to_hb_map[new_symbol]

            bid_ask_info = {
                pre_symbol: self.get_bid_ask(sell_pair),
                new_symbol: self.get_bid_ask(buy_pair),
            }

            sell_amount, buy_amount = self.transition_tracker.get_possible_transitions(
                row, bid_ask_info, self.ret_processor
            )
            if sell_amount <= 0 or buy_amount <= 0:
                continue

            sell_px = Decimal(str(bid_ask_info[pre_symbol]["bid_price"]))
            buy_px = Decimal(str(bid_ask_info[new_symbol]["ask_price"]))

            last_edge = None
            try:
                ret_bid = self.ret_processor.mid_rets[pre_symbol].get_ret_for_price(float(sell_px))
                ret_ask = self.ret_processor.mid_rets[new_symbol].get_ret_for_price(float(buy_px))
                if ret_bid == ret_bid and ret_ask == ret_ask:  # not NaN
                    last_edge = float(ret_bid - ret_ask)
            except Exception:
                last_edge = None

            ret_prev = None
            ret_new = None
            mid_prev = None
            mid_new = None
            ba_prev = None
            ba_new = None
            if rets is not None and pre_symbol in rets.index and pd.notna(rets[pre_symbol]):
                ret_prev = float(rets[pre_symbol])
            if rets is not None and new_symbol in rets.index and pd.notna(rets[new_symbol]):
                ret_new = float(rets[new_symbol])
            if signal_mid is not None and pre_symbol in signal_mid.index and pd.notna(signal_mid[pre_symbol]):
                mid_prev = float(signal_mid[pre_symbol])
            if signal_mid is not None and new_symbol in signal_mid.index and pd.notna(signal_mid[new_symbol]):
                mid_new = float(signal_mid[new_symbol])
            if ba_spread is not None and pre_symbol in ba_spread.index and pd.notna(ba_spread[pre_symbol]):
                ba_prev = float(ba_spread[pre_symbol])
            if ba_spread is not None and new_symbol in ba_spread.index and pd.notna(ba_spread[new_symbol]):
                ba_new = float(ba_spread[new_symbol])

            rotation_info = {
                "sell_pair": sell_pair,
                "buy_pair": buy_pair,
                "sell_amount": Decimal(str(sell_amount)),
                "buy_amount": Decimal(str(buy_amount)),
                "sell_price": sell_px,
                "buy_price": buy_px,
                "sell_volume": bid_ask_info[pre_symbol]["bid_volume"],
                "buy_volume": bid_ask_info[new_symbol]["ask_volume"],
                "transition_value": row["transition_value"],
                "transition_score": row["transition_score"],
                "quote_earned": Decimal("0"),
                "quote_spent": Decimal("0"),
                "active_buy_id": None,
                "buy_attempts": 0,
                "started_ts": self._now_s(),
                "ret_prev": ret_prev,
                "ret_new": ret_new,
                "signal_mid_prev": mid_prev,
                "signal_mid_new": mid_new,
                "ba_spread_prev": ba_prev,
                "ba_spread_new": ba_new,
                "last_edge": last_edge,
                "sell_notional": Decimal("0"),
                "sell_qty": Decimal("0"),
                "buy_notional": Decimal("0"),
                "buy_qty": Decimal("0"),
                "sell_fee_quote": Decimal("0"),
                "buy_fee_quote": Decimal("0"),
            }

            sell_base, _ = sell_pair.split("-")

            available_sell_amount = Decimal(str(connector.get_available_balance(sell_base)))
            if available_sell_amount < rotation_info["sell_amount"]:
                self.logger().warning(f"Not enough {sell_base} available to sell for {sell_pair}; skip.")
                if self._metrics is not None:
                    self._metrics.log_event(
                        "not_enough_balance",
                        {"symbol": pre_symbol, "pair": sell_pair, "needed": str(rotation_info["sell_amount"])},
                        level="warning",
                    )
                continue

            self.logger().info(
                f"ROTATE(parallel): {pre_symbol}->{new_symbol} | "
                f"sell_pair={sell_pair} sell@{rotation_info['sell_price']} amount={rotation_info['sell_amount']} | "
                f"buy_pair={buy_pair} buy@{rotation_info['buy_price']} amount={rotation_info['buy_amount']}"
            )

            sell_order_id = self.sell(
                self.config.exchange,
                sell_pair,
                Decimal(rotation_info["sell_amount"]),
                OrderType.LIMIT,
                price=Decimal(str(rotation_info["sell_price"])),
            )
            if not sell_order_id:
                continue

            if self._metrics is not None:
                self._metrics.on_rotation_started(
                    sell_order_id=sell_order_id,
                    prev_symbol=pre_symbol,
                    new_symbol=new_symbol,
                    transition_score=float(row["transition_score"]),
                    transition_value=float(row["transition_value"]),
                    sell_pair=sell_pair,
                    buy_pair=buy_pair,
                    last_edge=last_edge,
                    intended_sell_price=float(sell_px),
                    intended_buy_price=float(buy_px),
                    signal_mid_prev=mid_prev,
                    signal_mid_new=mid_new,
                    ret_prev=ret_prev,
                    ret_new=ret_new,
                    ba_spread_prev=ba_prev,
                    ba_spread_new=ba_new,
                    metadata={
                        "sell_amount": str(rotation_info["sell_amount"]),
                        "buy_amount": str(rotation_info["buy_amount"]),
                        "sell_price": str(rotation_info["sell_price"]),
                        "buy_price": str(rotation_info["buy_price"]),
                    },
                )
            self._rotations_by_sell_id[sell_order_id] = rotation_info
            self.pending_order_ids.add(sell_order_id)
            self._track_order(sell_order_id, sell_pair, "sell")
            return

        self.all_rotations_sent = True

    def _rotation_execution_metrics(self, rotation: Dict) -> Dict:
        """VWAP / slippage / realized_edge(+net) / latency once both legs have size."""
        out: Dict = {}
        sell_qty = Decimal(str(rotation.get("sell_qty", "0")))
        buy_qty = Decimal(str(rotation.get("buy_qty", "0")))
        sell_vwap = None
        buy_vwap = None
        sell_vwap_net = None
        buy_vwap_net = None
        if sell_qty > 0:
            sell_notional = Decimal(str(rotation["sell_notional"]))
            sell_fees = Decimal(str(rotation.get("sell_fee_quote", "0")))
            sell_vwap = float(sell_notional / sell_qty)
            out["sell_vwap"] = sell_vwap
            intended_sell = float(rotation["sell_price"])
            slip = compute_log_slippage(sell_vwap, intended_sell)
            if slip is not None:
                out["sell_slippage"] = slip
            net_sell = sell_notional - sell_fees
            if net_sell > 0:
                sell_vwap_net = float(net_sell / sell_qty)
                out["sell_vwap_net"] = sell_vwap_net
        if buy_qty > 0:
            buy_notional = Decimal(str(rotation["buy_notional"]))
            buy_fees = Decimal(str(rotation.get("buy_fee_quote", "0")))
            buy_vwap = float(buy_notional / buy_qty)
            out["buy_vwap"] = buy_vwap
            intended_buy = float(rotation["buy_price"])
            slip = compute_log_slippage(buy_vwap, intended_buy)
            if slip is not None:
                out["buy_slippage"] = slip
            buy_vwap_net = float((buy_notional + buy_fees) / buy_qty)
            out["buy_vwap_net"] = buy_vwap_net

        if sell_qty > 0 and buy_qty > 0:
            out["total_fee_quote"] = float(
                Decimal(str(rotation.get("sell_fee_quote", "0")))
                + Decimal(str(rotation.get("buy_fee_quote", "0")))
            )

        ret_prev = rotation.get("ret_prev")
        ret_new = rotation.get("ret_new")
        mid_prev = rotation.get("signal_mid_prev")
        mid_new = rotation.get("signal_mid_new")
        if (
            sell_vwap is not None
            and buy_vwap is not None
            and ret_prev is not None
            and ret_new is not None
            and mid_prev is not None
            and mid_new is not None
        ):
            realized = compute_realized_edge(ret_prev, ret_new, sell_vwap, buy_vwap, mid_prev, mid_new)
            if realized is not None:
                out["realized_edge"] = realized
            if sell_vwap_net is not None and buy_vwap_net is not None:
                realized_net = compute_realized_edge(
                    ret_prev, ret_new, sell_vwap_net, buy_vwap_net, mid_prev, mid_new
                )
                if realized_net is not None:
                    out["realized_edge_net"] = realized_net

        started = rotation.get("started_ts")
        if started is not None:
            out["latency_sec"] = float(self._now_s() - float(started))
        return out

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        sell_id = self._buy_to_sell.pop(event.order_id, None)
        rotation = self._rotations_by_sell_id.get(sell_id) if sell_id is not None else None

        if rotation is not None and rotation.get("active_buy_id") == event.order_id:
            rotation["active_buy_id"] = None

        if event.order_id in self.pending_order_ids:
            self.remove_pending_order(event.order_id, trigger_cash_collector=False)

        self._order_meta.pop(event.order_id, None)

        cc_id = self._metrics_cc_by_buy.pop(event.order_id, None)
        if cc_id is not None and self._metrics is not None:
            self._metrics.on_rotation_update(
                cc_id,
                status="buy_complete",
                quote_spent=Decimal(str(event.quote_asset_amount)),
            )

        if sell_id is None or rotation is None:
            self._maybe_cash_collect()
            return

        rotation["quote_spent"] = max(
            Decimal(str(rotation.get("quote_spent", "0"))),
            Decimal(str(event.quote_asset_amount)),
        )

        remaining = self._rotation_remaining_quote(rotation)
        self.logger().info(
            f"BUY complete sell_id={sell_id} spent={rotation['quote_spent']} remaining={remaining}"
        )
        done = remaining <= Decimal(str(self.config.min_quote_to_spend))
        if self._metrics is not None:
            update_kw = {
                "status": "buy_complete" if done else "buy_partial",
                "quote_spent": rotation["quote_spent"],
                "metadata": {"remaining_quote": str(remaining)},
            }
            if done:
                update_kw.update(self._rotation_execution_metrics(rotation))
            self._metrics.on_rotation_update(sell_id, **update_kw)

        self._place_buy_retry_for_sell(sell_id)
        self._maybe_cash_collect()

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        rotation_info = self._rotations_by_sell_id.get(event.order_id)
        if rotation_info is None:
            if event.order_id in self.pending_order_ids:
                self.remove_pending_order(event.order_id, trigger_cash_collector=True)
            self._order_meta.pop(event.order_id, None)
            return

        rotation_info["quote_earned"] = max(
            Decimal(str(rotation_info.get("quote_earned", "0"))),
            event.quote_asset_amount,
        )

        if self._metrics is not None:
            exec_partial = self._rotation_execution_metrics(rotation_info)
            self._metrics.on_rotation_update(
                event.order_id,
                status="sell_complete",
                quote_earned=rotation_info["quote_earned"],
                sell_vwap=exec_partial.get("sell_vwap"),
                sell_slippage=exec_partial.get("sell_slippage"),
            )

        self._place_buy_retry_for_sell(event.order_id)

        if event.order_id in self.pending_order_ids:
            self.remove_pending_order(event.order_id, trigger_cash_collector=False)

        self._order_meta.pop(event.order_id, None)
        self._maybe_cash_collect()

    def did_fill_order(self, event: OrderFilledEvent):
        oid = event.order_id
        sell_id = self._metrics_rotation_sell_id(oid)
        if self._metrics is not None:
            self._metrics.on_fill(event, sell_order_id=sell_id)

        px = Decimal(str(event.price))
        qty = Decimal(str(event.amount))
        notional = px * qty
        quote = event.trading_pair.split("-")[1] if "-" in event.trading_pair else "IRR"
        fee_q = fee_to_quote(event.trade_fee, px, qty, quote, None)

        rotation = self._rotations_by_sell_id.get(oid)
        if rotation is not None and event.trade_type == TradeType.SELL:
            rotation["quote_earned"] = Decimal(str(rotation.get("quote_earned", "0"))) + notional
            rotation["sell_notional"] = Decimal(str(rotation.get("sell_notional", "0"))) + notional
            rotation["sell_qty"] = Decimal(str(rotation.get("sell_qty", "0"))) + qty
            rotation["sell_fee_quote"] = Decimal(str(rotation.get("sell_fee_quote", "0"))) + fee_q
            self._place_buy_retry_for_sell(oid)
            return

        sell_id = self._buy_to_sell.get(oid)
        if sell_id is not None and event.trade_type == TradeType.BUY:
            rot = self._rotations_by_sell_id.get(sell_id)
            if rot is None:
                return
            rot["quote_spent"] = Decimal(str(rot.get("quote_spent", "0"))) + notional
            rot["buy_notional"] = Decimal(str(rot.get("buy_notional", "0"))) + notional
            rot["buy_qty"] = Decimal(str(rot.get("buy_qty", "0"))) + qty
            rot["buy_fee_quote"] = Decimal(str(rot.get("buy_fee_quote", "0"))) + fee_q

    def did_fail_order(self, order_failed_event: MarketOrderFailureEvent):
        self._handle_order_end(order_failed_event.order_id, reason="failed")

    def did_cancel_order(self, cancelled_event: OrderCancelledEvent):
        self._handle_order_end(cancelled_event.order_id, reason="cancelled")

    def did_expire_order(self, expired_event: OrderExpiredEvent):
        self._handle_order_end(expired_event.order_id, reason="expired")

    def _handle_order_end(self, order_id: str, reason: str):
        sell_id = self._buy_to_sell.pop(order_id, None)
        if sell_id is not None:
            rot = self._rotations_by_sell_id.get(sell_id)
            if rot is not None and rot.get("active_buy_id") == order_id:
                rot["active_buy_id"] = None
                self._place_buy_retry_for_sell(sell_id)

            if self._metrics is not None:
                self._metrics.on_rotation_update(sell_id, status=f"buy_{reason}")
                self._metrics.log_event(
                    "order_end",
                    {"order_id": order_id, "sell_order_id": sell_id, "reason": reason, "side": "buy"},
                    level="warning",
                )

            if order_id in self.pending_order_ids:
                self.remove_pending_order(order_id, trigger_cash_collector=False)
            self._order_meta.pop(order_id, None)
            self._maybe_cash_collect()
            return

        cc_id = self._metrics_cc_by_buy.pop(order_id, None)
        if cc_id is not None and self._metrics is not None:
            self._metrics.on_rotation_update(cc_id, status=f"buy_{reason}")
            self._metrics.log_event(
                "order_end",
                {"order_id": order_id, "sell_order_id": cc_id, "reason": reason, "side": "buy"},
                level="warning",
            )

        if order_id in self._rotations_by_sell_id:
            self._place_buy_retry_for_sell(order_id)
            if self._metrics is not None:
                self._metrics.on_rotation_update(order_id, status=f"sell_{reason}")
                self._metrics.log_event(
                    "order_end",
                    {"order_id": order_id, "reason": reason, "side": "sell"},
                    level="warning",
                )

        if order_id in self.pending_order_ids:
            self.remove_pending_order(order_id, trigger_cash_collector=False)
        self._order_meta.pop(order_id, None)
        self._maybe_cash_collect()

    def remove_pending_order(self, order_id, trigger_cash_collector: bool = True):
        if order_id in self.pending_order_ids:
            self.pending_order_ids.remove(order_id)

        if len(self.pending_order_ids) == 0:
            self.logger().info("No pending orders left")
            if trigger_cash_collector:
                self._maybe_cash_collect()

    def cash_collector(self):
        self._refresh_lowest_ret_symbol()
        if self.lowest_ret_symbol is None:
            return None

        connector = self.connectors[self.config.exchange]
        buy_pair = self.strat_to_hb_map[self.lowest_ret_symbol]

        base, quote = buy_pair.split("-")
        quote_balance = Decimal(str(connector.get_available_balance(quote)))

        if quote_balance <= Decimal("0"):
            self.logger().warning(f"No {quote} available to buy {buy_pair}")
            return None

        buy_px = self._aggressive_limit_price(buy_pair, attempt=0)
        if buy_px <= Decimal("0"):
            self.logger().warning(f"Invalid buy price for {buy_pair}")
            return None

        quote_to_spend = quote_balance * Decimal(str(self.config.spend_factor))
        buy_amount = quote_to_spend / buy_px

        if buy_amount <= Decimal("0"):
            self.logger().warning(f"Computed buy_amount<=0 for {buy_pair}")
            return None

        self.logger().info(
            f"CASH_COLLECTOR: Buying {buy_amount} {buy_pair} at {buy_px} (limit at best ask) "
            f"using {quote_to_spend} {quote} (from {quote_balance} available)"
        )
        if self._metrics is not None:
            self._metrics.log_event(
                "cash_collector",
                {
                    "pair": buy_pair,
                    "symbol": self.lowest_ret_symbol,
                    "buy_amount": str(buy_amount),
                    "buy_price": str(buy_px),
                    "quote_to_spend": str(quote_to_spend),
                },
            )

        buy_order_id = self.buy(
            self.config.exchange,
            buy_pair,
            buy_amount,
            OrderType.LIMIT,
            price=buy_px,
        )
        if buy_order_id:
            self.pending_order_ids.add(buy_order_id)
            self._track_order(buy_order_id, buy_pair, "buy")
            if self._metrics is not None:
                cc_key = f"cash_collector:{buy_order_id}"
                self._metrics_cc_by_buy[buy_order_id] = cc_key
                self._metrics.on_rotation_started(
                    sell_order_id=cc_key,
                    prev_symbol="CASH",
                    new_symbol=self.lowest_ret_symbol,
                    transition_score=0.0,
                    transition_value=float(quote_to_spend),
                    sell_pair="",
                    buy_pair=buy_pair,
                    status="cash_collector",
                    metadata={
                        "kind": "cash_collector",
                        "buy_order_id": buy_order_id,
                        "buy_amount": str(buy_amount),
                        "buy_price": str(buy_px),
                        "quote_to_spend": str(quote_to_spend),
                    },
                )

        return buy_order_id or None

    async def on_stop(self):
        try:
            save_checkpoint(self, self.state_dir, self._ts_ms_now())
            self.logger().info(f"Final checkpoint written to {self.state_dir}")
            if self._metrics is not None:
                self._metrics.log_event("checkpoint_saved", {"state_dir": self.state_dir})
        except Exception as e:
            self.logger().warning(f"Final checkpoint failed: {e}")
            if self._metrics is not None:
                self._metrics.log_event("checkpoint_failed", {"error": str(e)}, level="warning")
        await super().on_stop()
