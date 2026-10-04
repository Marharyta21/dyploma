import re
import pandas as pd
from sklearn.model_selection import train_test_split


def clean(text):
    text = text.lower()
    text = re.sub(r"<[^>]+>", " ", text)
    # punctuation is dropped, apostrophes stay so "don't" is not split in two
    text = re.sub(r"[^a-z0-9' ]", " ", text)
    return re.sub(r" +", " ", text).strip()


if __name__ == "__main__":
    df = pd.read_csv("data/processed/reviews_raw.csv")
    df["text"] = df["text"].map(clean)
    # rating groups for the 4-class task: 1-2, 3-4, 7-8, 9-10 (ratings 5-6 are not in the dataset)
    df["label4"] = pd.cut(df.rating, bins=[0, 2, 4, 8, 10], labels=False)
    df.to_csv("data/processed/reviews_clean.csv", index=False)

    train = df[df.split == "train"].drop(columns="split")
    test = df[df.split == "test"].drop(columns="split")
    train, val = train_test_split(train, test_size=0.2, stratify=train.label, random_state=42)
    for name, part in [("train", train), ("val", val), ("test", test)]:
        part.to_csv(f"data/processed/{name}.csv", index=False)
        print(name, part.shape, round(part.label.mean(), 3), part.label4.value_counts().sort_index().tolist())
