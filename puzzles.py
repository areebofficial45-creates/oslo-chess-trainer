import json
import random
import chess
import os

# linking to railway volume
STATS_FILE = "/data/stats.json" if os.path.exists("/data") else "stats.json"

def load_stats():
    try:
        if not os.path.exists(STATS_FILE):
            return {}
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading stats: {e}")
        return {}

def save_stats(stats):
    try:
        # Ensure the volume directory exists
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=4)
    except Exception as e:
        print(f"Error saving stats: {e}")

active_puzzles = {}

def start_puzzle(user_id, puzzle):
    board = chess.Board(puzzle["fen"])
    moves = puzzle["moves"]
    first_move = chess.Move.from_uci(moves[0])
    first_move_san = board.san(first_move)
    board.push(first_move)

    active_puzzles[user_id] = {
        "fen": board.fen(),
        "initial_fen": puzzle["fen"],
        "moves": moves,
        "themes": puzzle["themes"],
        "rating": puzzle["rating"],
        "move_index": 1
    }
    return first_move_san

def check_move(user_id, move):
    if user_id not in active_puzzles:
        return "No active puzzle."

    puzzle_state = active_puzzles[user_id]
    moves = puzzle_state["moves"]
    index = puzzle_state["move_index"]
    correct_move = moves[index]
    board = chess.Board(puzzle_state["fen"])

    for i in range(1, index):
        board.push(chess.Move.from_uci(moves[i]))

    correct_san = board.san(chess.Move.from_uci(correct_move))

    if move.strip().lower().replace("+", "").replace("#", "") == correct_san.lower().replace("+", "").replace("#", ""):
        index += 1
        puzzle_state["move_index"] = index

        if index >= len(moves):
            rating = puzzle_state["rating"]
            del active_puzzles[user_id]
            
            stats = load_stats()
            uid = str(user_id)

            if uid not in stats:
                stats[uid] = {"solved": 0, "best": 0}

            stats[uid]["solved"] += 1
            if rating > stats[uid]["best"]:
                stats[uid]["best"] = rating

            save_stats(stats)
            return "Correct! Puzzle solved.✅"

        board.push(chess.Move.from_uci(correct_move))
        opponent_move = board.san(chess.Move.from_uci(moves[index]))
        index += 1
        puzzle_state["move_index"] = index

        if index >= len(moves):
            del active_puzzles[user_id]
            return f"Correct. Opponent plays {opponent_move}. Puzzle solved!"

        return f"Correct. Opponent plays {opponent_move}. Your move?"
    else:
        return "Incorrect move. Try again.❌"

def load_puzzles(level):
    with open(f"data/{level}.json", "r") as file:
        puzzles = json.load(file)
    return puzzles

def get_random_puzzle(level):
    puzzles = load_puzzles(level)
    return random.choice(puzzles)
