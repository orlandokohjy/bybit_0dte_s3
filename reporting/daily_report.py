"""
Daily and weekly performance reports.

Reads the trade log CSV, computes quant metrics over the full history
(daily) or the current ISO week (weekly), and formats Telegram-ready
HTML reports.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0


@dataclass
class TradeRow:
    date: str
    net_pnl: float
    capital_before: float
    capital_after: float
    spot_entry: float
    spot_exit: float
    put_premium_entry: float
    put_premium_exit: float
    num_straddles: int
    straddle_cost: float
    exit_reason: str
    spot_margin_used: float = 0.0
    put_premium_cost: float = 0.0
    total_capital_used: float = 0.0
    put_strike: float = 0.0


@dataclass
class DailyMetrics:
    # Today's trade
    trade_date: str
    trade_pnl: float
    trade_return_pct: float
    spot_entry: float
    spot_exit: float
    spot_move_pct: float
    num_straddles: int

    # Portfolio state
    equity: float
    initial_capital: float

    # Cumulative
    total_trades: int
    total_pnl: float
    cumulative_return_pct: float

    # Win/loss
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    best_trade: float
    worst_trade: float

    # Streaks
    current_streak: int          # positive = wins, negative = losses
    max_win_streak: int
    max_loss_streak: int

    # Risk-adjusted
    sharpe_ratio: float          # annualised
    sortino_ratio: float         # annualised
    calmar_ratio: float

    # Drawdown
    max_drawdown_pct: float
    current_drawdown_pct: float
    high_water_mark: float

    # Expectancy
    expectancy: float            # avg $ per trade
    expectancy_ratio: float      # expectancy / avg loss (reward-to-risk)

    # Volatility
    daily_vol: float             # stdev of daily returns
    annualised_vol: float

    # Option premium (today's trade)
    put_premium_entry: float
    put_premium_exit: float

    # Capital / Margin (today's trade)
    spot_margin_used: float
    put_premium_cost: float
    total_capital_used: float
    put_strike: float


def _load_trades() -> list[TradeRow]:
    path = config.TRADE_LOG_FILE
    if not os.path.exists(path):
        return []
    trades: list[TradeRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trades.append(TradeRow(
                    date=row["date"],
                    net_pnl=float(row["net_pnl"]),
                    capital_before=float(row["capital_before"]),
                    capital_after=float(row["capital_after"]),
                    spot_entry=float(row["spot_entry"]),
                    spot_exit=float(row["spot_exit"]),
                    put_premium_entry=float(row["put_premium_entry"]),
                    put_premium_exit=float(row["put_premium_exit"]),
                    num_straddles=int(row["num_straddles"]),
                    straddle_cost=float(row["straddle_cost"]),
                    exit_reason=row.get("exit_reason", ""),
                    spot_margin_used=float(row.get("spot_margin_used", 0)),
                    put_premium_cost=float(row.get("put_premium_cost", 0)),
                    total_capital_used=float(row.get("total_capital_used", 0)),
                    put_strike=float(row.get("put_strike", 0)),
                ))
            except (ValueError, KeyError):
                continue
    return trades


def _compute_drawdown_series(equities: list[float]) -> tuple[float, float, float]:
    """Returns (max_drawdown_pct, current_drawdown_pct, high_water_mark)."""
    if not equities:
        return 0.0, 0.0, config.INITIAL_CAPITAL_USD

    hwm = equities[0]
    max_dd = 0.0
    for eq in equities:
        hwm = max(hwm, eq)
        dd = (hwm - eq) / hwm if hwm > 0 else 0.0
        max_dd = max(max_dd, dd)

    current_hwm = max(equities)
    current_dd = (current_hwm - equities[-1]) / current_hwm if current_hwm > 0 else 0.0
    return max_dd, current_dd, current_hwm


def _compute_streaks(pnls: list[float]) -> tuple[int, int, int]:
    """Returns (current_streak, max_win_streak, max_loss_streak)."""
    if not pnls:
        return 0, 0, 0

    current = 0
    max_win = 0
    max_loss = 0
    streak = 0

    for p in pnls:
        if p >= 0:
            streak = streak + 1 if streak > 0 else 1
        else:
            streak = streak - 1 if streak < 0 else -1
        max_win = max(max_win, streak) if streak > 0 else max_win
        max_loss = min(max_loss, streak) if streak < 0 else max_loss

    return streak, max_win, abs(max_loss)


def compute_report(equity: float) -> Optional[DailyMetrics]:
    """Compute the full daily report from the trade log."""
    trades = _load_trades()
    if not trades:
        return None

    latest = trades[-1]
    pnls = [t.net_pnl for t in trades]
    returns = [t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0 for t in trades]
    equities = [config.INITIAL_CAPITAL_USD]
    for t in trades:
        equities.append(t.capital_after)

    # Win / loss breakdown
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    total = len(trades)

    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Streaks
    current_streak, max_win_streak, max_loss_streak = _compute_streaks(pnls)

    # Drawdown
    max_dd, current_dd, hwm = _compute_drawdown_series(equities)

    # Volatility and risk-adjusted metrics
    mean_ret = sum(returns) / len(returns) if returns else 0.0
    daily_vol = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5 if len(returns) > 1 else 0.0
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    sharpe = ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR) / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
              if daily_vol > 0 else 0.0)

    downside_returns = [r for r in returns if r < 0]
    downside_vol = ((sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
                    if downside_returns else 0.0)
    sortino = ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR) / downside_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
               if downside_vol > 0 else 0.0)

    ann_return = (equity / config.INITIAL_CAPITAL_USD) ** (TRADING_DAYS_PER_YEAR / max(total, 1)) - 1
    calmar = ann_return / max_dd if max_dd > 0 else 0.0

    # Expectancy
    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    # Today's trade
    trade_return = latest.net_pnl / latest.capital_before if latest.capital_before > 0 else 0.0
    spot_move = (latest.spot_exit - latest.spot_entry) / latest.spot_entry if latest.spot_entry > 0 else 0.0
    cum_return = (equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD

    return DailyMetrics(
        trade_date=latest.date,
        trade_pnl=latest.net_pnl,
        trade_return_pct=trade_return,
        spot_entry=latest.spot_entry,
        spot_exit=latest.spot_exit,
        spot_move_pct=spot_move,
        num_straddles=latest.num_straddles,
        equity=equity,
        initial_capital=config.INITIAL_CAPITAL_USD,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy,
        expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol,
        annualised_vol=ann_vol,
        put_premium_entry=latest.put_premium_entry,
        put_premium_exit=latest.put_premium_exit,
        spot_margin_used=latest.spot_margin_used,
        put_premium_cost=latest.put_premium_cost,
        total_capital_used=latest.total_capital_used,
        put_strike=latest.put_strike,
    )


def format_telegram_report(m: DailyMetrics) -> str:
    """Format the metrics into an HTML Telegram message."""

    streak_emoji = ""
    if m.current_streak > 0:
        streak_emoji = f" ({m.current_streak}W)"
    elif m.current_streak < 0:
        streak_emoji = f" ({abs(m.current_streak)}L)"

    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    leverage = config.SPOT_LEVERAGE
    notional = m.spot_entry * config.QTY_PER_LEG * m.num_straddles if m.spot_entry else 0

    spot_btc = config.QTY_PER_LEG * m.num_straddles
    num_puts = config.NUM_PUTS * m.num_straddles
    put_btc = config.QTY_PER_LEG * num_puts
    total_btc = spot_btc + put_btc
    entry_spot_usd = spot_btc * m.spot_entry
    entry_put_usd = put_btc * m.spot_entry
    exit_spot_usd = spot_btc * m.spot_exit
    exit_put_usd = put_btc * m.spot_exit

    lines = [
        f"<b>DAILY REPORT — {m.trade_date}</b>",
        "",
        "<b>Today's Trade</b>",
        f"  P&L: {pnl_sign}${m.trade_pnl:,.2f} ({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Spot: ${m.spot_entry:,.0f} → ${m.spot_exit:,.0f} ({m.spot_move_pct:+.2%})",
        f"  Option: ${m.put_premium_entry:,.2f} → ${m.put_premium_exit:,.2f}",
        f"  Put strike: ${m.put_strike:,.0f}",
        f"  Straddles: {m.num_straddles}",
        "",
        "<b>Volume</b>",
        f"  Spot: {m.num_straddles} × {config.QTY_PER_LEG} = {spot_btc:.1f} BTC",
        f"  Puts: {num_puts} × {config.QTY_PER_LEG} = {put_btc:.1f} BTC",
        "",
        f"  <b>Entry</b> (@ ${m.spot_entry:,.0f})",
        f"    Spot: ${entry_spot_usd:,.0f} | Puts: ${entry_put_usd:,.0f}",
        f"    Total: {total_btc:.1f} BTC / ${entry_spot_usd + entry_put_usd:,.0f}",
        f"  <b>Exit</b> (@ ${m.spot_exit:,.0f})",
        f"    Spot: ${exit_spot_usd:,.0f} | Puts: ${exit_put_usd:,.0f}",
        f"    Total: {total_btc:.1f} BTC / ${exit_spot_usd + exit_put_usd:,.0f}",
        "",
        "<b>Capital Required (this trade)</b>",
        f"  Spot margin ({leverage}×): ${m.spot_margin_used:,.2f}",
        f"    Notional: ${notional:,.2f} / {leverage}× = ${m.spot_margin_used:,.2f}",
        f"  Option premium: ${m.put_premium_cost:,.2f}",
        f"    ({config.NUM_PUTS} puts × {config.QTY_PER_LEG} BTC × ${m.put_premium_cost / max(config.NUM_PUTS * config.QTY_PER_LEG * m.num_straddles, 1):,.0f})",
        f"  <b>Total deployed: ${m.total_capital_used:,.2f}</b>",
        f"  Equity: ${m.equity + m.trade_pnl:,.2f} → ${m.equity:,.2f}",
        "",
        "<b>Portfolio</b>",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative P&L: ${m.total_pnl:,.2f} ({m.cumulative_return_pct:+.1%})",
        f"  High Water Mark: ${m.high_water_mark:,.2f}",
        "",
        "<b>Win/Loss ({} trades)</b>".format(m.total_trades),
        f"  Win rate: {m.win_rate:.1%} ({m.wins}W / {m.losses}L){streak_emoji}",
        f"  Avg win: ${m.avg_win:,.2f} | Avg loss: ${m.avg_loss:,.2f}",
        f"  Best: ${m.best_trade:,.2f} | Worst: ${m.worst_trade:,.2f}",
        f"  Profit factor: {m.profit_factor:.2f}",
        f"  Streaks: {m.max_win_streak}W max / {m.max_loss_streak}L max",
        "",
        "<b>Risk Metrics</b>",
        f"  Sharpe: {m.sharpe_ratio:.2f}",
        f"  Sortino: {m.sortino_ratio:.2f}",
        f"  Calmar: {m.calmar_ratio:.2f}",
        f"  Max DD: {m.max_drawdown_pct:.2%}",
        f"  Current DD: {m.current_drawdown_pct:.2%}",
        f"  Daily vol: {m.daily_vol:.2%} | Ann. vol: {m.annualised_vol:.1%}",
        "",
        "<b>Edge</b>",
        f"  Expectancy: ${m.expectancy:,.2f}/trade",
        f"  Expectancy ratio: {m.expectancy_ratio:.2f}",
    ]

    return "\n".join(lines)


def format_telegram_summary(m: DailyMetrics) -> str:
    """Short summary: today's trade + volume breakdown."""

    pnl_sign = "+" if m.trade_pnl >= 0 else ""
    spot_btc = config.QTY_PER_LEG * m.num_straddles
    num_puts = config.NUM_PUTS * m.num_straddles
    put_btc = config.QTY_PER_LEG * num_puts
    total_btc = spot_btc + put_btc

    entry_spot_usd = spot_btc * m.spot_entry
    entry_put_usd = put_btc * m.spot_entry
    entry_total_usd = entry_spot_usd + entry_put_usd

    exit_spot_usd = spot_btc * m.spot_exit
    exit_put_usd = put_btc * m.spot_exit
    exit_total_usd = exit_spot_usd + exit_put_usd

    lines = [
        f"<b>TRADE SUMMARY — {m.trade_date}</b>",
        "",
        "<b>Today's Trade</b>",
        f"  P&L: {pnl_sign}${m.trade_pnl:,.2f} ({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Spot: ${m.spot_entry:,.0f} → ${m.spot_exit:,.0f}",
        f"  Option: ${m.put_premium_entry:,.2f} → ${m.put_premium_exit:,.2f}",
        f"  Equity: ${m.equity:,.2f}",
        "",
        "<b>Volume</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Spot: {m.num_straddles} × {config.QTY_PER_LEG} = {spot_btc:.1f} BTC",
        f"  Options: {num_puts} × {config.QTY_PER_LEG} = {put_btc:.1f} BTC",
        "",
        f"  <b>Entry exposure</b> (@ ${m.spot_entry:,.0f})",
        f"    Spot: ${entry_spot_usd:,.0f}",
        f"    Options: ${entry_put_usd:,.0f}",
        f"    Total: {total_btc:.1f} BTC / ${entry_total_usd:,.0f}",
        "",
        f"  <b>Exit exposure</b> (@ ${m.spot_exit:,.0f})",
        f"    Spot: ${exit_spot_usd:,.0f}",
        f"    Options: ${exit_put_usd:,.0f}",
        f"    Total: {total_btc:.1f} BTC / ${exit_total_usd:,.0f}",
    ]

    return "\n".join(lines)


