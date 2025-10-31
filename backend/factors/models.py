"""Factor models for trading factor analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd


@dataclass
class Factor:
    """Trading factor definition.

    Attributes:
        id: Unique factor identifier
        name: Human-readable factor name
        description: Factor description
        columns: List of column definitions for display
        compute: Function to compute factor DataFrame from history data
    """

    id: str
    name: str
    description: str
    columns: list[dict[str, Any]]
    compute: Callable[[dict[str, pd.DataFrame], pd.DataFrame | None], pd.DataFrame]
