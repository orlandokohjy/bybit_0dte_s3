"""
Daily and weekly performance reports for the multi-session Bybit 0DTE algo.

A "trading day" = one 08:00 UTC option expiry. Both sessions of a trading
day (afternoon Mon + morning Tue → Tuesday's expiry) roll up into the
same daily report.

Daily report fires CHAINED off the LAST_CLOSE session's `_on_close`
handler in main.py — i.e. immediately after the morning session ends.
The Risk Metrics and Edge sections have been removed per user request.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)

TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0


@dataclass
class ExecutionMetrics:
    duration_sec: float = 0.0
    attempts: int = 0
    ref_mark: float = 0.0
    ref_quote: float = 0.0
    slippage_vs_mark_pct: float = 0.0
    saved_vs_taker_usd: float = 0.0


@dataclass
class TradeRow:
    date: str
    trading_day: str
    session_name: str
    entry_time: str
    net_pnl: float
    capital_before: float
    capital_after: float
    spot_entry: float
    spot_exit: float
    put_premium_entry: float
    put_premium_exit: float
    num_straddles: int
    qty_per_leg: float
    straddle_cost: float
    exit_reason: str
    spot_margin_used: float = 0.0
    put_premium_cost: float = 0.0
    total_capital_used: float = 0.0
    put_strike: float = 0.0
    spot_entry_exec: Optional[ExecutionMetrics] = None
    put_entry_exec: Optional[ExecutionMetrics] = None
    spot_exit_exec: Optional[ExecutionMetrics] = None
    put_exit_exec: Optional[ExecutionMetrics] = None


@dataclass
class DailyMetrics:
    # Trading-day identifier (08:00 UTC expiry date)
    trade_date: str
    trade_pnl: float
    trade_return_pct: float
    spot_entry: float
    spot_exit: float
    spot_move_pct: float
    num_straddles: int

    equity: float
    initial_capital: float

    total_trades: int
    total_pnl: float
    cumulative_return_pct: float

    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    best_trade: float
    worst_trade: float

    current_streak: int
    max_win_streak: int
    max_loss_streak: int

    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    max_drawdown_pct: float
    current_drawdown_pct: float
    high_water_mark: float

    expectancy: float
    expectancy_ratio: float

    daily_vol: float
    annualised_vol: float

    put_premium_entry: float
    put_premium_exit: float

    spot_margin_used: float
    put_premium_cost: float
    total_capital_used: float
    put_strike: float

    starting_equity: float = 0.0

    # Multi-session: today's trading-day breakdown
    today_trades: list = field(default_factory=list)

    spot_entry_exec: Optional[ExecutionMetrics] = None
    put_entry_exec: Optional[ExecutionMetrics] = None
    spot_exit_exec: Optional[ExecutionMetrics] = None
    put_exit_exec: Optional[ExecutionMetrics] = None


# ─────────────────────── helpers ──────────────────────────────────

def _f(row: dict, key: str, default: float = 0.0) -> float:
    raw = row.get(key, "")
    if raw == "" or raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _i(row: dict, key: str, default: int = 0) -> int:
    raw = row.get(key, "")
    if raw == "" or raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _exec_from_row(
    row: dict, prefix: str, quote_key: str,
) -> ExecutionMetrics:
    return ExecutionMetrics(
        duration_sec=_f(row, f"{prefix}_duration_sec"),
        attempts=_i(row, f"{prefix}_attempts"),
        ref_mark=_f(row, f"{prefix}_ref_mark"),
        ref_quote=_f(row, f"{prefix}_{quote_key}"),
        slippage_vs_mark_pct=_f(row, f"{prefix}_slippage_vs_mark_pct"),
        saved_vs_taker_usd=_f(row, f"{prefix}_saved_vs_taker_usd"),
    )


def _trading_day_from_entry_time(
    entry_time_iso: str, fallback_date: str,
) -> str:
    """Derive the trading day (= 08:00 UTC option expiry date) from a row's
    entry_time. Used as a backfill when the CSV's `trading_day` column is
    blank (legacy rows)."""
    if not entry_time_iso:
        return fallback_date
    try:
        dt = datetime.fromisoformat(entry_time_iso)
    except Exception:
        return fallback_date
    return config.trading_day_for(dt).isoformat()


def _session_from_entry_time(entry_time_iso: str) -> str:
    """Backfill session name from entry time when the CSV column is empty.
    Compares the entry hour to each Session's entry_utc."""
    if not entry_time_iso:
        return ""
    try:
        dt = datetime.fromisoformat(entry_time_iso)
    except Exception:
        return ""
    h, m = dt.hour, dt.minute
    best: Optional[config.Session] = None
    best_diff = math.inf
    for s in config.SESSIONS:
        diff = abs((h * 60 + m) - (s.entry_utc.hour * 60 + s.entry_utc.minute))
        if diff < best_diff:
            best_diff = diff
            best = s
    return best.name if best else ""


