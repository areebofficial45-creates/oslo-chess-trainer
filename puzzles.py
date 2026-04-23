# Copyright (c) 2026 Night Wing. All Rights Reserved.

import json
import random
import chess
import os
import asyncio
import asyncpg
import shutil

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR   = "/data" if os.path.exists("/data") else "."
STATS_FILE = os.path.join(DATA_DIR, "stats.json")

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
_pool: asyncpg.Pool = None

# ── Active puzzle sessions ─────────────────────────────────────────────────────
active_puzzles: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def init_db():
    """
    Create connection pool, create tables, run legacy migration.
    Called once from bot.py setup_hook before bot connects to Discord.
    asyncpg is natively async — no thread pool needed anywhere.
    """
    global _pool

    if not DATABASE_URL:
        raise RuntimeError(
            "[Oslo] DATABASE_URL not set. "
            "Add it to Railway Variables from the Postgres service."
        )

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )

    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      TEXT PRIMARY KEY,
                solved       INTEGER NOT NULL DEFAULT 0,
                best_rating  INTEGER NOT NULL DEFAULT 0,
                total_score  INTEGER NOT NULL DEFAULT 0,
                hints_used   INTEGER NOT NULL DEFAULT 0,
                streak       INTEGER NOT NULL DEFAULT 0,
                best_streak  INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                id                   INTEGER PRIMARY KEY DEFAULT 1,
                total_puzzles_solved BIGINT NOT NULL DEFAULT 0,
                total_interactions   BIGINT NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            INSERT INTO bot_stats (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_guilds (
                user_id   TEXT NOT NULL,
                guild_id  TEXT NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
        """)

    print("[Oslo] PostgreSQL connected and tables ready")
    await _migrate_json_to_db()


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION
# ─────────────────────────────────────────────────────────────────────────────

async def _migrate_json_to_db():
    """
    Reads /data/stats.json from Railway volume on first boot.
    Score = solved * 8 as legacy baseline.
    Backfills score=0 users if re-run safely.
    Renames file after migration.
    """
    source = None
    if os.path.exists(STATS_FILE):
        source = STATS_FILE
    elif os.path.exists(STATS_FILE + ".migrated"):
        source = STATS_FILE + ".migrated"

    if not source:
        return

    try:
        with open(source, "r") as f:
            legacy = json.load(f)
        if not legacy or not isinstance(legacy, dict):
            return

        migrated = 0
        async with _pool.acquire() as conn:
            for user_id, data in legacy.items():
                solved       = data.get("solved", 0)
                best         = data.get("best", 0)
                legacy_score = solved * 8

                existing = await conn.fetchrow(
                    "SELECT total_score FROM users WHERE user_id = $1", user_id
                )
                if existing is None:
                    await conn.execute("""
                        INSERT INTO users (user_id, solved, best_rating, total_score)
                        VALUES ($1, $2, $3, $4)
                    """, user_id, solved, best, legacy_score)
                    migrated += 1
                elif existing["total_score"] == 0 and legacy_score > 0:
                    await conn.execute("""
                        UPDATE users
                        SET total_score = $1,
                            solved      = $2,
                            best_rating = GREATEST(best_rating, $3)
                        WHERE user_id = $4
                    """, legacy_score, solved, best, user_id)
                    migrated += 1

        # Backfill bot_stats with legacy totals
        # Uses GREATEST so it never overwrites higher real values
        total_solved = sum(d.get('solved', 0) for d in legacy.values())
        async with _pool.acquire() as conn:
            await conn.execute("""
                UPDATE bot_stats
                SET total_puzzles_solved = GREATEST(total_puzzles_solved, $1),
                    total_interactions   = GREATEST(total_interactions,   $2)
                WHERE id = 1
            """, total_solved, total_solved * 3)
        print(f"[Oslo] Bot stats backfilled: {total_solved} solved, {total_solved * 3} interactions")

        if source == STATS_FILE:
            try:
                shutil.move(STATS_FILE, STATS_FILE + ".migrated")
            except Exception:
                pass

        print(f"[Oslo] Migration: {migrated} users imported from stats.json")
    except Exception as e:
        print(f"[Oslo] Migration warning: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# USER STATS
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_user(user_id: str, solved_delta: int, rating: int,
                       score_delta: int, hint_used: bool, won: bool):
    """Atomic upsert — single connection, no race condition."""
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)

        row = await conn.fetchrow(
            "SELECT streak FROM users WHERE user_id = $1", user_id
        )
        new_streak = ((row["streak"] + 1) if won else 0) if row else (1 if won else 0)

        await conn.execute("""
            UPDATE users
            SET solved      = solved      + $1,
                best_rating = GREATEST(best_rating, $2),
                total_score = total_score + $3,
                hints_used  = hints_used  + $4,
                streak      = $5,
                best_streak = GREATEST(best_streak, $5)
            WHERE user_id = $6
        """, solved_delta, rating, score_delta,
             1 if hint_used else 0, new_streak, user_id)

        await conn.execute("""
            UPDATE bot_stats
            SET total_puzzles_solved = total_puzzles_solved + $1,
                total_interactions   = total_interactions   + 1
            WHERE id = 1
        """, solved_delta)


async def get_user_stats(user_id: str):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1", user_id
        )
        return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# BOT STATS
# ─────────────────────────────────────────────────────────────────────────────

async def get_bot_stats():
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bot_stats WHERE id = 1")
        return dict(row) if row else {}


async def record_interaction():
    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE bot_stats
            SET total_interactions = total_interactions + 1
            WHERE id = 1
        """)


# ─────────────────────────────────────────────────────────────────────────────
# GUILD TRACKING
# ─────────────────────────────────────────────────────────────────────────────

async def record_guild(user_id: str, guild_id: str):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_guilds (user_id, guild_id) VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, user_id, guild_id)


