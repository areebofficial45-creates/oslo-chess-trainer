# Copyright (c) 2026 Night Wing. All Rights Reserved.
"""
build_puzzle_pools.py
─────────────────────
Streams puzzles from the Lichess CSV directly into PostgreSQL.
Never loads more than one batch into memory at a time.

How to use:
  1. Download: https://database.lichess.org/lichess_db_puzzle.csv.zst
  2. Decompress: python decompress_puzzles.py
  3. Set DATABASE_URL:
       Windows: $env:DATABASE_URL="postgresql://..."
  4. Run: python build_puzzle_pools.py

This is a one-time data pipeline. The bot never calls this script.
"""

import asyncio
import asyncpg
import csv
import json
import os
import random

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

INPUT_FILE   = "lichess_puzzles.csv"
OUTPUT_DIR   = "data"
DATABASE_URL = os.getenv("DATABASE_URL")

# How many puzzles to insert per level into PostgreSQL
DB_POOL_SIZE = 50000

# How many to save in JSON fallback files
JSON_FALLBACK = 2000

# Difficulty bands — must match scoring tiers in puzzles.py exactly
DIFFICULTY_BANDS = {
    "easy":   (400,  999),
    "medium": (1000, 1499),
    "hard":   (1500, 1999),
    "insane": (2000, 9999),
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_display_theme(themes: list) -> str:
    priority = ["mateIn1", "mateIn2", "mate", "sacrifice", "fork", "pin",
                "endgame", "middlegame", "opening", "promotion"]
    for t in priority:
        if t in themes:
            return t
    skip = {"crushing", "advantage", "short", "long", "veryLong",
            "master", "masterVsMaster", "oneMove"}
    for t in themes:
        if t not in skip:
            return t
    return themes[0] if themes else "tactic"


def level_for_rating(rating: int):
    for level, (lo, hi) in DIFFICULTY_BANDS.items():
        if lo <= rating <= hi:
            return level
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def build():
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: {INPUT_FILE} not found.")
        return

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.")
        print("Run: $env:DATABASE_URL='postgresql://...'")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Connect and set up DB first ───────────────────────────────────────────
    print("Connecting to PostgreSQL...")
    conn = await asyncpg.connect(DATABASE_URL)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS puzzles (
            id            SERIAL PRIMARY KEY,
            fen           TEXT    NOT NULL,
            moves         TEXT    NOT NULL,
            themes        TEXT    NOT NULL,
            rating        INTEGER NOT NULL,
            level         TEXT    NOT NULL,
            display_theme TEXT
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_puzzles_level ON puzzles (level)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_puzzles_level_theme ON puzzles (level, display_theme)")

    # Clear existing puzzles
    existing = await conn.fetchval("SELECT COUNT(*) FROM puzzles")
    if existing > 0:
        print(f"Clearing {existing:,} existing puzzles...")
        await conn.execute("TRUNCATE TABLE puzzles")

    print(f"DB ready. Target: {DB_POOL_SIZE:,} per level = {DB_POOL_SIZE*4:,} total")
    print("Reading CSV and streaming to DB...\n")

    # ── Track counts per level ────────────────────────────────────────────────
    inserted  = {level: 0 for level in DIFFICULTY_BANDS}
    # Small JSON sample buffers — kept in memory
    samples   = {level: [] for level in DIFFICULTY_BANDS}
    done      = set()   # levels that have hit DB_POOL_SIZE

    BATCH_SIZE = 500
    batch      = []
    total_read = 0

    with open(INPUT_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            total_read += 1

            # Progress update every 100k rows
            if total_read % 100_000 == 0:
                print(f"  Rows read: {total_read:,} | inserted: { {k: v for k,v in inserted.items()} }")
                print(f"  Levels complete: {sorted(done)}")

            # Stop once all 4 levels are done
            if len(done) == 4:
                print(f"\n  All levels complete at row {total_read:,} — stopping")
                break

            try:
                rating = int(row["Rating"])
            except (ValueError, KeyError):
                continue

            level = level_for_rating(rating)
            if level is None or level in done:
                continue

            themes = row["Themes"].split()
            moves  = row["Moves"].split()
            if not themes or not moves:
                continue

            display_theme = get_display_theme(themes)
            themes_str    = row["Themes"]

            # Add to DB batch
            batch.append((row["FEN"], " ".join(moves), themes_str,
                          rating, level, display_theme))
            inserted[level] += 1

            # Keep a small sample for JSON fallback
            if len(samples[level]) < JSON_FALLBACK:
                samples[level].append({
                    "fen":           row["FEN"],
                    "moves":         moves,
                    "themes":        themes,
                    "rating":        rating,
                    "display_theme": display_theme,
                })

            # Mark level as done
            if inserted[level] >= DB_POOL_SIZE:
                done.add(level)
                print(f"  ✅ {level} complete ({DB_POOL_SIZE:,} puzzles)")

            # Flush batch to DB
            if len(batch) >= BATCH_SIZE:
                await conn.copy_records_to_table(
                    "puzzles",
                    records=batch,
                    columns=["fen", "moves", "themes", "rating", "level", "display_theme"]
                )
                batch.clear()

    # Flush any remaining batch
    if batch:
        await conn.copy_records_to_table(
            "puzzles",
            records=batch,
            columns=["fen", "moves", "themes", "rating", "level", "display_theme"]
        )

    await conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    total_inserted = sum(inserted.values())
    print(f"\n[DB] Done. Total inserted: {total_inserted:,}")
    for level, count in inserted.items():
        print(f"  {level}: {count:,}")

    # ── Save JSON fallback files ───────────────────────────────────────────────
    print(f"\nSaving JSON fallback files ({JSON_FALLBACK}/level)...")
    for level, puzzles in samples.items():
        path = os.path.join(OUTPUT_DIR, f"{level}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(puzzles, f, indent=2)
        print(f"  {level}: {len(puzzles)} puzzles → {path}")

    print("\nDone. Restart the bot to use new puzzles.")


if __name__ == "__main__":
    asyncio.run(build())