def _session_chronological_key(t: TradeRow) -> tuple:
    """Sort key within a trading_day. Afternoon (offset=+1) precedes
    morning (offset=0). Within a session, sort by entry_time."""
    sess = next((s for s in config.SESSIONS if s.name == t.session_name), None)
    pos = sess.trading_day_close_position if sess else 99
    return (pos, t.entry_time)


def _session_time_label(name: str) -> str:
    sess = next((s for s in config.SESSIONS if s.name == name), None)
    return sess.time_label if sess else name


def _load_trades() -> list[TradeRow]:
    path = config.TRADE_LOG_FILE
    if not os.path.exists(path):
        return []
    trades: list[TradeRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date = row.get("date", "")
                entry_time = row.get("entry_time", "")
                trading_day = (
                    row.get("trading_day", "")
                    or _trading_day_from_entry_time(entry_time, date)
                )
                session_name = (
                    row.get("session", "")
                    or _session_from_entry_time(entry_time)
                )
                trades.append(TradeRow(
                    date=date,
                    trading_day=trading_day,
                    session_name=session_name,
                    entry_time=entry_time,
                    net_pnl=float(row["net_pnl"]),
                    capital_before=float(row["capital_before"]),
                    capital_after=float(row["capital_after"]),
                    spot_entry=float(row["spot_entry"]),
                    spot_exit=float(row["spot_exit"]),
                    put_premium_entry=float(row["put_premium_entry"]),
                    put_premium_exit=float(row["put_premium_exit"]),
                    num_straddles=int(row["num_straddles"]),
                    qty_per_leg=_f(row, "qty_per_leg",
                                   default=config.QTY_PER_LEG),
                    straddle_cost=float(row["straddle_cost"]),
                    exit_reason=row.get("exit_reason", ""),
                    spot_margin_used=_f(row, "spot_margin_used"),
                    put_premium_cost=_f(row, "put_premium_cost"),
                    total_capital_used=_f(row, "total_capital_used"),
                    put_strike=_f(row, "put_strike"),
                    spot_entry_exec=_exec_from_row(row, "spot_entry", "ref_ask"),
                    put_entry_exec=_exec_from_row(row, "put_entry", "ref_ask"),
                    spot_exit_exec=_exec_from_row(row, "spot_exit", "ref_bid"),
                    put_exit_exec=_exec_from_row(row, "put_exit", "ref_bid"),
                ))
            except (ValueError, KeyError):
                continue
    return trades


def _compute_drawdown_series(equities: list[float]) -> tuple[float, float, float]:
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


def _inception_equity(trades: list[TradeRow]) -> float:
    """Return the algo's true starting capital for cumulative-return math.

    Uses the `capital_before` of the first trade ever logged, falling back
    to config.INITIAL_CAPITAL_USD if no trades have happened yet.
    """
    if not trades:
        return config.INITIAL_CAPITAL_USD
    first = trades[0]
    if first.capital_before > 0:
        return first.capital_before
    return config.INITIAL_CAPITAL_USD


def _current_trading_day() -> str:
    """The trading day "in flight" right now, based on the 08:00 UTC cutoff."""
    return config.trading_day_for(datetime.utcnow()).isoformat()


# ────────────────────────── compute ──────────────────────────────

def compute_report(
    equity: float, trading_day: str | None = None,
) -> Optional[DailyMetrics]:
    """Compute the daily report scoped to one trading day.

    If `trading_day` is None, defaults to the most recent trading day
    that has any trades in the log.
    """
    trades = _load_trades()
    if not trades:
        return None

    if trading_day is None:
        # Pick the trading_day of the most recent trade.
        trading_day = trades[-1].trading_day or trades[-1].date

    today_trades = [t for t in trades if t.trading_day == trading_day]
    if not today_trades:
        log.info("compute_report_no_trading_day",
                 trading_day=trading_day,
                 latest_in_log=trades[-1].trading_day)
        return None
    today_trades.sort(key=_session_chronological_key)

    pnls = [t.net_pnl for t in trades]
    returns = [
        t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
        for t in trades
    ]
    inception = _inception_equity(trades)
    equities = [inception]
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
    daily_vol = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5 \
        if len(returns) > 1 else 0.0
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    sharpe = ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR)
              / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
              if daily_vol > 0 else 0.0)

    downside_returns = [r for r in returns if r < 0]
    downside_vol = ((sum(r ** 2 for r in downside_returns) / len(downside_returns)) ** 0.5
                    if downside_returns else 0.0)
    sortino = ((mean_ret - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR)
               / downside_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
               if downside_vol > 0 else 0.0)

    ann_return = (equity / inception) ** (TRADING_DAYS_PER_YEAR / max(total, 1)) - 1 \
        if inception > 0 else 0.0
    calmar = ann_return / max_dd if max_dd > 0 else 0.0

    expectancy = sum(pnls) / total if total > 0 else 0.0
    expectancy_ratio = expectancy / abs(avg_loss) if avg_loss != 0 else 0.0

    # Aggregate today's (trading-day) numbers
    today_pnl = sum(t.net_pnl for t in today_trades)
    today_capital_before = today_trades[0].capital_before
    today_return = today_pnl / today_capital_before if today_capital_before > 0 else 0.0
    today_straddles = sum(t.num_straddles for t in today_trades)

    last = today_trades[-1]
    avg_spot_entry = (
        sum(t.spot_entry * t.num_straddles for t in today_trades) / today_straddles
    ) if today_straddles else last.spot_entry
    avg_spot_exit = (
        sum(t.spot_exit * t.num_straddles for t in today_trades) / today_straddles
    ) if today_straddles else last.spot_exit
    spot_move = (avg_spot_exit - avg_spot_entry) / avg_spot_entry \
        if avg_spot_entry > 0 else 0.0

    cum_return = (equity - inception) / inception if inception > 0 else 0.0

    return DailyMetrics(
        trade_date=trading_day,
        trade_pnl=today_pnl,
        trade_return_pct=today_return,
        spot_entry=avg_spot_entry,
        spot_exit=avg_spot_exit,
        spot_move_pct=spot_move,
        num_straddles=today_straddles,
        equity=equity,
        initial_capital=inception,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins, losses=n_losses, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe, sortino_ratio=sortino, calmar_ratio=calmar,
        max_drawdown_pct=max_dd, current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy, expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol, annualised_vol=ann_vol,
        put_premium_entry=last.put_premium_entry,
        put_premium_exit=last.put_premium_exit,
        spot_margin_used=sum(t.spot_margin_used for t in today_trades),
        put_premium_cost=sum(t.put_premium_cost for t in today_trades),
        total_capital_used=sum(t.total_capital_used for t in today_trades),
        put_strike=last.put_strike,
        starting_equity=today_capital_before,
        today_trades=today_trades,
        spot_entry_exec=last.spot_entry_exec,
        put_entry_exec=last.put_entry_exec,
        spot_exit_exec=last.spot_exit_exec,
        put_exit_exec=last.put_exit_exec,
    )


