import pytest
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import (
    BertConfig, BertForSequenceClassification, ConvNextConfig, ConvNextForImageClassification, PreTrainedTokenizerFast,
    RTDetrResNetConfig, RTDetrV2Config, RTDetrV2ForObjectDetection)

from tests.support import TEXT_VOCAB


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


@pytest.fixture(scope="session")
def tiny_bert(tmp_path_factory):
    """A 1-layer BERT and a word-level tokenizer over TEXT_VOCAB, saved the way a Hub checkpoint is."""
    path = tmp_path_factory.mktemp("bert")
    vocab = {w: i for i, w in enumerate(["[PAD]", "[UNK]", *TEXT_VOCAB])}
    tk = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    PreTrainedTokenizerFast(tokenizer_object=tk, unk_token="[UNK]", pad_token="[PAD]").save_pretrained(path)
    BertForSequenceClassification(BertConfig(
        vocab_size=len(vocab), hidden_size=16, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32,
        max_position_embeddings=64, pad_token_id=0)).save_pretrained(path)
    return path
