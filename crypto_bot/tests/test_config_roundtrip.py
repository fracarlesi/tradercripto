"""
YAML → runtime config roundtrip tests.

Verifies that values declared in trading.yaml actually reach the runtime
config objects (_ExecConfig, _StopsConfig) used by ExecutionEngineService.

Background: crypto_bot has two parallel config schemas — a Pydantic Config
in config/loader.py (dead code) and a ConservativeConfig dataclass in main.py
(prod path). The prod path loads YAML → ConservativeConfig → _ConfigAdapter →
_ExecConfig/_StopsConfig via manual field mapping. Any field added to YAML
without a corresponding propagation step is silently ignored.

These tests catch drift between trading.yaml and the runtime adapter.

NOTE: _ExecConfig and _StopsConfig are nested inside ConservativeBot._init_services
in main.py. To test them in isolation we replicate the same field assignments
here. If you change main.py:_init_services, mirror the change here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from crypto_bot.main import ConservativeConfig


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_YAML = REPO_ROOT / "crypto_bot" / "config" / "trading.yaml"


# -----------------------------------------------------------------------------
# Mirror of nested classes from main.py:_init_services
# Keep in sync with ConservativeBot._init_services
# -----------------------------------------------------------------------------
class _ExecConfigMirror:
    def __init__(self, cfg: ConservativeConfig):
        self.order_type = "limit" if cfg.prefer_limit else "market"
        self.max_slippage_pct = cfg.max_slippage_pct
        self.max_spread_pct = cfg.max_spread_pct
        self.limit_timeout_seconds = cfg.limit_timeout_seconds
        self.retry_attempts = 3
        self.retry_delay_seconds = 5
        self.position_sync_interval = 30
        self.fill_sync_interval = 10
        self.entry_mode = cfg.entry_mode
        self.maker_reprice_interval_seconds = cfg.maker_reprice_interval_seconds
        self.maker_max_reprices = cfg.maker_max_reprices


class _StopsConfigMirror:
    def __init__(self, cfg: ConservativeConfig):
        self.initial_atr_mult = cfg.initial_atr_mult
        self.trailing_atr_mult = cfg.trailing_atr_mult
        self.minimal_roi = cfg.minimal_roi
        self.max_hold_hours = cfg.max_hold_hours
        self.r_based_exits_enabled = cfg.r_based_exits_enabled
        self.bp_activation_r = cfg.bp_activation_r
        self.bp_offset_pct = cfg.bp_offset_pct
        self.strength_exit_r = cfg.strength_exit_r
        self.trailing_r_enabled = cfg.trailing_r_enabled
        self.trailing_start_r = cfg.trailing_start_r
        self.trailing_step_r = cfg.trailing_step_r
        self.trailing_lock_r = cfg.trailing_lock_r


# -----------------------------------------------------------------------------
# YAML execution: keys NOT propagated to _ExecConfig (intentional or hardcoded)
# -----------------------------------------------------------------------------
# These YAML keys exist under `execution:` but are intentionally not exposed on
# _ExecConfig because the runtime hardcodes them or they are unused by the
# execution engine. If you propagate them, remove from this whitelist.
EXECUTION_YAML_ORPHANS = {
    "max_retries",         # _ExecConfig.retry_attempts is hardcoded to 3
    "retry_delay_seconds", # _ExecConfig.retry_delay_seconds is hardcoded to 5
}

# YAML stops top-level keys NOT on _StopsConfig (handled elsewhere or unused)
STOPS_YAML_ORPHANS = {
    "stop_loss_pct",        # routed to _RiskConfig
    "take_profit_pct",      # routed to _RiskConfig
    "r_based_exits",        # nested → flattened into r_*/bp_*/trailing_* fields
    "violation_exit",       # nested → flattened into violation_* fields on cfg, not on _StopsConfig
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _load_prod_config() -> ConservativeConfig:
    assert TRADING_YAML.exists(), f"trading.yaml not found at {TRADING_YAML}"
    return ConservativeConfig.from_yaml(str(TRADING_YAML))


def _load_yaml_section(section: str) -> dict:
    with open(TRADING_YAML, "r") as f:
        data = yaml.safe_load(f)
    return data.get(section, {})


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
def test_execution_fields_reach_exec_config():
    """Every YAML execution field (minus orphans) must be present on _ExecConfig
    with the YAML value (not a default)."""
    exec_yaml = _load_yaml_section("execution")
    cfg = _load_prod_config()
    exec_cfg = _ExecConfigMirror(cfg)

    for key, yaml_value in exec_yaml.items():
        if key in EXECUTION_YAML_ORPHANS:
            continue
        assert hasattr(exec_cfg, key), (
            f"YAML execution.{key} has no destination on _ExecConfig. "
            f"Add it to _ExecConfig or to EXECUTION_YAML_ORPHANS."
        )
        actual = getattr(exec_cfg, key)
        assert actual == yaml_value, (
            f"execution.{key}: YAML={yaml_value!r} but _ExecConfig={actual!r}"
        )


def test_stops_fields_reach_stops_config():
    """Every YAML stops top-level field (minus orphans) must be present on
    _StopsConfig."""
    stops_yaml = _load_yaml_section("stops")
    cfg = _load_prod_config()
    stops_cfg = _StopsConfigMirror(cfg)

    for key, yaml_value in stops_yaml.items():
        if key in STOPS_YAML_ORPHANS:
            continue
        assert hasattr(stops_cfg, key), (
            f"YAML stops.{key} has no destination on _StopsConfig. "
            f"Add it to _StopsConfig or to STOPS_YAML_ORPHANS."
        )
        actual = getattr(stops_cfg, key)
        assert actual == yaml_value, (
            f"stops.{key}: YAML={yaml_value!r} but _StopsConfig={actual!r}"
        )


def test_max_spread_pct_honored():
    """Regression test: changing max_spread_pct in YAML must reach _ExecConfig.
    Previously the value was silently ignored because of a defensive
    getattr() fallback that happened to match the YAML value."""
    with open(TRADING_YAML, "r") as f:
        data = yaml.safe_load(f)
    data.setdefault("execution", {})["max_spread_pct"] = 0.99

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as tmp:
        yaml.safe_dump(data, tmp)
        tmp_path = tmp.name

    try:
        cfg = ConservativeConfig.from_yaml(tmp_path)
        exec_cfg = _ExecConfigMirror(cfg)
        assert exec_cfg.max_spread_pct == 0.99, (
            f"max_spread_pct not propagated: got {exec_cfg.max_spread_pct}"
        )
    finally:
        os.unlink(tmp_path)


def test_no_orphaned_yaml_fields():
    """Whitelist enforcement: every YAML execution key must either be on
    _ExecConfig or in EXECUTION_YAML_ORPHANS. Fail loudly if a new YAML field
    is added without a propagation decision."""
    exec_yaml = _load_yaml_section("execution")
    cfg = _load_prod_config()
    exec_cfg = _ExecConfigMirror(cfg)

    unaccounted = []
    for key in exec_yaml.keys():
        if key in EXECUTION_YAML_ORPHANS:
            continue
        if not hasattr(exec_cfg, key):
            unaccounted.append(key)

    assert not unaccounted, (
        f"YAML execution keys with no _ExecConfig destination: {unaccounted}. "
        f"Either propagate them in main.py:_init_services._ExecConfig or "
        f"add them to EXECUTION_YAML_ORPHANS with a justification comment."
    )
