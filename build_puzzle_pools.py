import csv
import json
import os

# ensure data folder exists
os.makedirs("data", exist_ok=True)

easy = []
medium = []
hard = []
insane = []

LIMIT = 2000

print("Building puzzle pools...")

with open("lichess_puzzles.csv", encoding="utf-8") as f:

    reader = csv.DictReader(f)

    for row in reader:

        rating = int(row["Rating"])

        puzzle = {
            "fen": row["FEN"],
            "moves": row["Moves"].split(),
            "themes": row["Themes"].split(),
            "rating": rating
        }

        # difficulty pools
        if 400 <= rating < 800 and len(easy) < LIMIT:
            easy.append(puzzle)

        elif 800 <= rating < 1400 and len(medium) < LIMIT:
            medium.append(puzzle)

        elif 1400 <= rating < 2000 and len(hard) < LIMIT:
            hard.append(puzzle)

        elif rating >= 2000 and len(insane) < LIMIT:
            insane.append(puzzle)

        # stop early once pools filled
        if (
            len(easy) == LIMIT and
            len(medium) == LIMIT and
            len(hard) == LIMIT and
            len(insane) == LIMIT
        ):
            break


# save pools
with open("data/easy.json", "w") as f:
    json.dump(easy, f, indent=2)

with open("data/medium.json", "w") as f:
    json.dump(medium, f, indent=2)

with open("data/hard.json", "w") as f:
    json.dump(hard, f, indent=2)

with open("data/insane.json", "w") as f:
    json.dump(insane, f, indent=2)


print("Done.")
print("Pools created:")
print("easy:", len(easy))
print("medium:", len(medium))
print("hard:", len(hard))
print("insane:", len(insane))