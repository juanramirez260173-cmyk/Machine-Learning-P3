"""Data loading and preprocessing utilities."""

import pandas as pd
from sklearn.model_selection import train_test_split


def load_data(filepath):
    """Load a dataset from a CSV file."""
    return pd.read_csv(filepath)


def split_data(df, target_column, test_size=0.2, random_state=42):
    """Split data into training and test sets."""
    X = df.drop(columns=[target_column])
    y = df[target_column]
    return train_test_split(X, y, test_size=test_size, random_state=random_state)
