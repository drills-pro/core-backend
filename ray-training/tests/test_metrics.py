import numpy as np

from ray_training.metrics import classification_metrics, detection_metrics


def test_top1_top5_and_confusion():
    logits = np.array([[3, 2, 1, 0, 0, 0, 0.0], [0, 1, 3, 2, 0, 0, 0], [-9, 1, 2, 3, 4, 5, 6.0]])
    m, confusion = classification_metrics(logits, np.array([0, 3, 0]), 7)
    assert m == {"top1": 1 / 3, "top5": 2 / 3}
    assert confusion[0][0] == 1 and confusion[3][2] == 1 and confusion[0][6] == 1


def test_top5_with_fewer_than_five_classes_is_top_all():
    m, _ = classification_metrics(np.array([[0.0, 1.0]]), np.array([0]), 2)
    assert m == {"top1": 0.0, "top5": 1.0}


def test_map_perfect_wrong_and_shifted():
    gt = [[(0, [0.1, 0.1, 0.5, 0.5])], [(1, [0.2, 0.2, 0.6, 0.6])]]
    perfect = [[(c, 0.9, b)] for [(c, b)] in gt]
    assert detection_metrics(perfect, gt, 2) == {"map50": 1.0, "map50_95": 1.0}
    assert detection_metrics([[], []], gt, 2) == {"map50": 0.0, "map50_95": 0.0}
    wrong_class = [[(1 - c, 0.9, b)] for [(c, b)] in gt]
    assert detection_metrics(wrong_class, gt, 2)["map50"] == 0.0
    shifted = [[(c, 0.9, [b[0] + 0.05, b[1], b[2] + 0.05, b[3]])] for [(c, b)] in gt]  # IoU ~0.78
    m = detection_metrics(shifted, gt, 2)
    assert m["map50"] == 1.0 and 0.0 < m["map50_95"] < 1.0
