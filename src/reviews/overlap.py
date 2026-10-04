"""Test accuracy with and without the reviews that also appear in the training data."""
import os
import joblib
import pandas as pd

train = pd.concat([pd.read_csv("data/processed/train.csv"), pd.read_csv("data/processed/val.csv")])
test = pd.read_csv("data/processed/test.csv")
seen = test.text.isin(set(train.text))
print(seen.sum(), "test reviews also appear in train or val")

X = joblib.load("models/tfidf.joblib").transform(test.text)
preds = {name: joblib.load(f"models/{name}.joblib").predict(X) for name in ["nb", "lr", "svm"]}
for r in ["5000", "20000", "20000_len512"]:
    if os.path.exists(f"models/bert_test_pred_{r}.csv"):
        preds[f"bert_{r}"] = pd.read_csv(f"models/bert_test_pred_{r}.csv").pred.values
for arch in ["cnn", "lstm"]:
    if os.path.exists(f"models/{arch}_test_pred.csv"):
        preds[arch] = pd.read_csv(f"models/{arch}_test_pred.csv").pred.values

rows = []
for name, pred in preds.items():
    right = pred == test.label.values
    rows.append((name, right.mean(), right[~seen].mean(), right[seen].mean(), seen.sum()))

cols = ["model", "acc_all", "acc_without_overlap", "acc_on_overlap", "n_overlap"]
res = pd.DataFrame(rows, columns=cols).round(4)
res.to_csv("tables/overlap.csv", index=False)
print(res)