# ────────────────────────── format ───────────────────────────────

def _format_today_block(m: DailyMetrics) -> list[str]:
    """Detailed breakdown for today's trading day. Multi-session aware."""
    if not m.today_trades:
        return [
            "<b>Today's Trade</b>",
            f"  P&L: ${m.trade_pnl:,.2f}",
        ]

    lines: list[str] = ["<b>Today's Trades</b>"]
    if len(m.today_trades) > 1:
        # Multi-session: show 1st / 2nd entry
        ord_words = ["1st", "2nd", "3rd", "4th"]
        for idx, t in enumerate(m.today_trades):
            label = _session_time_label(t.session_name)
            ord_word = ord_words[idx] if idx < len(ord_words) else f"{idx + 1}th"
            sign = "+" if t.net_pnl >= 0 else ""
            ret = t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
            spot_move = (
                (t.spot_exit - t.spot_entry) / t.spot_entry
                if t.spot_entry > 0 else 0.0
            )
            lines.extend([
                f"  [{label}] {ord_word} entry",
                f"    P&L: {sign}${t.net_pnl:,.2f} ({sign}{ret:.2%})",
                f"    Spot: ${t.spot_entry:,.0f} → ${t.spot_exit:,.0f} "
                f"({spot_move:+.2%})",
                f"    Strike: ${t.put_strike:,.0f}  "
                f"Put: ${t.put_premium_entry:,.2f} → ${t.put_premium_exit:,.2f}",
                f"    Straddles: {t.num_straddles} × {t.qty_per_leg:.4f} BTC/leg",
            ])
        sign = "+" if m.trade_pnl >= 0 else ""
        lines.append(
            f"  <b>Trading day P&L: {sign}${m.trade_pnl:,.2f} "
            f"({sign}{m.trade_return_pct:.2%})</b>"
        )
    else:
        t = m.today_trades[0]
        label = _session_time_label(t.session_name) if t.session_name else ""
        header = f"  [{label}]" if label else "  Today"
        sign = "+" if m.trade_pnl >= 0 else ""
        lines.extend([
            header,
            f"    P&L: {sign}${m.trade_pnl:,.2f} "
            f"({sign}{m.trade_return_pct:.2%})",
            f"    Spot: ${m.spot_entry:,.0f} → ${m.spot_exit:,.0f} "
            f"({m.spot_move_pct:+.2%})",
            f"    Strike: ${m.put_strike:,.0f}  "
            f"Put: ${m.put_premium_entry:,.2f} → ${m.put_premium_exit:,.2f}",
            f"    Straddles: {t.num_straddles} × {t.qty_per_leg:.4f} BTC/leg",
        ])
    return lines


