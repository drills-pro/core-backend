import numpy as np

from ray_core.metrics import classification_metrics


def test_top1_top5_and_confusion():
    logits = np.array([[3, 2, 1, 0, 0, 0, 0.0], [0, 1, 3, 2, 0, 0, 0], [-9, 1, 2, 3, 4, 5, 6.0]])
    m, confusion = classification_metrics(logits, np.array([0, 3, 0]), 7)
    assert m == {"top1": 1 / 3, "top5": 2 / 3}
    assert confusion[0][0] == 1 and confusion[3][2] == 1 and confusion[0][6] == 1


def test_top5_with_fewer_than_five_classes_is_top_all():
    m, _ = classification_metrics(np.array([[0.0, 1.0]]), np.array([0]), 2)
    assert m == {"top1": 0.0, "top5": 1.0}
