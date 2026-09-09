"""Point-in-time evaluation helpers for the stock analyzers.

This package is deliberately separate from the live analyzer.  It imports the
production rules and replays them on truncated history, but it does not change
the production score, grade, or action logic.
"""

from .point_in_time import (
    ABLATION_FAMILIES,
    BacktestConfig,
    EvaluationBundle,
    evaluate_point_in_time,
    write_evaluation_bundle,
)

__all__ = [
    "ABLATION_FAMILIES",
    "BacktestConfig",
    "EvaluationBundle",
    "evaluate_point_in_time",
    "write_evaluation_bundle",
]
