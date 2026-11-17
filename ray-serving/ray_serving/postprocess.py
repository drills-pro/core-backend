"""What a served model's raw ONNX outputs become, one function per kind of output.

This runs in the Ray Serve replica (the ray_serving package; core-api also embeds this file as text for legacy
deployments), so it imports numpy and the standard library only, and each function takes `(outputs, class_names, width, height, params)`: the session's output
arrays, the size of the image the caller sent and the task's serving params (app.core.task_types.ServingParams as a
dict). Boxes are answered in pixels of that image as x, y, width, height, the same convention the datasets store them in.
"""
import numpy as np


def classify(outputs, class_names, width, height, params):
    """Image classification: logits (1, classes) -> every class with its softmax probability, best first."""
    logits = outputs[0][0]
    probs = np.exp(logits - logits.max())
    probs = probs / probs.sum()
    ranked = sorted(zip(class_names, probs.tolist()), key=lambda x: -x[1])
    return [{"class": c, "probability": p} for c, p in ranked]


def detect(outputs, class_names, width, height, params):
    """Object detection (DETR family): logits (1, queries, classes) and pred_boxes (1, queries, 4) as normalised
    cx, cy, w, h -> the best (query, class) pairs above the threshold, each with a box in pixels. The head has one
    sigmoid per class, so a query may be reported for more than one class."""
    scores = 1 / (1 + np.exp(-outputs[0][0]))
    boxes = outputs[1][0]
    flat = scores.reshape(-1)
    found = []
    for k in np.argsort(-flat)[:params["max_results"]]:
        if flat[k] < params["score_threshold"]:
            break
        query, cls = divmod(int(k), len(class_names))
        cx, cy, w, h = (float(v) for v in boxes[query])
        found.append({
            "class": class_names[cls], "probability": float(flat[k]),
            "box": {"x": (cx - w / 2) * width, "y": (cy - h / 2) * height, "width": w * width, "height": h * height}})
    return found


def ranked_probabilities(outputs, class_names, width, height, params):
    """Tabular classification: the `probabilities` output (1, classes), already softmax-ed by XGBoost -> every class with its
    probability, best first (the label output is not needed for it)."""
    ranked = sorted(zip(class_names, outputs[1][0].tolist()), key=lambda x: -x[1])
    return [{"class": c, "probability": p} for c, p in ranked]
