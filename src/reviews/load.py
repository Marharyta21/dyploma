from pathlib import Path
import pandas as pd

raw = Path("data/raw/aclImdb")
rows = []
for split in ["train", "test"]:
    for name, label in [("pos", 1), ("neg", 0)]:
        for f in sorted((raw / split / name).glob("*.txt")):
            rating = int(f.stem.split("_")[1])
            rows.append((f.read_text(encoding="utf-8"), label, rating, split))

df = pd.DataFrame(rows, columns=["text", "label", "rating", "split"])
Path("data/processed").mkdir(exist_ok=True)
df.to_csv("data/processed/reviews_raw.csv", index=False)
print(df.shape)
print(df.groupby(["split", "label"]).size())
