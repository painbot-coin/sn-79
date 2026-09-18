# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""SN79 live miner — Avellaneda–Stoikov inventory market maker.

Primary algorithm (the expert method for this work):
  Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book",
  Quantitative Finance. Reservation price and optimal bid/ask spread:

      r = s - q * γ * σ² * (T-t)
      δ^a + δ^b = γσ²(T-t) + (2/γ) ln(1 + γ/κ)

Fair value s is the Cont–Stoikov microprice plus Cartea–Jaimungal-style
order-flow imbalance (touch, depth, trades), not the raw mid.

Live SN79 rank-1 miner code is private. We implement the public expert
algorithm that matches how this subnet scores (Kappa-3 = risk-adjusted PnL).

Edit here, then run F:\\trading\\deploy-agent.ps1
"""

from __future__ import annotations

import json
import math
import os
import time

import bittensor as bt

from taos.common.agents import launch
from taos.im.agents import FinanceAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce
from taos.im.protocol import MarketSimulationStateUpdate, FinanceAgentResponse


class TradingMinerAgent(FinanceAgent):
    """Two-sided maker with inventory reservation and a gated take overlay."""

    def initialize(self) -> None:
        cfg = self.config
        self.quote_size = float(getattr(cfg, "quote_size", 0.20))
        self.max_inventory = float(getattr(cfg, "max_inventory", 2.2))
        self.imbalance_depth = int(getattr(cfg, "imbalance_depth", 5))
        self.gamma = float(getattr(cfg, "gamma", 1.15))
        self.kappa_liq = float(getattr(cfg, "kappa_liq", 1.5))
        self.take_z = float(getattr(cfg, "take_z", 1.05))
        self.pull_z = float(getattr(cfg, "pull_z", 0.52))
        self.reduce_z = float(getattr(cfg, "reduce_z", 0.08))
        self.heavy_z = float(getattr(cfg, "heavy_z", 0.16))
        self.flat_z = float(getattr(cfg, "flat_z", 0.28))
        self.stop_bps = float(getattr(cfg, "stop_bps", 32.0))
        self.vol_lambda = float(getattr(cfg, "vol_lambda", 0.18))
        self.expiry_period = int(getattr(cfg, "expiry_period", 25_000_000_000))
        self.max_fee_rate = float(getattr(cfg, "max_fee_rate", 0.012))
        self.max_quotes_per_side = 1

        self._state_path = os.path.expanduser(
            "~/.taos/agents/TradingMinerAgent.state.json"
        )
        self._initial_base: dict[int, float] = {}
        self._initial_wealth: dict[int, float] = {}
        self._mid: dict[int, float] = {}
        self._var: dict[int, float] = {}
        self._entry_mid: dict[int, float] = {}
        self._quoted: dict[int, tuple[float, float]] = {}
        self._logged_capital = False
        self.history_len = 0
        self.history = []
        self.config.lazy_load = True
        self._tick_budget_s = float(getattr(cfg, "tick_budget_s", 2.8))
        self._load_state()

        bt.logging.info(
            f"TradingMinerAgent AS2008 quote={self.quote_size} "
            f"gamma={self.gamma} kappa={self.kappa_liq} "
            f"take_z={self.take_z} pull_z={self.pull_z} "
            f"reduce_z={self.reduce_z} flat_z={self.flat_z} "
            f"state_books={len(self._initial_base)}"
        )

    def _decimals(self) -> tuple[int, int]:
        return (
            int(getattr(self.simulation_config, "priceDecimals", 8)),
            int(getattr(self.simulation_config, "volumeDecimals", 8)),
        )

    def _tick(self, price_decimals: int) -> float:
        return 10 ** (-price_decimals)

    def _px(self, raw: float, price_decimals: int) -> float:
        if raw <= 0:
            return 0.0
        return round(raw, price_decimals)

    def _qty(self, raw: float, volume_decimals: int, min_size: float) -> float:
        qty = max(raw, min_size)
        return round(qty, volume_decimals)

    def _load_state(self) -> None:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            bt.logging.warning(f"state load failed: {exc}")
            return
        self._initial_base = {
            int(k): float(v) for k, v in (data.get("initial_base") or {}).items()
        }
        self._entry_mid = {
            int(k): float(v) for k, v in (data.get("entry_mid") or {}).items()
        }

    def _save_state(self) -> None:
        payload = {
            "initial_base": {str(k): v for k, v in self._initial_base.items()},
            "entry_mid": {str(k): v for k, v in self._entry_mid.items()},
        }
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def _cfg_start_base(self) -> float | None:
        raw = getattr(self.simulation_config, "miner_base_balance", None)
        if raw is None:
            return None
        return float(raw)

    def _cfg_start_wealth(self) -> float | None:
        raw = getattr(self.simulation_config, "miner_wealth", None)
        if raw is None or float(raw) <= 0:
            return None
        return float(raw)

    def _net_position(self, book_id: int) -> float:
        acct = self.accounts[book_id]
        current = float(acct.base_balance.total) - float(getattr(acct, "base_loan", 0.0) or 0.0)
        start = self._cfg_start_base()
        if start is not None:
            return current - start
        if book_id not in self._initial_base:
            self._initial_base[book_id] = current
        return current - self._initial_base[book_id]

    def _mark_wealth(self, book_id: int, mid: float) -> float:
        acct = self.accounts[book_id]
        base = float(acct.base_balance.total) - float(getattr(acct, "base_loan", 0.0) or 0.0)
        quote = float(acct.quote_balance.total) - float(getattr(acct, "quote_loan", 0.0) or 0.0)
        wealth = quote + base * mid
        start = self._cfg_start_wealth()
        if start is not None:
            return wealth - start
        if book_id not in self._initial_wealth:
            self._initial_wealth[book_id] = wealth
        return wealth - self._initial_wealth[book_id]

    def _top_levels(self, levels, n: int) -> list:
        """LazyLevels does not accept slices (unhashable slice)."""
        if not levels:
            return []
        out = []
        limit = min(n, len(levels))
        for i in range(limit):
            out.append(levels[i])
        return out

    def _touch_qty(self, levels) -> tuple[float, float]:
        tops = self._top_levels(levels, 1)
        if not tops:
            return 0.0, 0.0
        top = tops[0]
        return float(top.price), float(top.quantity)

    def update(self, state: MarketSimulationStateUpdate) -> None:
        """Keep accounts/config only. Base update copies 10 full synapses, reads
        CSV history, and logs every book — that never finished before the 7s nonce."""
        self.simulation_config = getattr(state, "config", getattr(self, "simulation_config", None))
        try:
            self.accounts = state.accounts[self.uid]
        except Exception:
            self.accounts = {}
        try:
            self.events = state.notices[self.uid]
        except Exception:
            self.events = []

    def report(self, state: MarketSimulationStateUpdate, response) -> None:
        n = len(getattr(response, "instructions", None) or [])
        bt.logging.info(f"MM reply instructions={n}")

    def _depth_imbalance(self, book, timestamp: int) -> float:
        bids = self._top_levels(book.bids, self.imbalance_depth)
        asks = self._top_levels(book.asks, self.imbalance_depth)
        bv = sum(float(x.quantity) for x in bids)
        av = sum(float(x.quantity) for x in asks)
        tot = bv + av
        return (bv - av) / tot if tot > 0 else 0.0

    def _trade_imbalance(self, book) -> float:
        try:
            signed = float(book.trade_imbalance)
            tot = abs(signed)
            return signed / tot if tot > 0 else 0.0
        except Exception:
            return 0.0

    def _update_vol(self, book_id: int, mid: float) -> float:
        prev = self._mid.get(book_id)
        if prev and prev > 0:
            ret = (mid / prev) - 1.0
            prev_var = self._var.get(book_id, ret * ret)
            self._var[book_id] = (1.0 - self.vol_lambda) * prev_var + self.vol_lambda * ret * ret
        elif book_id not in self._var:
            self._var[book_id] = 1e-8
        self._mid[book_id] = mid
        return math.sqrt(max(self._var[book_id], 1e-12))

    def _avellaneda_stoikov(
        self,
        fair: float,
        inventory: float,
        vol: float,
        mid: float,
        spread: float,
        tick: float,
    ) -> tuple[float, float]:
        """Avellaneda & Stoikov (2008) reservation price and half-spread.

        r = s - q γ σ² τ
        optimal spread = γ σ² τ + (2/γ) ln(1 + γ/κ)

        σ is EWMA return vol mapped to price units. κ is book fill intensity.
        A small spread-scaled term keeps quotes inside a live book when σ is tiny.
        """
        sigma_p = max(vol * mid, tick)
        sigma2 = sigma_p * sigma_p
        tau = 1.0
        scale = max(mid, tick)
        reservation = fair - inventory * self.gamma * sigma2 * tau / scale
        reservation -= inventory * 0.35 * self.gamma * spread
        kappa = max(self.kappa_liq, 1e-6)
        gamma = max(self.gamma, 1e-6)
        opt_spread = (gamma * sigma2 * tau / scale) + (2.0 / gamma) * math.log(1.0 + gamma / kappa)
        half = max(tick, 0.22 * spread + 0.18 * abs(inventory) * spread + 0.08 * opt_spread)
        return reservation, half

    def _cancel_side(self, response, book_id: int, orders: list, delay: int = 0) -> None:
        for order in orders:
            oid = getattr(order, "id", None)
            if oid is not None:
                response.cancel_order(book_id, oid, delay=delay)

    def _same_price(self, a: float, b: float, tick: float) -> bool:
        return abs(a - b) < 0.51 * tick

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = self.make_response()
        price_decimals, volume_decimals = self._decimals()
        tick = self._tick(price_decimals)
        min_size = float(getattr(state.config, "min_order_size", 0.0) or 0.0)
        books = state.books or {}

        if not self._logged_capital:
            sc = self.simulation_config
            sample = next(iter(self.accounts.values()), None) if self.accounts else None
            bt.logging.info(
                f"capital type={getattr(sc, 'miner_capital_type', None)} "
                f"wealth={getattr(sc, 'miner_wealth', None)} "
                f"base0={getattr(sc, 'miner_base_balance', None)} "
                f"quote0={getattr(sc, 'miner_quote_balance', None)} "
                f"price0={getattr(sc, 'init_price', None)} "
                f"sample_base={getattr(getattr(sample, 'base_balance', None), 'total', None)} "
                f"sample_quote={getattr(getattr(sample, 'quote_balance', None), 'total', None)}"
            )
            self._logged_capital = True

        quoted = 0
        flattened = 0
        taken = 0
        skipped = 0
        pulled = 0
        abs_inv = 0.0
        loaded = 0
        mtm = 0.0
        volume = 0.0
        n_scored = 0
        deadline = time.monotonic() + max(0.4, self._tick_budget_s)
        truncated = 0
        seen = 0

        for book_id, book in books.items():
            if time.monotonic() >= deadline:
                truncated = max(0, len(books) - seen)
                break
            seen += 1
            if book_id not in self.accounts or not book.bids or not book.asks:
                continue

            acct = self.accounts[book_id]
            maker_fee = float(getattr(getattr(acct, "fees", None), "maker_fee_rate", 0.0) or 0.0)
            if maker_fee > self.max_fee_rate:
                skipped += 1
                continue

            bid, bid_qty = self._touch_qty(book.bids)
            ask, ask_qty = self._touch_qty(book.asks)
            if ask <= bid or bid <= 0:
                continue

            mid = 0.5 * (bid + ask)
            spread = ask - bid
            touch_tot = bid_qty + ask_qty
            touch_imb = (bid_qty - ask_qty) / touch_tot if touch_tot > 0 else 0.0
            depth_imb = max(-1.0, min(1.0, self._depth_imbalance(book, state.timestamp)))
            trade_imb = max(-1.0, min(1.0, self._trade_imbalance(book)))
            vol = self._update_vol(book_id, mid)

            micro = (ask * bid_qty + bid * ask_qty) / touch_tot if touch_tot > 0 else mid
            flow = 0.50 * touch_imb + 0.32 * depth_imb + 0.18 * trade_imb
            fair = micro + 0.45 * flow * spread

            pos = self._net_position(book_id)
            inv = max(-1.0, min(1.0, pos / max(self.max_inventory, 1e-9)))
            reservation, half = self._avellaneda_stoikov(
                fair, inv, vol, mid, spread, tick
            )
            buy_px = self._px(reservation - half, price_decimals)
            sell_px = self._px(reservation + half, price_decimals)

            # Stay maker-side unless we explicitly take.
            buy_px = self._px(min(buy_px, ask - tick, bid + tick), price_decimals)
            sell_px = self._px(max(sell_px, bid + tick, ask - tick), price_decimals)
            if buy_px <= 0 or sell_px <= 0 or buy_px >= sell_px:
                buy_px = self._px(bid, price_decimals)
                sell_px = self._px(ask, price_decimals)
                if buy_px >= sell_px:
                    continue

            # Kappa-3 is realized round-trip PnL with low downside. Pull the
            # side informed flow would pick off; lean into inventory reduction.
            want_buy = True
            want_sell = True
            if flow >= self.pull_z and inv > -0.20:
                want_buy = False
            if flow <= -self.pull_z and inv < 0.20:
                want_sell = False
            if inv > self.reduce_z:
                want_sell = True
                if inv > self.heavy_z:
                    want_buy = False
                sell_px = self._px(min(sell_px, ask - tick), price_decimals)
            elif inv < -self.reduce_z:
                want_buy = True
                if inv < -self.heavy_z:
                    want_sell = False
                buy_px = self._px(max(buy_px, bid + tick), price_decimals)
            if buy_px <= 0 or sell_px <= 0 or buy_px >= sell_px:
                buy_px = self._px(bid, price_decimals)
                sell_px = self._px(ask, price_decimals)

            n_scored += 1
            abs_inv += abs(inv)
            if abs(inv) > 0.10:
                loaded += 1
            mtm += self._mark_wealth(book_id, mid)
            volume += float(getattr(acct, "traded_volume", 0.0) or 0.0)

            open_orders = list(getattr(acct, "orders", []) or [])
            buys = [o for o in open_orders if int(getattr(o, "side", -1)) == int(OrderDirection.BUY)]
            sells = [o for o in open_orders if int(getattr(o, "side", -1)) == int(OrderDirection.SELL)]

            quote_free = float(acct.quote_balance.free)
            base_free = float(acct.base_balance.free)

            # --- flatten / stop: inventory is the Kappa killer ---
            if abs(inv) >= self.flat_z:
                entry = self._entry_mid.get(book_id, mid)
                long_stop = pos > 0 and mid < entry * (1.0 - self.stop_bps * 1e-4)
                short_stop = pos < 0 and mid > entry * (1.0 + self.stop_bps * 1e-4)
                unwind_qty = self._qty(abs(pos), volume_decimals, min_size)
                self._cancel_side(response, book_id, buys + sells, delay=0)
                if unwind_qty > 0:
                    if pos > 0 and (base_free >= unwind_qty):
                        if long_stop:
                            response.market_order(
                                book_id=book_id,
                                direction=OrderDirection.SELL,
                                quantity=unwind_qty,
                                stp=STP.CANCEL_OLDEST,
                                delay=1,
                            )
                        else:
                            response.limit_order(
                                book_id=book_id,
                                direction=OrderDirection.SELL,
                                quantity=unwind_qty,
                                price=self._px(ask, price_decimals),
                                stp=STP.CANCEL_BOTH,
                                timeInForce=TimeInForce.GTT,
                                expiryPeriod=self.expiry_period,
                                postOnly=False,
                                delay=1,
                            )
                    elif pos < 0 and quote_free >= unwind_qty * bid:
                        if short_stop:
                            response.market_order(
                                book_id=book_id,
                                direction=OrderDirection.BUY,
                                quantity=unwind_qty,
                                stp=STP.CANCEL_OLDEST,
                                delay=1,
                            )
                        else:
                            response.limit_order(
                                book_id=book_id,
                                direction=OrderDirection.BUY,
                                quantity=unwind_qty,
                                price=self._px(bid, price_decimals),
                                stp=STP.CANCEL_BOTH,
                                timeInForce=TimeInForce.GTT,
                                expiryPeriod=self.expiry_period,
                                postOnly=False,
                                delay=1,
                            )
                flattened += 1
                self._quoted.pop(book_id, None)
                continue

            # --- rare take: only when flow is strong and inventory agrees ---
            if abs(flow) >= self.take_z and inv * math.copysign(1.0, flow) <= 0.25:
                take_qty = self._qty(self.quote_size * 0.55, volume_decimals, min_size)
                if flow > 0 and inv < 0.45 and quote_free >= take_qty * ask:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.BUY,
                        quantity=take_qty,
                        price=self._px(ask, price_decimals),
                        stp=STP.CANCEL_OLDEST,
                        timeInForce=TimeInForce.IOC,
                        delay=0,
                    )
                    self._entry_mid[book_id] = mid
                    taken += 1
                    continue
                if flow < 0 and inv > -0.45 and base_free >= take_qty:
                    response.limit_order(
                        book_id=book_id,
                        direction=OrderDirection.SELL,
                        quantity=take_qty,
                        price=self._px(bid, price_decimals),
                        stp=STP.CANCEL_OLDEST,
                        timeInForce=TimeInForce.IOC,
                        delay=0,
                    )
                    self._entry_mid[book_id] = mid
                    taken += 1
                    continue

            # --- two-sided making; skip replace if quotes are still valid ---
            prev = self._quoted.get(book_id)
            keep_buy = (
                prev
                and buys
                and self._same_price(float(getattr(buys[0], "price", 0.0)), buy_px, tick)
            )
            keep_sell = (
                prev
                and sells
                and self._same_price(float(getattr(sells[0], "price", 0.0)), sell_px, tick)
            )
            if not want_buy:
                keep_buy = False
                self._cancel_side(response, book_id, buys, delay=0)
                buys = []
            if not want_sell:
                keep_sell = False
                self._cancel_side(response, book_id, sells, delay=0)
                sells = []
            if not want_buy and not want_sell:
                self._cancel_side(response, book_id, buys + sells, delay=0)
                self._quoted.pop(book_id, None)
                pulled += 1
                continue

            sides_ready = (not want_buy or keep_buy) and (not want_sell or keep_sell)
            if sides_ready and abs(inv) < self.reduce_z:
                skipped += 1
                continue

            if not keep_buy:
                self._cancel_side(response, book_id, buys, delay=0)
            if not keep_sell:
                self._cancel_side(response, book_id, sells, delay=0)

            buy_scale = max(0.30, 1.0 - 0.75 * inv)
            sell_scale = max(0.30, 1.0 + 0.75 * inv)
            buy_qty = self._qty(self.quote_size * buy_scale, volume_decimals, min_size)
            sell_qty = self._qty(self.quote_size * sell_scale, volume_decimals, min_size)
            buy_delay = 0 if inv < -self.reduce_z else 1
            sell_delay = 0 if inv > self.reduce_z else 1

            if want_buy and (not keep_buy) and inv < 0.90 and quote_free >= buy_qty * buy_px:
                response.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.BUY,
                    quantity=buy_qty,
                    price=buy_px,
                    stp=STP.CANCEL_BOTH,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=self.expiry_period,
                    postOnly=True,
                    delay=buy_delay,
                )
                self._entry_mid.setdefault(book_id, mid)
            if want_sell and (not keep_sell) and inv > -0.90 and base_free >= sell_qty:
                response.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.SELL,
                    quantity=sell_qty,
                    price=sell_px,
                    stp=STP.CANCEL_BOTH,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=self.expiry_period,
                    postOnly=True,
                    delay=sell_delay,
                )
                self._entry_mid.setdefault(book_id, mid)

            self._quoted[book_id] = (buy_px, sell_px)
            if not want_buy or not want_sell:
                pulled += 1
            quoted += 1

        avg_inv = abs_inv / n_scored if n_scored else 0.0
        bt.logging.info(
            f"MM tick books={len(books)} quoted={quoted} flat={flattened} "
            f"take={taken} keep={skipped} pull={pulled} "
            f"inv={avg_inv:.2f} loaded={loaded} mtm={mtm:.3f} vol={volume:.1f}"
            f"{' truncated=' + str(truncated) if truncated else ''}"
        )
        self._save_state()
        return response


if __name__ == "__main__":
    launch(TradingMinerAgent)
