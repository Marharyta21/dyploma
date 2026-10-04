import sys
import time
import joblib
import pandas as pd
from sklearn.naive_bayes import MultinomialNB
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC

# python -m reviews.train_classical label4 trains on the 4 rating groups, no argument means binary sentiment
target = sys.argv[1] if len(sys.argv) > 1 else "label"
suffix = "" if target == "label" else "_" + target

vec = joblib.load("models/tfidf.joblib")
train = pd.read_csv("data/processed/train.csv")
val = pd.read_csv("data/processed/val.csv")
X_train, y_train = vec.transform(train.text), train[target]
X_val, y_val = vec.transform(val.text), val[target]

# the lr grid goes up to 100 because with the old grid accuracy was still rising at C=10
grids = {
    "nb": (MultinomialNB, "alpha", [0.01, 0.1, 0.5, 1.0, 2.0], {}),
    "lr": (LogisticRegression, "C", [0.1, 0.5, 1.0, 5.0, 10.0, 20.0, 50.0, 100.0], {"max_iter": 1000, "random_state": 42}),
    "svm": (LinearSVC, "C", [0.01, 0.05, 0.1, 0.5, 1.0], {"random_state": 42}),
}

times = []
for name, (Model, param, values, extra) in grids.items():
    rows = []
    for v in values:
        t = time.time()
        clf = Model(**{param: v}, **extra).fit(X_train, y_train)
        rows.append((v, clf.score(X_val, y_val), time.time() - t))
    grid = pd.DataFrame(rows, columns=[param, "val_acc", "train_time_s"])
    grid.to_csv(f"tables/grid_{name}{suffix}.csv", index=False)
    print(name, flush=True)
    print(grid, flush=True)

    best = grid.loc[grid.val_acc.idxmax(), param]
    t = time.time()
    clf = Model(**{param: best}, **extra).fit(X_train, y_train)
    times.append((name + suffix, round(time.time() - t, 2)))
    joblib.dump(clf, f"models/{name}{suffix}.joblib")

# one shared times file for both tasks, rows of the same model are replaced
times = pd.DataFrame(times, columns=["model", "train_time_s"])
old = pd.read_csv("tables/train_times.csv")
pd.concat([old[~old.model.isin(times.model)], times]).to_csv("tables/train_times.csv", index=False)
print(times)
