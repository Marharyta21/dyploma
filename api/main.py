import os
import joblib
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from reviews.preprocess import clean

vec = joblib.load("models/tfidf.joblib")
clf = joblib.load("models/lr.joblib")
# 4-class rating model is optional so the API also runs with only the two files above
clf4 = joblib.load("models/lr_label4.joblib") if os.path.exists("models/lr_label4.joblib") else None

# same order as pd.cut in preprocess.py
GROUPS = ["1-2", "3-4", "7-8", "9-10"]

app = FastAPI(title="Review sentiment API")


class Review(BaseModel):
    text: str


@app.get("/")
def index():
    return FileResponse("api/static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/predict")
def predict(review: Review):
    X = vec.transform([clean(review.text)])
    proba = clf.predict_proba(X)[0]
    label = "positive" if proba[1] >= 0.5 else "negative"
    out = {"label": label, "probability": float(max(proba))}
    if clf4 is not None:
        out["rating_group"] = GROUPS[clf4.predict(X)[0]]
    return out
