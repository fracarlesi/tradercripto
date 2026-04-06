"""
YAML → runtime config roundtrip tests.

Verifies that values declared in trading.yaml actually reach the runtime
BotConfig (BotExecutionConfig / BotStopsConfig) consumed by
ExecutionEngineService.

Phase 4 cleanup: previously this test maintained hand-rolled mirrors of
the nested _ExecConfig / _StopsConfig adapter classes from main.py. Those
adapters have been replaced by Pydantic models in
crypto_bot/config/loader.py (BotConfig.from_conservative), so we now
import the real classes directly.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from crypto_bot.config.loader import BotConfig, BotExecutionConfig, BotStopsConfig
from crypto_bot.main import ConservativeConfig


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_YAML = REPO_ROOT / "crypto_bot" / "config" / "trading.yaml"


# -----------------------------------------------------------------------------
# YAML execution: keys NOT propagated to BotExecutionConfig
# -----------------------------------------------------------------------------
# Empty after phase 4: max_retries / retry_delay_seconds are now wired
# through to BotExecutionConfig.retry_attempts / retry_delay_seconds.
EXECUTION_YAML_ORPHANS: set[str] = set()

# YAML stops top-level keys NOT on BotStopsConfig (handled elsewhere or unused)
STOPS_YAML_ORPHANS = {
    "stop_loss_pct",   # routed to BotRiskConfig
    "take_profit_pct", # routed to BotRiskConfig
    "r_based_exits",   # nested → flattened into r_*/bp_*/trailing_* fields
    "violation_exit",  # nested → flattened into violation_* fields on cfg, not on BotStopsConfig
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _load_prod_config() -> BotConfig:
    assert TRADING_YAML.exists(), f"trading.yaml not found at {TRADING_YAML}"
    return BotConfig.from_yaml(TRADING_YAML)


def _load_yaml_section(section: str) -> dict:
    with open(TRADING_YAML, "r") as f:
        data = yaml.safe_load(f)
    return data.get(section, {})


# -----------------------------------------------------------------------------
# Field-name remapping: yaml execution.<key> → BotExecutionConfig.<attr>
# -----------------------------------------------------------------------------
# YAML uses execution-engine-friendly names for the retry block; the
# Pydantic model uses retry_attempts (not max_retries) to match the
# rest of the codebase. Document the mapping in one place.
EXECUTION_FIELD_MAP: dict[str, str] = {
    "max_retries": "retry_attempts",
}


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
def test_execution_fields_reach_exec_config():
    """Every YAML execution field (minus orphans) must be present on
    BotExecutionConfig with the YAML value (not a default)."""
    exec_yaml = _load_yaml_section("execution")
    bot_cfg = _load_prod_config()
    exec_cfg: BotExecutionConfig = bot_cfg.services.execution_engine

    for key, yaml_value in exec_yaml.items():
        if key in EXECUTION_YAML_ORPHANS:
            continue
        attr: str = EXECUTION_FIELD_MAP.get(key) or key
        assert hasattr(exec_cfg, attr), (
            f"YAML execution.{key} has no destination on BotExecutionConfig "
            f"(expected attr {attr!r})."
        )
        actual = getattr(exec_cfg, attr)
        assert actual == yaml_value, (
            f"execution.{key}: YAML={yaml_value!r} but BotExecutionConfig.{attr}={actual!r}"
        )


def test_stops_fields_reach_stops_config():
    """Every YAML stops top-level field (minus orphans) must be present on
    BotStopsConfig."""
    stops_yaml = _load_yaml_section("stops")
    bot_cfg = _load_prod_config()
    stops_cfg: BotStopsConfig = bot_cfg.stops

    for key, yaml_value in stops_yaml.items():
        if key in STOPS_YAML_ORPHANS:
            continue
        assert hasattr(stops_cfg, key), (
            f"YAML stops.{key} has no destination on BotStopsConfig."
        )
        actual = getattr(stops_cfg, key)
        assert actual == yaml_value, (
            f"stops.{key}: YAML={yaml_value!r} but BotStopsConfig.{key}={actual!r}"
        )


def test_max_spread_pct_honored():
    """Regression test: changing max_spread_pct in YAML must reach
    BotExecutionConfig."""
    with open(TRADING_YAML, "r") as f:
        data = yaml.safe_load(f)
    data.setdefault("execution", {})["max_spread_pct"] = 0.99

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as tmp:
        yaml.safe_dump(data, tmp)
        tmp_path = tmp.name

    try:
        bot_cfg = BotConfig.from_yaml(tmp_path)
        assert bot_cfg.services.execution_engine.max_spread_pct == 0.99, (
            f"max_spread_pct not propagated: got "
            f"{bot_cfg.services.execution_engine.max_spread_pct}"
        )
    finally:
        os.unlink(tmp_path)


def test_retry_fields_honored():
    """Phase 4 regression: max_retries / retry_delay_seconds in YAML must
    reach BotExecutionConfig.retry_attempts / retry_delay_seconds (and
    must be honored by the execution engine retry loop, which reads them
    from self._exec_config)."""
    with open(TRADING_YAML, "r") as f:
        data = yaml.safe_load(f)
    data.setdefault("execution", {})["max_retries"] = 7
    data["execution"]["retry_delay_seconds"] = 4

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as tmp:
        yaml.safe_dump(data, tmp)
        tmp_path = tmp.name

    try:
        bot_cfg = BotConfig.from_yaml(tmp_path)
        assert bot_cfg.services.execution_engine.retry_attempts == 7
        assert bot_cfg.services.execution_engine.retry_delay_seconds == 4
    finally:
        os.unlink(tmp_path)


def test_no_orphaned_yaml_fields():
    """Whitelist enforcement: every YAML execution key must either map to a
    BotExecutionConfig attr (directly or via EXECUTION_FIELD_MAP) or be in
    EXECUTION_YAML_ORPHANS. Fail loudly if a new YAML field is added without
    a propagation decision."""
    exec_yaml = _load_yaml_section("execution")
    bot_cfg = _load_prod_config()
    exec_cfg = bot_cfg.services.execution_engine

    unaccounted = []
    for key in exec_yaml.keys():
        if key in EXECUTION_YAML_ORPHANS:
            continue
        attr: str = EXECUTION_FIELD_MAP.get(key) or key
        if not hasattr(exec_cfg, attr):
            unaccounted.append(key)

    assert not unaccounted, (
        f"YAML execution keys with no BotExecutionConfig destination: "
        f"{unaccounted}. Either propagate them in BotConfig.from_conservative "
        f"or add them to EXECUTION_YAML_ORPHANS / EXECUTION_FIELD_MAP."
    )


def test_conservative_config_still_loads():
    """Sanity: the underlying ConservativeConfig.from_yaml still works."""
    cfg = ConservativeConfig.from_yaml(str(TRADING_YAML))
    assert cfg is not None
    assert cfg.entry_mode in ("taker", "maker")
