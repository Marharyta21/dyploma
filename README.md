# Comparison of text classification methods for reviews

Source code for my engineering thesis at PJATK. Sentiment classification of IMDB film reviews (Large Movie Review Dataset, Maas et al. 2011) with Multinomial Naive Bayes, Logistic Regression and Linear SVM on TF-IDF, a CNN and a BiLSTM with embeddings trained from scratch, and a fine-tuned DistilBERT.

## Setup

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

Download `aclImdb_v1.tar.gz` from https://ai.stanford.edu/~amaas/data/sentiment/ and unpack it into `data/raw/`.

## Running

```
python -m reviews.load
python -m reviews.eda
python -m reviews.preprocess
python -m reviews.features
python -m reviews.train_classical
python -m reviews.cv
python -m reviews.train_bert 20000
python -m reviews.train_nn cnn
python -m reviews.train_nn lstm
python -m reviews.evaluate
python -m reviews.mcnemar
python -m reviews.overlap
python -m reviews.timing
```

Adding `label4` to `train_classical`, `cv`, `evaluate`, `mcnemar` (or as the second argument of `train_bert`) runs the four-class rating task instead of binary sentiment.

Results go to `tables/` and `figures/`, models to `models/` (not in the repo).

## API

```
uvicorn api.main:app
```

Then open http://localhost:8000. There is also a `Dockerfile`. Tests: `pytest`.
