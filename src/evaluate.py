"""Evaluation and metrics utilities."""

from sklearn.metrics import accuracy_score, classification_report, confusion_matrix


def print_metrics(y_true, y_pred):
    """Print classification metrics."""
    print(f"Accuracy: {accuracy_score(y_true, y_pred):.4f}")
    print("\nClassification Report:")
    print(classification_report(y_true, y_pred))
    print("Confusion Matrix:")
    print(confusion_matrix(y_true, y_pred))
