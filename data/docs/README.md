# Dataset: IMDB Large Movie Review Dataset v1.0

## Source

Maas, A. L., Daly, R. E., Pham, P. T., Huang, D., Ng, A. Y., Potts, C. (2011).
*Learning Word Vectors for Sentiment Analysis.* Proceedings of the 49th Annual Meeting
of the Association for Computational Linguistics: Human Language Technologies, pp. 142-150.
Portland, Oregon, USA. http://www.aclweb.org/anthology/P11-1015

Downloaded from https://ai.stanford.edu/~amaas/data/sentiment/ (`aclImdb_v1.tar.gz`),
extracted to `data/raw/aclImdb/`. The raw folder is never modified and is not committed.

## License

The dataset is distributed by Stanford for research use. There is no separate license
file; the authors ask that the ACL 2011 paper above is cited whenever the data is used.
The review texts belong to their IMDb authors, so only aggregate results and short
quotes are placed in the thesis.

## What is in it

- 50,000 English movie reviews from IMDb, 25,000 train and 25,000 test.
- Binary sentiment: positive = star rating 7-10, negative = rating 1-4. Ratings 5 and 6 are not included.
- Exactly balanced: 12,500 positive and 12,500 negative in each of train and test.
- At most 30 reviews per movie, and train and test contain different movies.
- 50,000 extra unlabeled reviews (`train/unsup/`) are shipped with the archive but not used here.

## Files produced by the project

| File | Rows | Script |
|------|------|--------|
| `data/processed/reviews_raw.csv` | 50,000 | `src/reviews/load.py` |
| `data/processed/reviews_clean.csv` | 50,000 | `src/reviews/preprocess.py` |
| `data/processed/train.csv` | 20,000 | `src/reviews/preprocess.py` |
| `data/processed/val.csv` | 5,000 | `src/reviews/preprocess.py` |
| `data/processed/test.csv` | 25,000 | `src/reviews/preprocess.py` |

`train.csv` and `val.csv` are a stratified 80/20 split of the original train part
(`random_state=42`). `test.csv` is the original test part, untouched.

## Cleaning

`preprocess.clean()` lowercases the text, removes HTML tags (mostly `<br />`),
replaces every character that is not a letter, digit or apostrophe with a space
and collapses repeated spaces. Nothing is stemmed and no stop words are removed here;
that is left to the vectoriser.

## Basic numbers (from `tables/`)

- Mean review length after cleaning: about 231 words (median 173, shortest 6, longest 2,473).
- Exact duplicate texts: 99 inside train, 201 inside test, 123 texts appear in both.
  They are kept, since the official split is used as published.
- No empty reviews after cleaning.
- Star ratings are U-shaped: 1 and 10 are by far the most common.

Figures are in `figures/` and listed in `tables/figure_index.csv`.