def _format_volume_block(m: DailyMetrics) -> list[str]:
    """Round-trip volume: opened + closed BTC for both spot and puts."""
    lines = ["<b>Volume</b>"]

    if len(m.today_trades) > 1:
        for idx, t in enumerate(m.today_trades):
            label = _session_time_label(t.session_name)
            ord_words = ["1st", "2nd", "3rd", "4th"]
            ord_word = ord_words[idx] if idx < len(ord_words) else f"{idx + 1}th"
            spot_btc = t.qty_per_leg * t.num_straddles
            put_btc = config.NUM_PUTS * t.qty_per_leg * t.num_straddles
            lines.append(
                f"  [{label}] {ord_word} entry: {t.num_straddles} × "
                f"{t.qty_per_leg:.4f} BTC spot + "
                f"{config.NUM_PUTS}×{t.qty_per_leg:.4f} BTC puts "
                f"= {spot_btc:.4f} BTC spot / {put_btc:.4f} BTC puts"
            )

    spot_opened = sum(t.qty_per_leg * t.num_straddles for t in m.today_trades)
    put_opened = sum(
        config.NUM_PUTS * t.qty_per_leg * t.num_straddles for t in m.today_trades
    )
    spot_closed = spot_opened   # 1× round-trip per trade
    put_closed = put_opened
    spot_total = spot_opened + spot_closed
    put_total = put_opened + put_closed

    lines.extend([
        f"  Spot: opened {spot_opened:.4f} BTC, closed {spot_closed:.4f} BTC",
        f"  Puts: opened {put_opened:.4f} BTC, closed {put_closed:.4f} BTC",
        f"  <b>Total traded notional</b> "
        f"(spot+puts, round-trip): {spot_total + put_total:.4f} BTC",
    ])
    return lines


def _format_exec_block(
    leg: str, side: str, fill_price: float,
    e: Optional[ExecutionMetrics],
) -> list[str]:
    if e is None or e.duration_sec <= 0:
        return []
    quote_label = "Ask" if side == "entry" else "Bid"
    return [
        f"  {leg.upper()} ({side})",
        f"    Time to fill: {e.duration_sec:.1f}s, {e.attempts} attempt(s)",
        f"    Mark at start: ${e.ref_mark:,.2f} → Fill: ${fill_price:,.2f}",
        f"    Slippage vs mark: {e.slippage_vs_mark_pct:+.2f}%",
        f"    {quote_label} at start: ${e.ref_quote:,.2f}  "
        f"Saved vs taker: ${e.saved_vs_taker_usd:+,.2f}",
    ]


