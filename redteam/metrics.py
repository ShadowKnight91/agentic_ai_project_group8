"""Small statistics helpers (standard library only)."""

from math import comb, sqrt
from statistics import median


def wilson(k, n, z=1.96):
    """Proportion with a 95% Wilson score interval: (p, low, high)."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def fmt_rate(k, n):
    if n == 0:
        return "n/a"
    p, lo, hi = wilson(k, n)
    return f"{k}/{n} = {p:.0%} [{lo:.0%}-{hi:.0%}]"


def fisher_exact(a, b, c, d):
    """Two-sided Fisher's exact test p-value for the 2x2 table [[a, b], [c, d]]."""
    row1, row2, col1 = a + b, c + d, a + c
    n = row1 + row2
    if n == 0:
        return 1.0

    def prob(x):
        return comb(row1, x) * comb(row2, col1 - x) / comb(n, col1)

    observed = prob(a)
    lo, hi = max(0, col1 - row2), min(col1, row1)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + 1e-7)))


def cohen_kappa(labels_a, labels_b):
    """Cohen's kappa for two annotators' labels of the same items."""
    n = len(labels_a)
    if n == 0:
        return float("nan")
    categories = set(labels_a) | set(labels_b)
    observed = sum(x == y for x, y in zip(labels_a, labels_b)) / n
    expected = sum((labels_a.count(c) / n) * (labels_b.count(c) / n) for c in categories)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def safe_median(values):
    return median(values) if values else None
