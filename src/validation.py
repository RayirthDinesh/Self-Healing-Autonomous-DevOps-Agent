"""Validation helpers for transactions."""

REQUIRED_FIELDS = ("id", "amount", "currency")

def validate_amount(amount):
    """Return True if ``amount`` is strictly positive.

    The amount is cast to ``float`` first, so numeric strings such as
    ``"42.50"`` are accepted. Non-numeric input (e.g. ``None``) raises
    ``TypeError`` from the cast.
    """
    return float(amount) > 0

def validate_transaction(transaction):
    """Return True if ``transaction`` contains all required fields.

    Required fields: ``id``, ``amount``, ``currency``.
    """
    return all(field in transaction for field in REQUIRED_FIELDS)
