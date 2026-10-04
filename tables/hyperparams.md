# Hyperparameters of the compared models

Values for the classical models come from the grid search on the validation set
(`tables/grid_nb.csv`, `tables/grid_lr.csv`, `tables/grid_svm.csv`). The TF-IDF settings
are shared by all three classical models and were fixed before the grid search.

## TF-IDF features (shared by NB, LR, SVM)

| Hyperparameter | Value | Reason |
|---|---|---|
| ngram_range | (1, 2) | bigrams keep short negations such as "not good" together |
| min_df | 5 | drops rare words and typos that appear in fewer than 5 reviews |
| max_features | 50 000 | caps the vocabulary so the matrices fit easily in memory |
| sublinear_tf | True | uses 1 + log(tf), so a word repeated many times in a long review does not dominate |
| fit data | train split only | the validation and test splits never influence the vocabulary or idf weights |

## Multinomial Naive Bayes

| Hyperparameter | Value | Reason |
|---|---|---|
| alpha | 0.01 | best validation accuracy in the grid {0.01, 0.1, 0.5, 1.0, 2.0}; smaller smoothing helped because the vocabulary is large and counts are well estimated |

## Logistic Regression

| Hyperparameter | Value | Reason |
|---|---|---|
| C | 10.0 | best validation accuracy in the grid {0.1, 0.5, 1.0, 5.0, 10.0, 20.0, 50.0, 100.0}; the grid was extended past 10 and accuracy drops again for larger C |
| max_iter | 1000 | the default 100 did not converge on 50k features |
| solver | lbfgs (default) | default in scikit-learn, no reason to change |
| random_state | 42 | same seed as everywhere else in the project |

## Linear SVM (LinearSVC)

| Hyperparameter | Value | Reason |
|---|---|---|
| C | 0.5 | best validation accuracy in the grid {0.01, 0.05, 0.1, 0.5, 1.0} |
| loss | squared_hinge (default) | scikit-learn default |
| random_state | 42 | same seed as everywhere else in the project |

## DistilBERT (fine-tuning)

| Hyperparameter | Value | Reason |
|---|---|---|
| pretrained model | distilbert-base-uncased | small enough to fine-tune on a laptop, reviews are lowercased anyway |
| training set size | 5 000 and 20 000 reviews | 5k subset for a quick run, the whole train split to see how much more data helps |
| max_length | 256 tokens (one extra run with 512, the model maximum) | 256 covers the median review of about 170 words; the 512 run checks how much truncation costs |
| epochs | 4, best one kept | validation loss is checked after each epoch and the weights of the epoch with the lowest validation loss are kept, because the loss rises again after epoch 1-2 |
| batch size | 16 | fits in GPU memory on the Apple M4 Pro with 256 tokens |
| learning rate | 2e-5 | value from the BERT paper (Devlin et al., 2019) fine-tuning range |
| optimizer | AdamW | standard optimizer for transformer fine-tuning |
| seed | 42 | same seed as everywhere else in the project |

## CNN and BiLSTM (word embeddings trained from scratch)

| Hyperparameter | Value | Reason |
|---|---|---|
| vocabulary | 30 000 most frequent training words | covers almost all word occurrences, rarer words become one unknown id |
| max length | 400 words | covers 87 % of the reviews completely, the median review has 174 words |
| embedding size | 128 | trained from scratch, no pre-trained vectors, so the gain of pre-training is visible against DistilBERT |
| CNN filters | sizes 3, 4, 5, 100 filters each, max pooling | setup from Kim (2014) |
| BiLSTM | one bidirectional layer, 128 units per direction, max pooling over words | a small standard recurrent model |
| dropout | 0.5 | value used by Kim (2014) |
| optimizer | Adam, learning rate 1e-3, batch 64 | common defaults for these models |
| epochs | 10, best one kept | same rule as DistilBERT, lowest validation loss; with 5 epochs the CNN was still improving |
| seed | 42 | same seed as everywhere else in the project |

## 4-class rating task

The same hyperparameters are used for the second task, where the target is `label4`
(rating groups 1-2, 3-4, 7-8, 9-10). The classical models rerun the same grids on
that target (`tables/grid_<model>_label4.csv`), the TF-IDF vectorizer is shared and
not refit, and DistilBERT keeps the settings above with `num_labels = 4`.
