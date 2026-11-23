"""cbBTC trend bot for make.fun's btc-race-24h (agent-fun/bars-replay/v1).

Long-only EMA crossover on 1-minute bars, in the spirit of freqtrade's
AverageStrategy: enter when the fast EMA crosses above the slow EMA, then
leave on a take-profit or a stop-loss. On 1-minute bars the opposite cross
fires far too often to pay for the round trip, so it is not an exit here.
Orders are decided on a bar's close and fill at the next bar's open under
the replay's fee, slippage and pool-impact model, so the stake is sized
against the weight cap and exits are set well outside the ~26 bps a
full-size round trip costs.

Deterministic: float64 EMA arithmetic only, no clock, no environment, no
randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

FAST_SPAN = 20
SLOW_SPAN = 80
# Take-profit: the position is worth at least 26/25 of its cost (+4%).
TAKE_PROFIT = (26, 25)
# Stop-loss: the position is worth at most 49/50 of its cost (-2%).
STOP_LOSS = (49, 50)
# Stake per entry: this fraction of equity, before the caps.
STAKE_NUM, STAKE_DEN = 95, 100
MAX_SLIPPAGE_BPS = 100


class Ema:
    """An EMA seeded as the mean of its first span of closes."""

    __slots__ = ("span", "warm", "value")

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def step(self, close):
        if self.value is not None:
            self.value += (2.0 / (self.span + 1)) * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span


class Series:
    __slots__ = ("fast", "slow", "gap", "now", "down")

    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.gap = None  # fast - slow after the previous bar
        self.now = None  # fast - slow after this bar
        self.down = False  # a live cross-down sell signal


_SERIES = {}


def _push(bar):
    s = _SERIES.get(bar["symbol"])
    if s is None:
        s = _SERIES[bar["symbol"]] = Series()
    close = float(int(bar["c"]))
    s.fast.step(close)
    s.slow.step(close)
    s.gap = s.now
    if s.fast.value is not None and s.slow.value is not None:
        s.now = s.fast.value - s.slow.value
    if s.now is None or s.now >= 0:
        s.down = False
    return s


def _exits(state, symbol, held):
    value = state.value(symbol, held.qty)
    return (
        value * TAKE_PROFIT[1] >= held.cost * TAKE_PROFIT[0]
        or value * STOP_LOSS[1] <= held.cost * STOP_LOSS[0]
    )


def on_batch(state, batch):
    cross_up = set()
    fresh = set()
    for bar in batch["bars"]:
        s = _push(bar)
        fresh.add(bar["symbol"])
        if s.gap is None or s.now is None or bar.get("v") == "0":
            continue
        if s.now > 0 and s.gap <= 0:
            cross_up.add(bar["symbol"])
    if state.warming_up:
        return []

    caps = state.hello["caps"]
    fees = state.hello["fees"]
    min_notional = int(fees["min_notional_quote_units"])

    sells = []
    entries = []
    for info in state.hello["symbols"]:
        symbol = info["symbol"]
        if symbol not in fresh:
            continue
        held = state.positions.get(symbol)
        if held is not None:
            if _exits(state, symbol, held):
                sells.append(
                    {"side": "sell", "symbol": symbol, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}
                )
        elif symbol in cross_up:
            entries.append(symbol)

    orders = sells[: caps["max_orders_per_bar"]]
    cash = state.cash
    for symbol in entries:
        if len(orders) >= caps["max_orders_per_bar"]:
            break
        stake = state.equity * STAKE_NUM // STAKE_DEN
        room = state.weight_room(symbol)
        if room < stake:
            stake = room
        if cash < stake:
            stake = cash
        if stake < min_notional or stake <= 0:
            continue
        orders.append(
            {"side": "buy", "symbol": symbol, "notional_quote_units": stake,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}
        )
        cash -= stake
    return orders


makefun_bars.run(on_batch)
