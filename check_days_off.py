"""Offline coverage regression gate using a disposable synthetic database.

Replaces the old live-mutating check with hardcoded September dates. Proposed
coverage now deliberately refuses calendar creation; confirmation is required.
Run: python check_days_off.py (no URL argument or Google calls).
"""
import sys
import unittest
from test_household import WorkflowTests

if len(sys.argv) > 1:
    raise SystemExit('This gate now runs offline. Use: python check_days_off.py')
suite = unittest.TestSuite(WorkflowTests(name) for name in (
    'test_coverage_confirmation_and_calendar_gate',
    'test_closure_suppresses_routines',
))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
