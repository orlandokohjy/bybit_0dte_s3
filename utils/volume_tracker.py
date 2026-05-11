"""Monthly volume tracking for option contracts and BTC notional.

Multi-session: each call to record_trade() tags the row with the firing
session's qty_per_leg so monthly aggregations can be done at any
granularity.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

import structlog

import config

log = structlog.get_logger(__name__)


def _current_month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def record_trade(
    num_straddles: int, qty_per_leg: float | None = None,
    session_name: str = "",
) -> None:
    """
    Append volume for one session's trades.

    Per straddle (round-trip):
      option_contracts    = 2 × NUM_PUTS                (buy + sell)
      option_btc_notional = 2 × NUM_PUTS × qty_per_leg  (buy + sell)
      spot_btc_volume     = 2 × qty_per_leg             (buy + sell)
    """
    qty = qty_per_leg if qty_per_leg is not None else config.QTY_PER_LEG

    contracts_per = 2 * config.NUM_PUTS
    option_btc_per = 2 * qty * config.NUM_PUTS
    spot_btc_per = 2 * qty

    row = {
        "month": _current_month_key(),
        "session": session_name,
        "num_straddles": num_straddles,
        "option_contracts": contracts_per * num_straddles,
        "option_btc_notional": option_btc_per * num_straddles,
        "spot_btc_volume": spot_btc_per * num_straddles,
        "qty_per_leg": qty,
    }

    os.makedirs(os.path.dirname(config.VOLUME_FILE), exist_ok=True)
    file_exists = os.path.exists(config.VOLUME_FILE)

    with open(config.VOLUME_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    log.info("volume_recorded", **row)
