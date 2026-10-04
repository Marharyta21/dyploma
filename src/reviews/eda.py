import pandas as pd
import matplotlib.pyplot as plt
from sklearn.feature_extraction.text import CountVectorizer

df = pd.read_csv("data/processed/reviews_clean.csv")
df["n_words"] = df.text.str.split().str.len()

stats = df.groupby(["split", "label"]).n_words.agg(["count", "mean", "median", "max"]).round(1)
stats.to_csv("tables/summary_stats.csv")
print(stats)

train, test = df[df.split == "train"], df[df.split == "test"]
dup = pd.DataFrame({
    "split": ["train", "test", "all", "train_vs_test"],
    "duplicate_rows": [train.text.duplicated().sum(), test.text.duplicated().sum(), df.text.duplicated().sum(), len(set(train.text) & set(test.text))],
    "empty_rows": [(train.n_words == 0).sum(), (test.n_words == 0).sum(), (df.n_words == 0).sum(), 0],
})
dup.to_csv("tables/duplicates.csv", index=False)
print(dup)

df.groupby(["split", "label"]).size().unstack().plot.bar(rot=0)
plt.title("Class balance per split")
plt.xlabel("split")
plt.ylabel("reviews")
plt.savefig("figures/class_balance.png")
plt.close()

# a handful of reviews are over 2000 words, cut at 1000 so the plot is readable
plt.hist(df.n_words, bins=50, range=(0, 1000))
plt.title("Review length")
plt.xlabel("words")
plt.ylabel("reviews")
plt.savefig("figures/length_hist.png")
plt.close()

df.rating.value_counts().sort_index().plot.bar(rot=0)
plt.title("Star ratings")
plt.xlabel("rating")
plt.ylabel("reviews")
plt.savefig("figures/rating_hist.png")
plt.close()

# default token pattern splits "don't" into "don" and "t", keep the apostrophe inside the token
vec = CountVectorizer(stop_words="english", max_features=20000, token_pattern=r"[a-z][a-z']+")
X = vec.fit_transform(train.text)
words = vec.get_feature_names_out()
for label, name in [(1, "pos"), (0, "neg")]:
    counts = X[(train.label == label).values].sum(axis=0).A1
    top = pd.Series(counts, index=words).nlargest(20)
    top[::-1].plot.barh()
    plt.title(f"Top 20 words, {name} reviews (train)")
    plt.xlabel("count")
    plt.tight_layout()
    plt.savefig(f"figures/top_words_{name}.png")
    plt.close()

index = pd.DataFrame([
    ("class_balance.png", "number of positive and negative reviews in train and test"),
    ("length_hist.png", "histogram of review length in words, cut at 1000"),
    ("rating_hist.png", "distribution of the 1-10 star ratings"),
    ("top_words_pos.png", "20 most frequent words in positive train reviews, stop words removed"),
    ("top_words_neg.png", "20 most frequent words in negative train reviews, stop words removed"),
], columns=["filename", "description"])
index.to_csv("tables/figure_index.csv", index=False)
