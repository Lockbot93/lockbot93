"""
LockBot Scanner Results

This module defines the shared data structure used to pass
scanner results back to the LockBot controller.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict


@dataclass
class ScannerResults:
    scan_time: datetime | None = None

    market_open: bool = False

    account_equity: float = 0.0

    buying_power: float = 0.0

    daily_pnl: float = 0.0

    daily_pnl_percent: float = 0.0

    daily_loss_limit_hit: bool = False

    signals: Dict[str, str] = field(default_factory=dict)

    confidence_scores: Dict[str, int] = field(default_factory=dict)

    approval_status: Dict[str, bool] = field(default_factory=dict)