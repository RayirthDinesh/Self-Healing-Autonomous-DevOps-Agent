"""
Aggregation functions over a list of transaction amounts.
"""

def _ensure_not_empty(transactions):
    if not transactions:
        raise ValueError("transactions list is empty")


def total_value(transactions):
    """Return the sum of all transaction amounts."""
    _ensure_not_empty(transactions)
    return sum(transactions)


def average_value(transactions):
    """Return the mean of all transaction amounts."""
    _ensure_not_empty(transactions)
    return sum(transactions) / len(transactions)


def max_value(transactions):
    """Return the largest transaction amount."""
    _ensure_not_empty(transactions)
    return max(transactions)


def transaction_count(transactions):
    """Return the number of transactions."""
    return len(transactions)
