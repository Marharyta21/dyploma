import sys
import copy
import time
import torch
import pandas as pd
from sklearn.model_selection import train_test_split
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# python -m reviews.train_bert 5000 trains on a 5k stratified subset, no argument uses the whole train set
# a second argument label4 switches to the 4 rating groups, the default is binary sentiment
# a third argument sets the max token length, 256 by default, 512 is the model maximum
n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
target = sys.argv[2] if len(sys.argv) > 2 else "label"
max_len = int(sys.argv[3]) if len(sys.argv) > 3 else 256
suffix = "" if target == "label" else "_" + target
suffix += "" if max_len == 256 else f"_len{max_len}"
k = 2 if target == "label" else 4
device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
torch.manual_seed(42)

train = pd.read_csv("data/processed/train.csv")
val = pd.read_csv("data/processed/val.csv")
test = pd.read_csv("data/processed/test.csv")
if n < len(train):
    train, _ = train_test_split(train, train_size=n, stratify=train[target], random_state=42)

name = "distilbert-base-uncased"
tok = AutoTokenizer.from_pretrained(name)
model = AutoModelForSequenceClassification.from_pretrained(name, num_labels=k).to(device)
opt = torch.optim.AdamW(model.parameters(), lr=2e-5)


def batches(df, size):
    for i in range(0, len(df), size):
        part = df.iloc[i:i + size]
        enc = tok(list(part.text), truncation=True, max_length=max_len, padding=True, return_tensors="pt")
        yield enc.to(device), torch.tensor(part[target].values).to(device)


def predict(df):
    model.eval()
    probs, total = [], 0
    with torch.no_grad():
        for enc, y in batches(df, 64):
            out = model(**enc, labels=y)
            total += out.loss.item() * len(y)
            probs.append(torch.softmax(out.logits, dim=1).cpu())
    return torch.cat(probs).numpy(), total / len(df)


# validation loss rises after epoch 1-2, so the weights of the epoch with the lowest validation loss are kept
train_time, curve, best = 0, [], None
for epoch in range(4):
    t = time.time()
    model.train()
    part = train.sample(frac=1, random_state=42 + epoch)
    total = 0
    for step, (enc, y) in enumerate(batches(part, 16)):
        loss = model(**enc, labels=y).loss
        loss.backward()
        opt.step()
        opt.zero_grad()
        total += loss.item()
        if step % 50 == 0:
            print(epoch + 1, step, round(loss.item(), 4), flush=True)
    train_time += time.time() - t
    prob, val_loss = predict(val)
    val_acc = (prob.argmax(axis=1) == val[target]).mean()
    curve.append((epoch + 1, total / (step + 1), val_loss, val_acc))
    if best is None or val_loss < best[1]:
        best = (epoch + 1, val_loss, copy.deepcopy(model.state_dict()))
    print("epoch", epoch + 1, "train loss", round(curve[-1][1], 4), "val loss", round(val_loss, 4),
          "val acc", round(val_acc, 4), flush=True)
train_time = round(train_time, 2)
print("train time", train_time, "best epoch", best[0], flush=True)
model.load_state_dict(best[2])

curve = pd.DataFrame(curve, columns=["epoch", "train_loss", "val_loss", "val_acc"]).round(4)
curve["kept"] = curve.epoch == best[0]
curve.to_csv(f"tables/bert_curve_{n}{suffix}.csv", index=False)
model.save_pretrained(f"models/distilbert_{n}{suffix}")
tok.save_pretrained(f"models/distilbert_{n}{suffix}")

prob, _ = predict(test)
# the binary file keeps the old prob, pred columns, the 4-class file has one column per class
if k == 2:
    pred = pd.DataFrame({"prob": prob[:, 1]})
else:
    pred = pd.DataFrame(prob, columns=[f"prob_{i}" for i in range(k)])
pred["pred"] = prob.argmax(axis=1)
pred.to_csv(f"models/bert_test_pred_{n}{suffix}.csv", index=False)
print("test acc", round((pred.pred == test[target]).mean(), 4), flush=True)

times = pd.read_csv("tables/train_times.csv")
times = times[times.model != f"bert_{n}{suffix}"].reset_index(drop=True)
times.loc[len(times)] = [f"bert_{n}{suffix}", train_time]
times.to_csv("tables/train_times.csv", index=False)