async def import_legacy_to_guild(guild_id: str) -> int:
    """Link all legacy stats.json users to a guild for server leaderboard."""
    source = STATS_FILE + ".migrated" if os.path.exists(STATS_FILE + ".migrated") else STATS_FILE
    if not os.path.exists(source):
        return 0
    try:
        with open(source, "r") as f:
            legacy = json.load(f)
        if not legacy or not isinstance(legacy, dict):
            return 0
        inserted = 0
        async with _pool.acquire() as conn:
            for user_id in legacy:
                result = await conn.execute("""
                    INSERT INTO user_guilds (user_id, guild_id) VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                """, str(user_id), str(guild_id))
                # asyncpg returns "INSERT 0 N" — check last char for row count
                if result.split()[-1] != "0":
                    inserted += 1
        print(f"[Oslo] Linked {inserted} legacy users to guild {guild_id}")
        return inserted
    except Exception as e:
        print(f"[Oslo] Guild migration warning: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# LEADERBOARDS
# ─────────────────────────────────────────────────────────────────────────────

async def get_server_leaderboard(guild_id: str, sort_by: str = "score", limit: int = 10):
    order_col = "u.total_score" if sort_by == "score" else "u.solved"
    async with _pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT u.user_id, u.total_score, u.solved, u.best_rating
            FROM users u
            INNER JOIN user_guilds ug ON u.user_id = ug.user_id
            WHERE ug.guild_id = $1
            ORDER BY {order_col} DESC
            LIMIT $2
        """, guild_id, limit)
        return [dict(r) for r in rows]


async def get_global_leaderboard(sort_by: str = "score", limit: int = 10):
    order_col = "total_score" if sort_by == "score" else "solved"
    async with _pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT user_id, total_score, solved, best_rating
            FROM users
            ORDER BY {order_col} DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT  (for future migration to Oracle or any other DB)
# ─────────────────────────────────────────────────────────────────────────────

async def get_puzzle_source() -> dict:
    """Returns puzzle source info for !adminstatus."""
    if _pool is None:
        return {"source": "JSON fallback", "count": 0}
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT level, COUNT(*) as count FROM puzzles GROUP BY level"
            )
            if not rows:
                return {"source": "JSON fallback", "count": 0}
            total     = sum(r['count'] for r in rows)
            breakdown = {r['level']: r['count'] for r in rows}
            return {"source": "PostgreSQL", "count": total, "breakdown": breakdown}
    except Exception:
        return {"source": "JSON fallback", "count": 0}


async def export_all_data() -> dict:
    """
    Dumps everything from PostgreSQL as a dict.
    Used by !exportdata admin command.
    Safe to use for migrating to Oracle, Fly.io, or any future provider.
    Format: {"users": [...], "user_guilds": [...], "bot_stats": [...]}
    """
    async with _pool.acquire() as conn:
        users  = [dict(r) for r in await conn.fetch("SELECT * FROM users")]
        guilds = [dict(r) for r in await conn.fetch("SELECT * FROM user_guilds")]
        stats  = [dict(r) for r in await conn.fetch("SELECT * FROM bot_stats")]
    return {"users": users, "user_guilds": guilds, "bot_stats": stats}


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def calculate_score(rating: int, hint_used: bool,
                    wrong_moves: int = 0, clean_bonus: bool = False) -> int:
    if   rating < 1000: base = 10
    elif rating < 1500: base = 20
    elif rating < 2000: base = 30
    elif rating < 2400: base = 50
    else:               base = 60
    if hint_used:
        base = int(base * 0.7)
    base -= wrong_moves * 3
    if clean_bonus:
        base += 5
    return max(0, base)


# ─────────────────────────────────────────────────────────────────────────────
# PUZZLE SESSION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def start_puzzle(user_id: int, puzzle: dict) -> str:
    board          = chess.Board(puzzle["fen"])
    moves          = puzzle.get("moves", [])
    if not moves:
        raise ValueError(f"Puzzle has no moves: {puzzle.get('id', '?')}")
    first_move     = chess.Move.from_uci(moves[0])
    first_move_san = board.san(first_move)
    board.push(first_move)
    active_puzzles[user_id] = {
        "fen":           board.fen(),
        "initial_fen":   puzzle["fen"],
        "moves":         moves,
        "themes":        puzzle.get("themes", []),
        "display_theme": puzzle.get("display_theme"),  # precomputed by builder
        "rating":        puzzle.get("rating", 0),
        "move_index":    1,
        "hint_used":     False,
        "history":       [board.fen()],
    }
    return first_move_san


def check_move(user_id: int, move_input: str):
    if user_id not in active_puzzles:
        return "No active puzzle.", False
    state = active_puzzles[user_id]
    moves = state["moves"]
    index = state["move_index"]
    board = chess.Board(state["initial_fen"])
    for uci in moves[:index]:
        board.push(chess.Move.from_uci(uci))
    if index >= len(moves):
        active_puzzles.pop(user_id, None)
        return "Puzzle already completed.", True
    correct_uci = moves[index]
    try:
        correct_san = board.san(chess.Move.from_uci(correct_uci))
    except Exception:
        active_puzzles.pop(user_id, None)
        return "Puzzle data error. Session cleared.", True

    def norm(s):
        return s.strip().lower().replace("+","").replace("#","").replace("x","")

    if norm(move_input) != norm(correct_san):
        return "Incorrect move. Try again.", False

    board.push(chess.Move.from_uci(correct_uci))
    index += 1
    state["move_index"] = index
    state["history"].append(board.fen())

    if index >= len(moves):
        return "Correct! Puzzle solved.", True

    opp_uci = moves[index]
    opp_san = board.san(chess.Move.from_uci(opp_uci))
    board.push(chess.Move.from_uci(opp_uci))
    index += 1
    state["move_index"] = index
    state["fen"]        = board.fen()
    state["history"].append(board.fen())

    if index >= len(moves):
        return f"Correct. Opponent plays {opp_san}. Puzzle solved!", True

    return f"Correct. Opponent plays {opp_san}. Your move?", False


async def finish_puzzle(user_id: int, wrong_moves: int = 0, guild_id: str = None):
    if user_id not in active_puzzles:
        return
    wrong_moves = max(0, min(wrong_moves, 20))
    state     = active_puzzles.pop(user_id)
    rating    = state["rating"]
    hint_used = state["hint_used"]
    clean     = (not hint_used) and (wrong_moves == 0)
    score     = calculate_score(rating, hint_used, wrong_moves, clean)
    await _upsert_user(str(user_id), 1, rating, score, hint_used, True)
    if guild_id:
        await record_guild(str(user_id), str(guild_id))


async def forfeit_puzzle(user_id: int):
    if user_id not in active_puzzles:
        return
    state  = active_puzzles.pop(user_id)
    rating = state["rating"]
    await _upsert_user(str(user_id), 0, rating, 0, False, False)


def get_hint(user_id: int):
    if user_id not in active_puzzles:
        return None
    state = active_puzzles[user_id]
    moves = state["moves"]
    index = state["move_index"]
    board = chess.Board(state["initial_fen"])
    for uci in moves[:index]:
        board.push(chess.Move.from_uci(uci))
    if index >= len(moves):
        return None
    try:
        move_obj  = chess.Move.from_uci(moves[index])
        piece     = board.piece_at(move_obj.from_square)
        piece_sym = piece.symbol().upper() if piece else "?"
        dest      = chess.square_name(move_obj.to_square)
    except Exception:
        return None
    state["hint_used"] = True
    names = {"K":"King","Q":"Queen","R":"Rook","B":"Bishop","N":"Knight","P":"Pawn"}
    return f"Move your **{names.get(piece_sym, piece_sym)}** to **{dest}**"


# ─────────────────────────────────────────────────────────────────────────────
# PUZZLE LOADING
# Primary:  PostgreSQL (50k/level, fast indexed queries, zero RAM)
# Fallback: JSON files (2000/level, used if DB query fails)
# ─────────────────────────────────────────────────────────────────────────────

_puzzle_cache: dict = {}   # JSON fallback cache — loaded only if DB fails

# Core chess themes exposed in bot commands
SUPPORTED_THEMES = {
    "sacrifice", "endgame", "middlegame", "opening",
    "fork", "pin", "mate", "mateIn1", "mateIn2", "promotion",
}

LEVELS = ["easy", "medium", "hard", "insane"]


def _load_json_fallback(level: str) -> list:
    """Load JSON fallback file into memory cache."""
    if level not in _puzzle_cache:
        path = f"data/{level}.json"
        if not os.path.exists(path):
            raise FileNotFoundError(f"No fallback file: {path}")
        with open(path, "r") as f:
            data = json.load(f)
        if not data or not isinstance(data, list):
            raise ValueError(f"Fallback file empty: {path}")
        _puzzle_cache[level] = data
    return _puzzle_cache[level]


def _row_to_puzzle(row) -> dict:
    """Convert a DB row to the puzzle dict the bot expects."""
    return {
        "fen":           row["fen"],
        "moves":         row["moves"].split(),
        "themes":        row["themes"].split(),
        "rating":        row["rating"],
        "display_theme": row["display_theme"],
    }


async def get_random_puzzle(level: str, theme: str = None) -> dict:
    """
    Returns a random puzzle.
    Tries PostgreSQL first — instant query from 50k pool.
    Falls back to JSON files if DB is unavailable.

    level: easy / medium / hard / insane
    theme: optional — e.g. 'sacrifice', 'endgame'
    """
    # ── PostgreSQL primary ────────────────────────────────────────────────────
    if _pool is not None:
        try:
            async with _pool.acquire() as conn:
                if theme:
                    # Pick random from up to 200 matching rows — fast and varied
                    row = await conn.fetchrow("""
                        SELECT * FROM (
                            SELECT * FROM puzzles
                            WHERE level = $1
                              AND themes LIKE $2
                            LIMIT 200
                        ) sub
                        ORDER BY RANDOM()
                        LIMIT 1
                    """, level, f"%{theme}%")
                    if row is None:
                        # Theme not found at this level — fall back to any theme
                        row = await conn.fetchrow("""
                            SELECT * FROM (
                                SELECT * FROM puzzles
                                WHERE level = $1
                                LIMIT 200
                            ) sub
                            ORDER BY RANDOM()
                            LIMIT 1
                        """, level)
                else:
                    # Pick random from first 200 rows in this level — very fast
                    row = await conn.fetchrow("""
                        SELECT * FROM (
                            SELECT * FROM puzzles
                            WHERE level = $1
                            LIMIT 200
                        ) sub
                        ORDER BY RANDOM()
                        LIMIT 1
                    """, level)

                if row is not None:
                    return _row_to_puzzle(row)
        except Exception as e:
            print(f"[Oslo] DB puzzle query failed, using JSON fallback: {e}")

    # ── JSON fallback ─────────────────────────────────────────────────────────
    pool = _load_json_fallback(level)
    if theme:
        t        = theme.lower()
        filtered = [p for p in pool
                    if t in [x.lower() for x in p.get("themes", [])]]
        if len(filtered) >= 5:
            return random.choice(filtered)
    return random.choice(pool)


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY NAME HELPER
# ─────────────────────────────────────────────────────────────────────────────

def format_display_name(display_name: str, user_id: int) -> str:
    suffix = str(user_id)[-3:]
    clean  = display_name.strip().replace(" ", "")[:16]
    return f"{clean}_{suffix}"
