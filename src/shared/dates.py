"""Shared date math used across metadata sources and the chart engine."""

from datetime import date


def months_since(release_date: date) -> int:
    """Whole months elapsed since `release_date` (minimum 1)."""
    today = date.today()
    months = (today.year - release_date.year) * 12 + (today.month - release_date.month)
    if today.day < release_date.day:
        months -= 1
    return max(1, months)
