"""Classification top-1, top-5 and confusion matrix, shared by the classification bundles (image and text)."""
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
