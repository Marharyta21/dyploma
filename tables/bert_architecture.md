# DistilBERT architecture used in the thesis

The model is `distilbert-base-uncased` from Hugging Face, fine-tuned for binary sentiment
classification. DistilBERT is a distilled version of BERT-base: it keeps the same hidden size and
tokenizer but has half the transformer layers, which makes it about 40% smaller and 60% faster
while keeping about 97% of BERT's performance on GLUE (Sanh et al., 2019).

| Component | Value |
|---|---|
| Base model | distilbert-base-uncased |
| Transformer layers | 6 |
| Hidden size | 768 |
| Attention heads | 12 |
| Feed-forward size | 3072 |
| Vocabulary | 30 522 WordPiece tokens, uncased |
| Parameters | about 66 million |
| Max input length used | 256 tokens |
| Pooling | hidden state of the first token ([CLS]) |
| Classification head | pre-classifier linear 768→768 with ReLU, dropout 0.2, linear 768→2 |
| Output | softmax over two classes (negative, positive) |

The classification head is the default `DistilBertForSequenceClassification` head. Only the last
linear layer is initialised randomly; everything else starts from the pretrained weights and all
weights are updated during fine-tuning.

Reference: Sanh, V., Debut, L., Chaumond, J., Wolf, T. (2019). DistilBERT, a distilled version of
BERT: smaller, faster, cheaper and lighter. arXiv:1910.01108.