def _format_execution_quality(m: DailyMetrics) -> list[str]:
    blocks: list[str] = []

    entry_blocks = []
    entry_blocks += _format_exec_block(
        "put", "entry", m.put_premium_entry, m.put_entry_exec,
    )
    entry_blocks += _format_exec_block(
        "spot", "entry", m.spot_entry, m.spot_entry_exec,
    )
    if entry_blocks:
        blocks.append("<b>Entry execution</b>")
        blocks += entry_blocks

    exit_blocks = []
    exit_blocks += _format_exec_block(
        "spot", "exit", m.spot_exit, m.spot_exit_exec,
    )
    exit_blocks += _format_exec_block(
        "put", "exit", m.put_premium_exit, m.put_exit_exec,
    )
    if exit_blocks:
        if blocks:
            blocks.append("")
        blocks.append("<b>Exit execution</b>")
        blocks += exit_blocks

    if not blocks:
        return []

    legs = [
        m.spot_entry_exec, m.put_entry_exec,
        m.spot_exit_exec, m.put_exit_exec,
    ]
    legs = [x for x in legs if x and x.duration_sec > 0]
    if legs:
        total_saved = sum(x.saved_vs_taker_usd for x in legs)
        total_attempts = sum(x.attempts for x in legs)
        avg_dur = sum(x.duration_sec for x in legs) / len(legs)
        avg_slip = sum(x.slippage_vs_mark_pct for x in legs) / len(legs)
        blocks.append("")
        blocks.append("<b>Execution summary</b>")
        blocks.append(
            f"  Avg time to fill: {avg_dur:.1f}s "
            f"({total_attempts} total attempts across {len(legs)} legs)"
        )
        blocks.append(f"  Avg slippage vs mark: {avg_slip:+.2f}%")
        blocks.append(f"  Total saved vs taker: ${total_saved:+,.2f}")

    return blocks


def format_telegram_report(m: DailyMetrics) -> str:
    """Daily report — Risk Metrics & Edge sections REMOVED per user request."""

    streak_emoji = ""
    if m.current_streak > 0:
        streak_emoji = f" ({m.current_streak}W)"
    elif m.current_streak < 0:
        streak_emoji = f" ({abs(m.current_streak)}L)"

    if m.starting_equity > 0:
        equity_line = (
            f"  Equity: ${m.starting_equity:,.2f} → ${m.equity:,.2f}"
        )
    else:
        equity_line = (
            f"  Equity: ${m.equity + m.trade_pnl:,.2f} → ${m.equity:,.2f}"
        )

    lines: list[str] = [
        f"<b>DAILY REPORT — {m.trade_date}</b>",
        "",
    ]
    lines.extend(_format_today_block(m))
    lines.append("")
    lines.extend(_format_volume_block(m))
    lines.append("")
    lines.extend([
        "<b>Capital Required (this trading day)</b>",
        f"  Spot margin ({config.SPOT_LEVERAGE}×): ${m.spot_margin_used:,.2f}",
        f"  Option premium: ${m.put_premium_cost:,.2f}",
        f"  <b>Total deployed: ${m.total_capital_used:,.2f}</b>",
        equity_line,
        "",
        "<b>Portfolio</b>",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative P&L: ${m.total_pnl:,.2f} ({m.cumulative_return_pct:+.1%})",
        f"  High Water Mark: ${m.high_water_mark:,.2f}",
        "",
        f"<b>Win/Loss ({m.total_trades} trades)</b>",
        f"  Win rate: {m.win_rate:.1%} ({m.wins}W / {m.losses}L){streak_emoji}",
        f"  Avg win: ${m.avg_win:,.2f} | Avg loss: ${m.avg_loss:,.2f}",
        f"  Best: ${m.best_trade:,.2f} | Worst: ${m.worst_trade:,.2f}",
        f"  Profit factor: {m.profit_factor:.2f}",
        f"  Streaks: {m.max_win_streak}W max / {m.max_loss_streak}L max",
    ])

    exec_lines = _format_execution_quality(m)
    if exec_lines:
        lines.append("")
        lines.extend(exec_lines)

    return "\n".join(lines)


def format_telegram_summary(m: DailyMetrics) -> str:
    """Slim summary — kept for ad-hoc use; NOT used in the production
    chained-report flow (production uses format_telegram_report)."""
    return format_telegram_report(m)


# ═══════════════════════ Weekly Report ═══════════════════════════════

