import time
from pathlib import Path
import joblib
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

train = pd.read_csv("data/processed/train.csv")
vec = TfidfVectorizer(ngram_range=(1, 2), min_df=5, max_features=50000, sublinear_tf=True)
t = time.time()
vec.fit(train.text)
fit_time = round(time.time() - t, 2)

Path("models").mkdir(exist_ok=True)
joblib.dump(vec, "models/tfidf.joblib")
print(len(vec.vocabulary_), "features")

# vectorizer fitting is part of the cost of every classical model, so it goes into the same times file
times = pd.read_csv("tables/train_times.csv")
times = times[times.model != "tfidf"].reset_index(drop=True)
times.loc[len(times)] = ["tfidf", fit_time]
times.to_csv("tables/train_times.csv", index=False)
print("fit time", fit_time)
