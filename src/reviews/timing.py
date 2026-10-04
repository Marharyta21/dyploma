"""Prediction time of every binary model, as a batch over the test set and one review at a time."""
import time
import joblib
import torch
import pandas as pd
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from reviews.nn_models import CNN, LSTM, encode

test = pd.read_csv("data/processed/test.csv")
# bert on cpu is slow, so cpu runs and all one-by-one runs use a fixed sample instead of the whole test set
sample = test.sample(1000, random_state=42).text.tolist()
single = sample[:200]
vec = joblib.load("models/tfidf.joblib")
rows = []


def add(model, device, mode, texts, seconds):
    rows.append((model, device, mode, len(texts), round(seconds, 2), round(1000 * seconds / len(texts), 3)))
    print(rows[-1], flush=True)


for name in ["nb", "lr", "svm"]:
    clf = joblib.load(f"models/{name}.joblib")
    t = time.time()
    clf.predict(vec.transform(test.text))
    add(name, "cpu", "batch", test.text, time.time() - t)
    t = time.time()
    for text in single:
        clf.predict(vec.transform([text]))
    add(name, "cpu", "single", single, time.time() - t)

vocab = joblib.load("models/nn_vocab.joblib")
for name, Model in [("cnn", CNN), ("lstm", LSTM)]:
    for device in ["mps", "cpu"]:
        model = Model(len(vocab) + 2, 2)
        model.load_state_dict(torch.load(f"models/{name}.pt"))
        model = model.to(device).eval()
        with torch.no_grad():
            t = time.time()
            for i in range(0, len(test), 256):
                model(encode(test.text[i:i + 256], vocab).to(device)).argmax(dim=1).tolist()
            add(name, device, "batch", test.text, time.time() - t)
            t = time.time()
            for text in single:
                model(encode([text], vocab).to(device)).argmax(dim=1).tolist()
            add(name, device, "single", single, time.time() - t)

tok = AutoTokenizer.from_pretrained("models/distilbert_20000")
for device in ["mps", "cpu"]:
    model = AutoModelForSequenceClassification.from_pretrained("models/distilbert_20000").to(device).eval()
    texts = test.text.tolist() if device == "mps" else sample
    with torch.no_grad():
        model(**tok(single[:8], truncation=True, max_length=256, padding=True, return_tensors="pt").to(device))
        t = time.time()
        for i in range(0, len(texts), 64):
            enc = tok(texts[i:i + 64], truncation=True, max_length=256, padding=True, return_tensors="pt").to(device)
            model(**enc).logits.argmax(dim=1).tolist()
        add("bert_20000", device, "batch", texts, time.time() - t)
        t = time.time()
        for text in single:
            enc = tok([text], truncation=True, max_length=256, return_tensors="pt").to(device)
            model(**enc).logits.argmax(dim=1).tolist()
        add("bert_20000", device, "single", single, time.time() - t)

cols = ["model", "device", "mode", "n_reviews", "total_s", "ms_per_review"]
pd.DataFrame(rows, columns=cols).to_csv("tables/inference_times.csv", index=False)
