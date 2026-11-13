import pytest
from transformers import ConvNextConfig, ConvNextForImageClassification


@pytest.fixture(scope="session")
def tiny_convnext(tmp_path_factory):
    path = tmp_path_factory.mktemp("convnext")
    ConvNextForImageClassification(ConvNextConfig(hidden_sizes=[8, 16], depths=[1, 1], num_stages=2, image_size=32)).save_pretrained(path)
    return path
