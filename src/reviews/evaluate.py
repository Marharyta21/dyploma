import os
import sys
import joblib
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.metrics import (accuracy_score, precision_score, recall_score, f1_score,
                             roc_auc_score, roc_curve, ConfusionMatrixDisplay)

# python -m reviews.evaluate label4 evaluates the 4 rating groups, no argument means binary sentiment
target = sys.argv[1] if len(sys.argv) > 1 else "label"
suffix = "" if target == "label" else "_" + target
binary = target == "label"
avg = "binary" if binary else "macro"
classes = ["neg", "pos"] if binary else ["1-2", "3-4", "7-8", "9-10"]

test = pd.read_csv("data/processed/test.csv")
y = test[target]
vec = joblib.load("models/tfidf.joblib")
X = vec.transform(test.text)
times = pd.read_csv("tables/train_times.csv").set_index("model").train_time_s

scores = {}
for name in ["nb", "lr", "svm"]:
    clf = joblib.load(f"models/{name}{suffix}.joblib")
    s = clf.decision_function(X) if name == "svm" else clf.predict_proba(X)
    scores[name] = s[:, 1] if binary and name != "svm" else s
# bert runs: 5k reviews, all 20k reviews, and 20k with 512 tokens instead of 256, skip the ones not run yet
runs = ["5000", "20000", "20000_len512"]
for r in runs:
    if os.path.exists(f"models/bert_test_pred_{r}{suffix}.csv"):
        p = pd.read_csv(f"models/bert_test_pred_{r}{suffix}.csv")
        scores[f"bert_{r}"] = p.prob.values if binary else p.filter(like="prob_").values

rows = []
plt.figure()
for name, s in scores.items():
    if binary:
        pred = (s > 0).astype(int) if name == "svm" else (s > 0.5).astype(int)
        auc = roc_auc_score(y, s)
        fpr, tpr, _ = roc_curve(y, s)
        plt.plot(fpr, tpr, label=f"{name} (AUC={auc:.3f})")
    else:
        pred = s.argmax(axis=1)
        # LinearSVC gives no probabilities, so the one-vs-rest AUC is only computed for the other models
        auc = float("nan") if name == "svm" else roc_auc_score(y, s, multi_class="ovr")
    rows.append((name, accuracy_score(y, pred), precision_score(y, pred, average=avg),
                 recall_score(y, pred, average=avg), f1_score(y, pred, average=avg), auc, times[name + suffix]))

    ConfusionMatrixDisplay.from_predictions(y, pred, display_labels=classes)
    plt.title(f"Confusion matrix, {name}")
    plt.savefig(f"figures/cm_{name}{suffix}.png")
    plt.close()

    wrong = test[pred != y].copy()
    wrong["pred"] = pred[pred != y]
    wrong["text"] = wrong.text.str[:300]
    wrong = wrong.sample(10, random_state=42)[[target, "pred", "text"]]
    wrong.to_csv(f"tables/errors_{name}{suffix}.csv", index=False)

if binary:
    plt.figure(1)
    plt.plot([0, 1], [0, 1], "k--")
    plt.gca().set(xlabel="false positive rate", ylabel="true positive rate", title="ROC curves on the test set")
    plt.legend()
    plt.savefig("figures/roc_all.png")
plt.close("all")

plt.figure()
for r in runs:
    if os.path.exists(f"tables/bert_curve_{r}{suffix}.csv"):
        curve = pd.read_csv(f"tables/bert_curve_{r}{suffix}.csv")
        plt.plot(curve.epoch, curve.train_loss, "o-", label=f"train loss, bert_{r}")
        plt.plot(curve.epoch, curve.val_loss, "o--", label=f"val loss, bert_{r}")
plt.xticks([1, 2, 3, 4])
plt.gca().set(xlabel="epoch", ylabel="mean cross-entropy loss", title="DistilBERT loss per epoch")
plt.legend()
plt.savefig(f"figures/bert_curve{suffix}.png")
plt.close()

cols = ["model", "accuracy", "precision", "recall", "f1", "roc_auc", "train_time_s"]
results = pd.DataFrame(rows, columns=cols).round(4)
results.to_csv(f"tables/results{suffix}.csv", index=False)
print(results)

lr = joblib.load(f"models/lr{suffix}.joblib")
coefs = [pd.Series(c, index=vec.get_feature_names_out()) for c in lr.coef_]
if binary:
    parts = [(coefs[0].nlargest(20), "pos"), (coefs[0].nsmallest(20), "neg")]
else:
    parts = [(c.nlargest(10), cls) for c, cls in zip(coefs, classes)]
top = pd.concat([s.rename("coef").reset_index().assign(side=side) for s, side in parts])
top.columns = ["word", "coef", "side"]
top.to_csv(f"tables/lr_top_words{suffix}.csv", index=False)

task = "binary sentiment" if binary else "4 rating groups"
new = [(f"cm_{n}{suffix}.png", f"confusion matrix of {n} on the test set, {task}") for n in scores]
new += [(f"bert_curve{suffix}.png", f"train and validation loss of DistilBERT per epoch, {task}")]
new += [("roc_all.png", "ROC curves of all models on the test set")] if binary else []
new = pd.DataFrame(new, columns=["filename", "description"])
index = pd.read_csv("tables/figure_index.csv")
pd.concat([index[~index.filename.isin(new.filename)], new]).to_csv("tables/figure_index.csv", index=False)
