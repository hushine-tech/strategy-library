"""
Shared test utilities for the strategy-library test suite.
"""
import math


def isclose(a: float, b: float, *, rel: float = 1e-12, abs_: float = 1e-9) -> bool:
    """Return True if a and b are close within the given tolerances."""
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)