# ═══════════════════════ Weekly Report ═══════════════════════════════

def _monday_of_week(date_str: str) -> str:
    """Return the ISO Monday (YYYY-MM-DD) for a given trade date string."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def compute_weekly_report(equity: float) -> Optional[DailyMetrics]:
    """Compute a report scoped to the current ISO week (Mon-Fri)."""
    all_trades = _load_trades()
    if not all_trades:
        return None

    today = datetime.utcnow()
    week_monday = today - timedelta(days=today.weekday())
    week_start = week_monday.strftime("%Y-%m-%d")

    trades = [t for t in all_trades if _monday_of_week(t.date) == week_start]
    if not trades:
        return None

    pnls = [t.net_pnl for t in trades]
    returns = [t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0 for t in trades]

    equity_start = trades[0].capital_before
    equities = [equity_start]
    for t in trades:
        equities.append(t.capital_after)

    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    total = len(trades)

    win_rate = n_wins / total if total > 0 else 0.0
    avg_win = sum(wins) / n_wins if n_wins > 0 else 0.0
    avg_loss = sum(losses) / n_losses if n_losses > 0 else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    current_streak, max_win_streak, max_loss_streak = _compute_streaks(pnls)
    max_dd, current_dd, hwm = _compute_drawdown_series(equities)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    daily_vol = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5 if len(returns) > 1 else 0.0
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    sharpe = ((mean_ret / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
              if daily_vol > 0 else 0.0)

    downside_returns = [r for r in returns if r < 0]
    downside_vol = ((sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
                    if downside_returns else 0.0)
    sortino = ((mean_ret / downside_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
               if downside_vol > 0 else 0.0)

    weekly_return = sum(pnls) / equity_start if equity_start > 0 else 0.0
    calmar = (weekly_return * 52) / max_dd if max_dd > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    latest = trades[-1]
    cum_return = (equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD

    total_straddles = sum(t.num_straddles for t in trades)
    avg_spot_entry = sum(t.spot_entry * t.num_straddles for t in trades) / total_straddles if total_straddles else 0
    avg_spot_exit = sum(t.spot_exit * t.num_straddles for t in trades) / total_straddles if total_straddles else 0
    avg_put_entry = sum(t.put_premium_entry * t.num_straddles for t in trades) / total_straddles if total_straddles else 0
    avg_put_exit = sum(t.put_premium_exit * t.num_straddles for t in trades) / total_straddles if total_straddles else 0

    return DailyMetrics(
        trade_date=week_start,
        trade_pnl=sum(pnls),
        trade_return_pct=weekly_return,
        spot_entry=avg_spot_entry,
        spot_exit=avg_spot_exit,
        spot_move_pct=(avg_spot_exit - avg_spot_entry) / avg_spot_entry if avg_spot_entry else 0,
        num_straddles=total_straddles,
        equity=equity,
        initial_capital=config.INITIAL_CAPITAL_USD,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins,
        losses=n_losses,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy,
        expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol,
        annualised_vol=ann_vol,
        put_premium_entry=avg_put_entry,
        put_premium_exit=avg_put_exit,
        spot_margin_used=sum(t.spot_margin_used for t in trades),
        put_premium_cost=sum(t.put_premium_cost for t in trades),
        total_capital_used=sum(t.total_capital_used for t in trades),
        put_strike=latest.put_strike,
    )


def format_weekly_report(m: DailyMetrics) -> str:
    """Format weekly metrics into a Telegram HTML message for the group chat."""

    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    spot_btc = config.QTY_PER_LEG * m.num_straddles
    num_puts = config.NUM_PUTS * m.num_straddles
    put_btc = config.QTY_PER_LEG * num_puts
    total_btc = spot_btc + put_btc

    spot_usd = spot_btc * m.spot_entry if m.spot_entry else 0
    option_usd = put_btc * m.spot_entry if m.spot_entry else 0

    lines = [
        f"<b>WEEKLY REPORT — Week of {m.trade_date}</b>",
        "",
        f"  Weekly P&L: {pnl_sign}${m.trade_pnl:,.2f} ({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Trades: {m.total_trades} ({m.wins}W / {m.losses}L)",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative: {m.cumulative_return_pct:+.1%}",
        "",
        "<b>Volume (this week)</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Spot: {spot_btc:.1f} BTC / ${spot_usd:,.0f}",
        f"  Options: {put_btc:.1f} BTC / ${option_usd:,.0f}",
    ]

    return "\n".join(lines)