def _monday_of_week(date_str: str) -> str:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def compute_weekly_report(equity: float) -> Optional[DailyMetrics]:
    """Compute a report scoped to the current ISO week (Mon-Sat). Bucketed
    by trading_day."""
    all_trades = _load_trades()
    if not all_trades:
        return None

    today = datetime.utcnow()
    week_monday = today - timedelta(days=today.weekday())
    week_start = week_monday.strftime("%Y-%m-%d")

    trades = [t for t in all_trades if _monday_of_week(t.trading_day or t.date)
              == week_start]
    if not trades:
        return None

    pnls = [t.net_pnl for t in trades]
    returns = [t.net_pnl / t.capital_before if t.capital_before > 0 else 0.0
               for t in trades]

    inception = _inception_equity(all_trades)
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
    daily_vol = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5 \
        if len(returns) > 1 else 0.0
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
    cum_return = (equity - inception) / inception if inception > 0 else 0.0

    total_straddles = sum(t.num_straddles for t in trades)
    avg_spot_entry = sum(t.spot_entry * t.num_straddles for t in trades) \
        / total_straddles if total_straddles else 0
    avg_spot_exit = sum(t.spot_exit * t.num_straddles for t in trades) \
        / total_straddles if total_straddles else 0
    avg_put_entry = sum(t.put_premium_entry * t.num_straddles for t in trades) \
        / total_straddles if total_straddles else 0
    avg_put_exit = sum(t.put_premium_exit * t.num_straddles for t in trades) \
        / total_straddles if total_straddles else 0

    return DailyMetrics(
        trade_date=week_start,
        trade_pnl=sum(pnls),
        trade_return_pct=weekly_return,
        spot_entry=avg_spot_entry,
        spot_exit=avg_spot_exit,
        spot_move_pct=(avg_spot_exit - avg_spot_entry) / avg_spot_entry
            if avg_spot_entry else 0,
        num_straddles=total_straddles,
        equity=equity,
        initial_capital=inception,
        total_trades=total,
        total_pnl=sum(pnls),
        cumulative_return_pct=cum_return,
        wins=n_wins, losses=n_losses, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss,
        profit_factor=profit_factor,
        best_trade=max(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        current_streak=current_streak,
        max_win_streak=max_win_streak, max_loss_streak=max_loss_streak,
        sharpe_ratio=sharpe, sortino_ratio=sortino, calmar_ratio=calmar,
        max_drawdown_pct=max_dd, current_drawdown_pct=current_dd,
        high_water_mark=hwm,
        expectancy=expectancy, expectancy_ratio=expectancy_ratio,
        daily_vol=daily_vol, annualised_vol=ann_vol,
        put_premium_entry=avg_put_entry,
        put_premium_exit=avg_put_exit,
        spot_margin_used=sum(t.spot_margin_used for t in trades),
        put_premium_cost=sum(t.put_premium_cost for t in trades),
        total_capital_used=sum(t.total_capital_used for t in trades),
        put_strike=latest.put_strike,
        starting_equity=trades[0].capital_before if trades else 0.0,
        today_trades=trades,
    )


def format_weekly_report(m: DailyMetrics) -> str:
    pnl_sign = "+" if m.trade_pnl >= 0 else ""

    spot_opened = sum(t.qty_per_leg * t.num_straddles for t in m.today_trades)
    put_opened = sum(
        config.NUM_PUTS * t.qty_per_leg * t.num_straddles for t in m.today_trades
    )
    spot_closed = spot_opened
    put_closed = put_opened
    spot_total = spot_opened + spot_closed
    put_total = put_opened + put_closed

    lines = [
        f"<b>WEEKLY REPORT — Week of {m.trade_date}</b>",
        "",
        f"  Weekly P&L: {pnl_sign}${m.trade_pnl:,.2f} "
        f"({pnl_sign}{m.trade_return_pct:.2%})",
        f"  Trades: {m.total_trades} ({m.wins}W / {m.losses}L)",
        f"  Equity: ${m.equity:,.2f}",
        f"  Cumulative: {m.cumulative_return_pct:+.1%}",
        "",
        "<b>Volume (this week)</b>",
        f"  Straddles: {m.num_straddles}",
        f"  Spot: opened {spot_opened:.4f} BTC, closed {spot_closed:.4f} BTC",
        f"  Puts: opened {put_opened:.4f} BTC, closed {put_closed:.4f} BTC",
        f"  <b>Total traded notional</b> "
        f"(spot+puts, round-trip): {spot_total + put_total:.4f} BTC",
    ]

    return "\n".join(lines)
