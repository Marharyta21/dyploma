# Codebook

All files are UTF-8 csv with a header row, produced from `data/raw/aclImdb/`.

## `reviews_raw.csv`, `reviews_clean.csv`

| Column | Type | Meaning | Allowed values |
|--------|------|---------|----------------|
| `text` | string | review text. Raw file keeps the original text, clean file holds the output of `preprocess.clean()` | non-empty string |
| `label` | int | sentiment class | 1 = positive (rating 7-10), 0 = negative (rating 1-4) |
| `rating` | int | star rating given by the author, taken from the file name `<id>_<rating>.txt` | 1, 2, 3, 4, 7, 8, 9, 10 |
| `label4` | int | rating group, target of the 4-class task | 0 = rating 1-2, 1 = rating 3-4, 2 = rating 7-8, 3 = rating 9-10 |
| `split` | string | which part of the original dataset the review comes from | `train`, `test` |

## `train.csv`, `val.csv`, `test.csv`

Same `text`, `label`, `rating` and `label4` columns as `reviews_clean.csv`. The `split` column is
dropped because the file name says it. `train` and `val` come from the original train
part (stratified 80/20, `random_state=42`), `test` is the original test part.

| File | Rows | Positive | Negative |
|------|------|----------|----------|
| `train.csv` | 20,000 | 10,000 | 10,000 |
| `val.csv` | 5,000 | 2,500 | 2,500 |
| `test.csv` | 25,000 | 12,500 | 12,500 |

Rows per `label4` group (the groups are unbalanced because ratings 1 and 10 are the most common):

| File | 1-2 | 3-4 | 7-8 | 9-10 |
|------|-----|-----|-----|------|
| `train.csv` | 5,926 | 4,074 | 4,405 | 5,595 |
| `val.csv` | 1,458 | 1,042 | 1,100 | 1,400 |
| `test.csv` | 7,324 | 5,176 | 5,157 | 7,343 |

## `tables/summary_stats.csv`

| Column | Meaning |
|--------|---------|
| `split`, `label` | group key |
| `count` | number of reviews in the group |
| `mean`, `median`, `max` | review length in words after cleaning |

## `tables/duplicates.csv`

| Column | Meaning |
|--------|---------|
| `split` | `train`, `test`, `all`, or `train_vs_test` for texts present in both parts |
| `duplicate_rows` | rows whose text is an exact copy of an earlier row |
| `empty_rows` | rows with zero words after cleaning |

## `tables/figure_index.csv`

`filename` of a png in `figures/` and a one-line `description`.
