import pytest
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import BertConfig, BertForSequenceClassification, PreTrainedTokenizerFast

from tests.support import TEXT_VOCAB


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
