import logging
import os
from decimal import Decimal
from typing import Dict, List, Literal, Optional, Set

from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import MarketOrderFailureEvent, OrderFilledEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase, s_decimal_nan
from hummingbot.strategy_metrics import CrossExchangeMmMetricsTracker, MetricsConfig


class CrossExchangeIrtMmConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = []

    maker_connector: str = Field(
        "nobitex",
        json_schema_extra={"prompt": "Maker connector (e.g. nobitex, bitpin)", "prompt_on_new": True},
    )
    hedge_connector: str = Field(
        "mexc",
        json_schema_extra={"prompt": "Hedge connector (MEXC)", "prompt_on_new": True},
    )
    maker_trading_pair: str = Field(
        "BTC-IRT",
        json_schema_extra={"prompt": "Maker pair (IRT quote)", "prompt_on_new": True},
    )
    hedge_trading_pair: str = Field(
        "BTC-USDT",
        json_schema_extra={"prompt": "Hedge pair on MEXC", "prompt_on_new": True},
    )
    fx_trading_pair: str = Field(
        "USDT-IRT",
        json_schema_extra={
            "prompt": "FX pair on maker exchange for USDT/IRT mid (pricing only)",
            "prompt_on_new": True,
        },
    )
    order_amount: Decimal = Field(
        Decimal("0.001"),
        json_schema_extra={"prompt": "Order size in base asset (BTC)", "prompt_on_new": True},
    )
    half_spread_bps: Decimal = Field(
        Decimal("10"),
        json_schema_extra={"prompt": "Half-spread from fair in bps (10 = 20 bps total width)", "prompt_on_new": True},
    )
    refresh_drift_bps: Decimal = Field(
        Decimal("2.5"),
        json_schema_extra={"prompt": "Refresh quotes when fair moves this many bps", "prompt_on_new": True},
    )
    refresh_interval_sec: int = Field(
        5,
        json_schema_extra={"prompt": "Refresh quotes at least every N seconds", "prompt_on_new": True},
    )
    hedge_order_type: Literal["MARKET", "LIMIT"] = Field(
        "MARKET",
        json_schema_extra={"prompt": "Hedge order type on hedge exchange (MARKET or LIMIT)", "prompt_on_new": True},
    )
    metrics_enabled: bool = Field(
        True,
        json_schema_extra={"prompt": "Log performance metrics to PostgreSQL", "prompt_on_new": True},
    )
    metrics_strategy_name: str = Field(
        "cross_exchange_irt_mm",
        json_schema_extra={"prompt": "Strategy name in metrics DB", "prompt_on_new": True},
    )
    metrics_snapshot_interval_sec: int = Field(
        60,
        json_schema_extra={"prompt": "Balance snapshot interval (seconds)", "prompt_on_new": True},
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        maker_pairs: Set[str] = {self.maker_trading_pair, self.fx_trading_pair}
        markets[self.maker_connector] = markets.get(self.maker_connector, set()) | maker_pairs
        markets[self.hedge_connector] = markets.get(self.hedge_connector, set()) | {self.hedge_trading_pair}
        return markets


class CrossExchangeIrtMm(StrategyV2Base):
    """
    Cross-exchange market making on an IRT maker leg with MEXC (or other) BTC hedge.

    Fair value (IRT):
        hedge BTC-USDT mid × maker USDT-IRT mid

    Refresh maker quotes when fair drifts >= refresh_drift_bps OR refresh_interval_sec elapsed.
    On maker fill → hedge opposite side on hedge exchange.
    """

    def __init__(self, connectors: Dict[str, ConnectorBase], config: CrossExchangeIrtMmConfig):
        super().__init__(connectors, config)
        self.config: CrossExchangeIrtMmConfig = config
        self._last_fair_irt: Optional[Decimal] = None
        self._last_refresh_ts: float = 0.0
        self._last_fair_display: Optional[Decimal] = None
        self._last_fair_usdt_display: Optional[Decimal] = None
        self._last_usdt_irt: Optional[Decimal] = None
        self._metrics: Optional[CrossExchangeMmMetricsTracker] = None
        self._hedge_order_ids: set = set()
        if config.metrics_enabled:
            try:
                metrics_config = MetricsConfig(
                    enabled=True,
                    strategy_name=config.metrics_strategy_name,
                    config_file=os.path.basename(__file__),
                    snapshot_interval_sec=config.metrics_snapshot_interval_sec,
                    maker_connector=config.maker_connector,
                    hedge_connector=config.hedge_connector,
                    maker_trading_pair=config.maker_trading_pair,
                    hedge_trading_pair=config.hedge_trading_pair,
                    fx_trading_pair=config.fx_trading_pair,
                )
                self._metrics = CrossExchangeMmMetricsTracker(metrics_config, connectors)
            except Exception as exc:
                self.logger().warning("Strategy metrics disabled: %s", exc)

    def start(self, clock, timestamp: float):
        super().start(clock, timestamp)
        if self._metrics is not None:
            self._metrics.start()

    def stop(self, clock):
        if self._metrics is not None:
            self._metrics.stop()
        super().stop(clock)

    async def on_stop(self):
        if self._metrics is not None:
            self._metrics.stop()
        await super().on_stop()

    @staticmethod
    def _fmt(value, decimals=2):
        return f"{value:.{decimals}f}" if value is not None else "None"

    def _meets_order_constraints(
            self, connector_name: str, trading_pair: str, amount: Decimal, price: Decimal,
    ) -> bool:
        connector = self.connectors.get(connector_name)
        if connector is None:
            return False

        rule = connector.trading_rules.get(trading_pair)
        if rule is None:
            return False

        if amount < rule.min_order_size:
            return False

        check_price = price
        if check_price.is_nan() or check_price <= 0:
            check_price = self._safe_mid_price(connector, trading_pair)
            if check_price is None:
                return False

        notional = amount * check_price
        return notional >= rule.min_notional_size

    def buy(
            self, connector_name: str, trading_pair: str, amount: Decimal, order_type: OrderType,
            price=s_decimal_nan, position_action=PositionAction.OPEN,
    ) -> str:
        if not self._meets_order_constraints(connector_name, trading_pair, amount, price):
            self.logger().info(
                f"Skip buy {trading_pair} on {connector_name}: "
                f"amount={amount} price={price} below exchange minimums"
            )
            return ""
        return super().buy(connector_name, trading_pair, amount, order_type, price, position_action)

    def sell(
            self, connector_name: str, trading_pair: str, amount: Decimal, order_type: OrderType,
            price=s_decimal_nan, position_action=PositionAction.OPEN,
    ) -> str:
        if not self._meets_order_constraints(connector_name, trading_pair, amount, price):
            self.logger().info(
                f"Skip sell {trading_pair} on {connector_name}: "
                f"amount={amount} price={price} below exchange minimums"
            )
            return ""
        return super().sell(connector_name, trading_pair, amount, order_type, price, position_action)

    def on_tick(self):
        if self._metrics is not None and self.ready_to_trade:
            self._metrics.maybe_snapshot(self.current_timestamp)

        fair_tuple = self._compute_fair()
        if fair_tuple is None:
            self.logger().warning("Fair price unavailable — waiting for market data.")
            return

        fair_usdt, fair_irt, usdt_irt = fair_tuple
        self._last_fair_display = fair_irt
        self._last_fair_usdt_display = fair_usdt
        self._last_usdt_irt = usdt_irt

        if not self._should_refresh(fair_irt):
            return

        if self._maker_orders():
            self._cancel_maker_orders()
            return

        proposal = self._create_proposal(fair_irt)
        if not proposal:
            return

        adjusted = self.connectors[self.config.maker_connector].budget_checker.adjust_candidates(
            proposal, all_or_none=False
        )
        for order in adjusted:
            if order.amount <= 0:
                continue
            self._place_order(self.config.maker_connector, order)

        self._last_fair_irt = fair_irt
        self._last_refresh_ts = self.current_timestamp

        bid_irt = next((o.price for o in proposal if o.order_side == TradeType.BUY), None)
        ask_irt = next((o.price for o in proposal if o.order_side == TradeType.SELL), None)
        bid_usdt = (bid_irt / usdt_irt) if bid_irt is not None else None
        ask_usdt = (ask_irt / usdt_irt) if ask_irt is not None else None

        self.logger().info(
            f"Refreshed quotes | fair_irt={fair_irt:.0f} IRT | "
            f"fair_usdt={fair_usdt:.2f} USDT | "
            f"usdt_irt={usdt_irt:.0f} IRT | "
            f"bid_irt={self._fmt(bid_irt, 0)} ask_irt={self._fmt(ask_irt, 0)} | "
            f"bid_usdt={self._fmt(bid_usdt)} ask_usdt={self._fmt(ask_usdt)}"
        )

    def did_fill_order(self, event: OrderFilledEvent):
        if event.trading_pair == self.config.maker_trading_pair:
            maker_connector = self.connectors[self.config.maker_connector]
            usdt_irt = maker_connector.get_mid_price(self.config.fx_trading_pair)

            maker_fill_usdt_txt = "N/A"
            maker_fill_usdt = None
            if usdt_irt is not None and usdt_irt > 0:
                maker_fill_usdt = event.price / usdt_irt
                maker_fill_usdt_txt = f"{maker_fill_usdt:.2f}"

            msg = (
                f"Maker fill: {event.trade_type.name} {event.amount} {event.trading_pair} "
                f"@ {event.price:.0f} IRT (~{maker_fill_usdt_txt} USDT)"
            )
            self.log_with_clock(logging.INFO, msg)
            self.notify_hb_app_with_timestamp(msg)

            if self._metrics is not None:
                self._metrics.on_maker_fill(
                    event,
                    fair_price=self._last_fair_display,
                    fx_rate=usdt_irt,
                    price_quote=maker_fill_usdt,
                )

            if event.trade_type == TradeType.BUY:
                self._hedge_sell(event.amount)
            elif event.trade_type == TradeType.SELL:
                self._hedge_buy(event.amount)

            self._last_refresh_ts = 0.0
            return

        if event.trading_pair == self.config.hedge_trading_pair:
            maker_connector = self.connectors[self.config.maker_connector]
            usdt_irt = maker_connector.get_mid_price(self.config.fx_trading_pair)

            hedge_fill_irt_txt = "N/A"
            if usdt_irt is not None and usdt_irt > 0:
                hedge_fill_irt = event.price * usdt_irt
                hedge_fill_irt_txt = f"{hedge_fill_irt:.0f}"

            self._hedge_order_ids.discard(event.order_id)
            msg = (
                f"Hedge fill: {event.trade_type.name} {event.amount} {event.trading_pair} "
                f"@ {event.price:.4f} USDT (~{hedge_fill_irt_txt} IRT)"
            )
            self.log_with_clock(logging.INFO, msg)
            self.notify_hb_app_with_timestamp(msg)
            if self._metrics is not None:
                self._metrics.on_hedge_fill(event)

    def did_fail_order(self, order_failed_event: MarketOrderFailureEvent):
        oid = order_failed_event.order_id
        if oid not in self._hedge_order_ids:
            return
        self._hedge_order_ids.discard(oid)
        if self._metrics is not None:
            self._metrics.on_hedge_failed(order_failed_event)

    def _safe_mid_price(self, connector: ConnectorBase, trading_pair: str) -> Optional[Decimal]:
        order_books = getattr(getattr(connector, "order_book_tracker", None), "order_books", None)
        if order_books is not None and trading_pair not in order_books:
            return None
        try:
            mid = connector.get_mid_price(trading_pair)
        except (ValueError, KeyError, AttributeError):
            return None
        if mid is None or mid <= 0:
            return None
        return mid

    def _compute_fair(self) -> Optional[tuple[Decimal, Decimal, Decimal]]:
        maker = self.connectors[self.config.maker_connector]
        hedge = self.connectors[self.config.hedge_connector]
        btc_usdt = self._safe_mid_price(hedge, self.config.hedge_trading_pair)
        usdt_irt = self._safe_mid_price(maker, self.config.fx_trading_pair)
        if btc_usdt is None or usdt_irt is None:
            return None
        return btc_usdt, btc_usdt * usdt_irt, usdt_irt

    def _should_refresh(self, fair_irt: Decimal) -> bool:
        if self._last_fair_irt is None:
            return True
        drift_bps = abs(fair_irt - self._last_fair_irt) / self._last_fair_irt * Decimal("10000")
        elapsed = self.current_timestamp - self._last_refresh_ts
        return (
            drift_bps >= self.config.refresh_drift_bps
            or elapsed >= self.config.refresh_interval_sec
        )

    def _create_proposal(self, fair_irt: Decimal) -> List[OrderCandidate]:
        spread_factor = self.config.half_spread_bps / Decimal("10000")
        bid_price = fair_irt * (Decimal("1") - spread_factor)
        ask_price = fair_irt * (Decimal("1") + spread_factor)
        amount = self.config.order_amount

        connector = self.connectors[self.config.maker_connector]
        base, quote = self.config.maker_trading_pair.split("-")
        buy_amt = min(amount, connector.get_available_balance(quote) / bid_price)
        sell_amt = min(amount, connector.get_available_balance(base))

        orders: List[OrderCandidate] = []
        if buy_amt > 0:
            orders.append(OrderCandidate(
                trading_pair=self.config.maker_trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=TradeType.BUY,
                amount=buy_amt,
                price=bid_price,
            ))
        if sell_amt > 0:
            orders.append(OrderCandidate(
                trading_pair=self.config.maker_trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=TradeType.SELL,
                amount=sell_amt,
                price=ask_price,
            ))
        return orders

    def _maker_orders(self):
        return [
            o for o in self.get_active_orders(connector_name=self.config.maker_connector)
            if o.trading_pair == self.config.maker_trading_pair
        ]

    def _cancel_maker_orders(self):
        for order in self._maker_orders():
            self.cancel(
                self.config.maker_connector,
                order.trading_pair,
                order.client_order_id,
            )

    def _place_order(self, connector_name: str, order: OrderCandidate):
        if order.order_side == TradeType.BUY:
            self.buy(connector_name, order.trading_pair, order.amount, order.order_type, order.price)
        else:
            self.sell(connector_name, order.trading_pair, order.amount, order.order_type, order.price)

    def _hedge_order_type(self) -> OrderType:
        return OrderType.MARKET if self.config.hedge_order_type == "MARKET" else OrderType.LIMIT

    def _hedge_buy(self, amount: Decimal):
        connector = self.config.hedge_connector
        pair = self.config.hedge_trading_pair
        order_type = self._hedge_order_type()
        price = Decimal("NaN")
        if order_type == OrderType.LIMIT:
            result = self.connectors[connector].get_price_for_volume(pair, True, amount)
            price = result.result_price
        candidate = OrderCandidate(
            trading_pair=pair,
            is_maker=False,
            order_type=order_type,
            order_side=TradeType.BUY,
            amount=amount,
            price=price,
        )
        adjusted = self.connectors[connector].budget_checker.adjust_candidate(
            candidate, all_or_none=False
        )
        order_id = self.buy(connector, pair, adjusted.amount, adjusted.order_type, adjusted.price)
        if order_id:
            self._hedge_order_ids.add(order_id)
        self.logger().info(f"Hedge BUY {adjusted.amount} {pair} on {connector}")
        if self._metrics is not None:
            self._metrics.on_hedge_placed(
                order_id=order_id or None,
                side="BUY",
                requested_amount=adjusted.amount,
                limit_price=adjusted.price if adjusted.order_type == OrderType.LIMIT else None,
                fail_reason=None if order_id else "order_not_placed",
            )

    def _hedge_sell(self, amount: Decimal):
        connector = self.config.hedge_connector
        pair = self.config.hedge_trading_pair
        order_type = self._hedge_order_type()
        price = Decimal("NaN")
        if order_type == OrderType.LIMIT:
            result = self.connectors[connector].get_price_for_volume(pair, False, amount)
            price = result.result_price
        candidate = OrderCandidate(
            trading_pair=pair,
            is_maker=False,
            order_type=order_type,
            order_side=TradeType.SELL,
            amount=amount,
            price=price,
        )
        adjusted = self.connectors[connector].budget_checker.adjust_candidate(
            candidate, all_or_none=False
        )
        order_id = self.sell(connector, pair, adjusted.amount, adjusted.order_type, adjusted.price)
        if order_id:
            self._hedge_order_ids.add(order_id)
        self.logger().info(f"Hedge SELL {adjusted.amount} {pair} on {connector}")
        if self._metrics is not None:
            self._metrics.on_hedge_placed(
                order_id=order_id or None,
                side="SELL",
                requested_amount=adjusted.amount,
                limit_price=adjusted.price if adjusted.order_type == OrderType.LIMIT else None,
                fail_reason=None if order_id else "order_not_placed",
            )

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = [super().format_status()]
        if self._last_fair_display is not None:
            lines.append(f"\n  Fair BTC-IRT: {self._last_fair_display:.0f}")
        if self._last_fair_usdt_display is not None:
            lines.append(f"  Fair BTC-USDT: {self._last_fair_usdt_display:.2f}")
        if self._last_usdt_irt is not None:
            lines.append(f"  USDT-IRT: {self._last_usdt_irt:.0f}")

        if self._last_fair_display is not None and self._last_fair_irt is not None:
            drift = (
                abs(self._last_fair_display - self._last_fair_irt)
                / self._last_fair_irt
                * Decimal("10000")
            )
            lines.append(f"  Drift since last refresh: {drift:.2f} bps")
        return "\n".join(lines)
