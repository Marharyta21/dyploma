"""Pairwise McNemar tests between the models on the test set."""
import os
import sys
from itertools import combinations
import joblib
import pandas as pd
from scipy.stats import binomtest

# python -m reviews.mcnemar label4 compares the 4-class models, no argument means binary sentiment
target = sys.argv[1] if len(sys.argv) > 1 else "label"
suffix = "" if target == "label" else "_" + target

test = pd.read_csv("data/processed/test.csv")
y = test[target].values
X = joblib.load("models/tfidf.joblib").transform(test.text)

preds = {name: joblib.load(f"models/{name}{suffix}.joblib").predict(X) for name in ["nb", "lr", "svm"]}
for r in ["5000", "20000", "20000_len512"]:
    if os.path.exists(f"models/bert_test_pred_{r}{suffix}.csv"):
        preds[f"bert_{r}"] = pd.read_csv(f"models/bert_test_pred_{r}{suffix}.csv").pred.values
for arch in ["cnn", "lstm"]:
    if os.path.exists(f"models/{arch}_test_pred{suffix}.csv"):
        preds[arch] = pd.read_csv(f"models/{arch}_test_pred{suffix}.csv").pred.values

# exact version of the test: only the reviews where exactly one of the two models is right count
rows = []
for a, b in combinations(preds, 2):
    only_a = ((preds[a] == y) & (preds[b] != y)).sum()
    only_b = ((preds[a] != y) & (preds[b] == y)).sum()
    p = binomtest(int(only_a), int(only_a + only_b), 0.5).pvalue
    rows.append((a, b, (preds[a] == y).mean(), (preds[b] == y).mean(), only_a, only_b, p, p < 0.05))

cols = ["model_a", "model_b", "acc_a", "acc_b", "only_a_right", "only_b_right", "p_value", "significant"]
res = pd.DataFrame(rows, columns=cols).round(4)
res.to_csv(f"tables/mcnemar{suffix}.csv", index=False)
print(res.to_string())
