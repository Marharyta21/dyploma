import torch
import torch.nn as nn


class CNN(nn.Module):
    # filter sizes 3, 4, 5 with 100 filters each, as in Kim (2014)
    def __init__(self, n_words, k):
        super().__init__()
        self.emb = nn.Embedding(n_words, 128, padding_idx=0)
        self.convs = nn.ModuleList([nn.Conv1d(128, 100, s) for s in (3, 4, 5)])
        self.out = nn.Sequential(nn.Dropout(0.5), nn.Linear(300, k))

    def forward(self, x):
        e = self.emb(x).transpose(1, 2)
        return self.out(torch.cat([c(e).relu().max(dim=2).values for c in self.convs], dim=1))


class LSTM(nn.Module):
    def __init__(self, n_words, k):
        super().__init__()
        self.emb = nn.Embedding(n_words, 128, padding_idx=0)
        self.lstm = nn.LSTM(128, 128, batch_first=True, bidirectional=True)
        self.out = nn.Sequential(nn.Dropout(0.5), nn.Linear(256, k))

    def forward(self, x):
        h, _ = self.lstm(self.emb(x))
        # max over the real words only, padding positions are pushed to -inf
        h = h.masked_fill((x == 0).unsqueeze(2), -1e9)
        return self.out(h.max(dim=1).values)


def encode(texts, vocab, max_len=400):
    # id 0 is padding and 1 is an unknown word
    ids = torch.zeros(len(texts), max_len, dtype=torch.long)
    for i, t in enumerate(texts):
        words = [vocab.get(w, 1) for w in t.split()[:max_len]]
        ids[i, :len(words)] = torch.tensor(words)
    return ids
