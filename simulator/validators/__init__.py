"""Consistency validation layer for the Sensgreen Sensor Simulator.

Public entry point: :func:`run_validation`.
"""

from .validation_report import (
    Finding,
    Severity,
    ValidationReport,
    run_validation,
)

__all__ = ["Finding", "Severity", "ValidationReport", "run_validation"]
