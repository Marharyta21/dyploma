"""CNN and BiLSTM classifiers with word embeddings trained from scratch, the middle step between TF-IDF and DistilBERT."""
import sys
import copy
import time
from collections import Counter
import torch
import joblib
import torch.nn as nn
import pandas as pd
from reviews.nn_models import CNN, LSTM, encode

# python -m reviews.train_nn cnn or python -m reviews.train_nn lstm, a second argument label4 switches to the 4 rating groups
arch = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else "label"
suffix = "" if target == "label" else "_" + target
k = 2 if target == "label" else 4
device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
torch.manual_seed(42)

train = pd.read_csv("data/processed/train.csv")
val = pd.read_csv("data/processed/val.csv")
test = pd.read_csv("data/processed/test.csv")

# 30k most frequent words, ids 0 and 1 are kept for padding and unknown words
counts = Counter(w for t in train.text for w in t.split())
vocab = {w: i + 2 for i, (w, _) in enumerate(counts.most_common(30000))}

model = (CNN if arch == "cnn" else LSTM)(len(vocab) + 2, k).to(device)
opt = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.CrossEntropyLoss()
# 400 words covers 87 % of the reviews completely, the median review has 174 words
X_train, y_train = encode(train.text, vocab), torch.tensor(train[target].values)
X_val, y_val = encode(val.text, vocab), torch.tensor(val[target].values)
X_test, y_test = encode(test.text, vocab), torch.tensor(test[target].values)


def predict(X, y):
    model.eval()
    probs, total = [], 0
    with torch.no_grad():
        for i in range(0, len(X), 256):
            logits = model(X[i:i + 256].to(device))
            total += loss_fn(logits, y[i:i + 256].to(device)).item() * len(logits)
            probs.append(logits.softmax(dim=1).cpu())
    return torch.cat(probs).numpy(), total / len(X)


train_time, curve, best = 0, [], None
# 10 epochs because with 5 the CNN was still improving at the last epoch
for epoch in range(10):
    t = time.time()
    model.train()
    order = torch.randperm(len(X_train))
    total = 0
    for i in range(0, len(order), 64):
        idx = order[i:i + 64]
        loss = loss_fn(model(X_train[idx].to(device)), y_train[idx].to(device))
        loss.backward()
        opt.step()
        opt.zero_grad()
        total += loss.item() * len(idx)
    train_time += time.time() - t
    prob, val_loss = predict(X_val, y_val)
    val_acc = (prob.argmax(axis=1) == y_val.numpy()).mean()
    curve.append((epoch + 1, total / len(X_train), val_loss, val_acc))
    print(arch, curve[-1], flush=True)
    # same rule as for DistilBERT, the epoch with the lowest validation loss is kept
    if best is None or val_loss < best[1]:
        best = (epoch + 1, val_loss, copy.deepcopy(model.state_dict()))
model.load_state_dict(best[2])
train_time = round(train_time, 2)
print("train time", train_time, "best epoch", best[0], flush=True)

curve = pd.DataFrame(curve, columns=["epoch", "train_loss", "val_loss", "val_acc"]).round(4)
curve["kept"] = curve.epoch == best[0]
curve.to_csv(f"tables/{arch}_curve{suffix}.csv", index=False)

prob, _ = predict(X_test, y_test)
pred = pd.DataFrame({"prob": prob[:, 1]}) if k == 2 else pd.DataFrame(prob, columns=[f"prob_{i}" for i in range(k)])
pred["pred"] = prob.argmax(axis=1)
pred.to_csv(f"models/{arch}_test_pred{suffix}.csv", index=False)
print("test acc", round((pred.pred == test[target]).mean(), 4), flush=True)

torch.save(model.state_dict(), f"models/{arch}{suffix}.pt")
joblib.dump(vocab, "models/nn_vocab.joblib")

times = pd.read_csv("tables/train_times.csv")
times = times[times.model != arch + suffix].reset_index(drop=True)
times.loc[len(times)] = [arch + suffix, train_time]
times.to_csv("tables/train_times.csv", index=False)
