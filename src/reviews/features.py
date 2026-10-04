from pathlib import Path
import joblib
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

train = pd.read_csv("data/processed/train.csv")
vec = TfidfVectorizer(ngram_range=(1, 2), min_df=5, max_features=50000, sublinear_tf=True)
vec.fit(train.text)

Path("models").mkdir(exist_ok=True)
joblib.dump(vec, "models/tfidf.joblib")
print(len(vec.vocabulary_), "features")
