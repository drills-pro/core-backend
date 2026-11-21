"""Classification top-1, top-5 and confusion matrix, shared by the classification bundles (image and text); regression rmse, mae and r2."""
import numpy as np

from ray_core.constants import CONFUSION_TOP_K


def classification_metrics(logits: np.ndarray, targets: np.ndarray, num_classes: int) -> tuple[dict, list[list[int]]]:
    """Returns ({top1, top5}, confusion[true][predicted]). top5 is top-min(5, classes)."""
    k = min(CONFUSION_TOP_K, num_classes)
    top = np.argsort(-logits, axis=1)[:, :k]
    confusion = np.zeros((num_classes, num_classes), dtype=int)
    np.add.at(confusion, (targets, top[:, 0]), 1)
    return ({"top1": float((top[:, 0] == targets).mean()), "top5": float((top == targets[:, None]).any(axis=1).mean())},
            confusion.tolist())


def regression_metrics(predicted: np.ndarray, targets: np.ndarray) -> dict:
    """{rmse, mae, r2}. r2 is 0 when the targets are all one value (nothing to explain), never NaN."""
    error = predicted - targets
    spread = float(((targets - targets.mean()) ** 2).sum())
    return {"rmse": float(np.sqrt((error**2).mean())), "mae": float(np.abs(error).mean()),
            "r2": 1 - float((error**2).sum()) / spread if spread else 0.0}
