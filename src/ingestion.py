"""
CSV ingestion for the transaction pipeline.
"""

import os

import pandas as pd


def parse_transactions(filepath):
    """Read a transactions CSV and return the amounts as a list of floats.

    Args:
        filepath: Path to a CSV file with an ``amount`` column.

    Returns:
        list[float]: One amount per row, in file order.

    Raises:
        FileNotFoundError: If ``filepath`` does not exist.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Transactions file not found: {filepath}")

    df = pd.read_csv(filepath)
    amounts = [float(amount) for amount in df["amount"]]
    return amounts
