import sys
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import StratifiedKFold
from sklearn.naive_bayes import MultinomialNB
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC
from sklearn.metrics import accuracy_score, f1_score

# python -m reviews.cv label4 runs the same folds on the 4 rating groups, no argument means binary sentiment
target = sys.argv[1] if len(sys.argv) > 1 else "label"
suffix = "" if target == "label" else "_" + target
avg = "binary" if target == "label" else "macro"

df = pd.concat([pd.read_csv("data/processed/train.csv"), pd.read_csv("data/processed/val.csv")], ignore_index=True)
y = df[target]


def best(name):
    grid = pd.read_csv(f"tables/grid_{name}{suffix}.csv")
    return grid.loc[grid.val_acc.idxmax(), grid.columns[0]]


models = {
    "nb": MultinomialNB(alpha=best("nb")),
    "lr": LogisticRegression(C=best("lr"), max_iter=1000, random_state=42),
    "svm": LinearSVC(C=best("svm"), random_state=42),
}

# the vectorizer is refit on the training fold each time so the test fold never leaks into the vocabulary
skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
scores = {name: [] for name in models}
for k, (tr, te) in enumerate(skf.split(df.text, y)):
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=5, max_features=50000, sublinear_tf=True)
    X_tr, X_te = vec.fit_transform(df.text[tr]), vec.transform(df.text[te])
    for name, clf in models.items():
        pred = clf.fit(X_tr, y[tr]).predict(X_te)
        scores[name].append((accuracy_score(y[te], pred), f1_score(y[te], pred, average=avg)))
        print("fold", k, name, scores[name][-1], flush=True)

rows = []
for name, s in scores.items():
    s = pd.DataFrame(s, columns=["acc", "f1"])
    rows.append((name, s.acc.mean(), s.acc.std(), s.f1.mean(), s.f1.std()))
cv = pd.DataFrame(rows, columns=["model", "acc_mean", "acc_std", "f1_mean", "f1_std"]).round(4)
cv.to_csv(f"tables/cv_results{suffix}.csv", index=False)
print(cv)
