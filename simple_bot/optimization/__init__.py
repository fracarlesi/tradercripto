"""
Optimization Module for Multi-Strategy Trading Bot

This module provides LLM-based auto-optimization using DeepSeek Reasoner.
Components:
- ConfigManager: Hot-reload configuration with DB versioning
- DataCollector: Hourly metrics collection
- TieredSummarizer: Context preparation with rolling windows
- DeepSeekClient: API integration with prompt engineering
- SafetyMonitor: Performance monitoring and auto-rollback
- OptimizationOrchestrator: Main coordination loop
"""

from .config_manager import HotReloadConfigManager
from .data_collector import HourlyMetricsCollector
from .summarizer import TieredSummarizer
from .deepseek_client import DeepSeekOptimizer, OptimizationResult
from .rollback import SafetyMonitor
from .optimizer import OptimizationOrchestrator

__all__ = [
    "HotReloadConfigManager",
    "HourlyMetricsCollector",
    "TieredSummarizer",
    "DeepSeekOptimizer",
    "OptimizationResult",
    "SafetyMonitor",
    "OptimizationOrchestrator",
]
