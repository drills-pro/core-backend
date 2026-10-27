import pytest
from transformers import (
    ConvNextConfig, ConvNextForImageClassification, RTDetrResNetConfig, RTDetrV2Config, RTDetrV2ForObjectDetection)


@pytest.fixture(scope="session")
def tiny_convnext(tmp_path_factory):
    path = tmp_path_factory.mktemp("convnext")
    ConvNextForImageClassification(ConvNextConfig(hidden_sizes=[8, 16], depths=[1, 1], num_stages=2, image_size=32)).save_pretrained(path)
    return path


@pytest.fixture(scope="session")
def tiny_rtdetr(tmp_path_factory):
    path = tmp_path_factory.mktemp("rtdetr")
    backbone = RTDetrResNetConfig(
        embedding_size=8, hidden_sizes=[8, 16, 32, 64], depths=[1, 1, 1, 1], out_features=["stage2", "stage3", "stage4"],
        layer_type="basic", downsample_in_bottleneck=False)
    config = RTDetrV2Config(
        backbone_config=backbone, encoder_hidden_dim=16, d_model=16, encoder_in_channels=[16, 32, 64], feat_strides=[8, 16, 32],
        num_queries=10, decoder_layers=2, encoder_layers=1, encoder_ffn_dim=32, decoder_ffn_dim=32,  # >= 2 decoder layers: weight tying
        encoder_attention_heads=2, decoder_attention_heads=2, decoder_in_channels=[16, 16, 16], num_labels=2,
        num_feature_levels=3, decoder_n_points=2, num_denoising=0, anchor_image_size=None)
    RTDetrV2ForObjectDetection(config).save_pretrained(path)
    return path
