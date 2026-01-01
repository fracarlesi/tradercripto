"""
HLQuantBot v2.0 - Test Suite
==============================

Integration and unit tests for the HLQuantBot trading system.

Test Categories:
    - test_imports: Verify all modules can be imported
    - test_message_bus: Test pub/sub messaging
    - test_config: Test configuration loading
    - test_services: Test service lifecycle

Running Tests:
    # Run all tests
    pytest simple_bot/tests/ -v
    
    # Run specific test file
    pytest simple_bot/tests/test_integration.py -v
    
    # Run with coverage
    pytest simple_bot/tests/ --cov=simple_bot --cov-report=html

Author: Francesco Carlesi
"""

__version__ = "2.0.0"
