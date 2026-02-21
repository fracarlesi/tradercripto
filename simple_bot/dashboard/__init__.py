"""
HLQuantBot v3.0 Dashboard
=========================

Modern Flask dashboard for the HLQuantBot v3.0 microservices architecture.

Features:
- Real-time service health monitoring
- Opportunity rankings display
- Strategy decisions and signals
- Position management
- Performance analytics
- Learning/optimization history

Usage:
    cd simple_bot/dashboard
    python app.py
    # Visit http://localhost:5611
"""

from .app import app, run_dashboard

__all__ = ["app", "run_dashboard"]
