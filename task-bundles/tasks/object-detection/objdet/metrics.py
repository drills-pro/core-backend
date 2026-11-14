"""Detection mAP50 and mAP50-95 (COCO style)."""
import numpy as np

MAP_IOU_THRESHOLDS = tuple(0.5 + 0.05 * i for i in range(10))  # COCO: 0.50..0.95
MAP_RECALL_POINTS = 101


def _iou(box: np.ndarray, others: np.ndarray) -> np.ndarray:
    """IoU of one xyxy box against many."""
    iw = np.clip(np.minimum(box[2], others[:, 2]) - np.maximum(box[0], others[:, 0]), 0, None)
    ih = np.clip(np.minimum(box[3], others[:, 3]) - np.maximum(box[1], others[:, 1]), 0, None)
    inter = iw * ih
    area = (box[2] - box[0]) * (box[3] - box[1]) + (others[:, 2] - others[:, 0]) * (others[:, 3] - others[:, 1]) - inter
    return inter / np.maximum(area, 1e-9)


def _ap(preds: list, gts: dict, threshold: float) -> float:
    """AP of one class at one IoU threshold. preds: [(image, score, xyxy)], gts: {image: array (n, 4)}."""
    n_gt = sum(len(g) for g in gts.values())
    used = {image: np.zeros(len(g), dtype=bool) for image, g in gts.items()}
    hits = []
    for image, _, box in sorted(preds, key=lambda p: -p[1]):
        hit = False
        if image in gts and len(gts[image]):
            ious = _iou(box, gts[image])
            ious[used[image]] = -1
            best = int(ious.argmax())
            if ious[best] >= threshold:
                used[image][best], hit = True, True
        hits.append(hit)
    tp = np.cumsum(hits)
    recall = tp / n_gt
    precision = tp / np.arange(1, len(hits) + 1)
    precision = np.maximum.accumulate(precision[::-1])[::-1]  # monotone envelope
    points = np.linspace(0, 1, MAP_RECALL_POINTS)
    idx = np.searchsorted(recall, points, side="left")
    return float(np.mean([precision[i] if i < len(precision) else 0.0 for i in idx]))


def detection_metrics(predictions: list[list], targets: list[list], num_classes: int) -> dict:
    """predictions[image] = [(class, score, xyxy)], targets[image] = [(class, xyxy)] in one coordinate system.
    Returns {map50, map50_95}: AP averaged over the classes that have ground truth."""
    per_class = {}
    for c in range(num_classes):
        gts = {i: np.array([b for k, b in t if k == c], dtype=float).reshape(-1, 4) for i, t in enumerate(targets)}
        if not sum(len(g) for g in gts.values()):
            continue
        preds = [(i, s, np.asarray(b, dtype=float)) for i, p in enumerate(predictions) for k, s, b in p if k == c]
        per_class[c] = [_ap(preds, gts, t) if preds else 0.0 for t in MAP_IOU_THRESHOLDS]
    if not per_class:
        return {"map50": 0.0, "map50_95": 0.0}
    table = np.array(list(per_class.values()))
    return {"map50": float(table[:, 0].mean()), "map50_95": float(table.mean())}
