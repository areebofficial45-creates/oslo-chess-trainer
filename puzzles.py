import json
import random
import chess


def load_stats():
    try:
        with open("stats.json", "r") as f:
            return json.load(f)
    except:
        return {}

def save_stats(stats):
    with open("stats.json", "w") as f:
        json.dump(stats, f)

active_puzzles = {}


def start_puzzle(user_id, puzzle):

    board = chess.Board(puzzle["fen"])
    moves = puzzle["moves"]

    # play the blunder that leads into the puzzle
    first_move = chess.Move.from_uci(moves[0])
    first_move_san = board.san(first_move)
    board.push(first_move)

    active_puzzles[user_id] = {
        "fen": board.fen(),            # board AFTER blunder
        "initial_fen": puzzle["fen"],  # original position
        "moves": moves,
        "themes": puzzle["themes"],
        "rating": puzzle["rating"],
        "move_index": 1                # user must now play move[1]
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

    for i in range(1 , index):
        board.push(chess.Move.from_uci(moves[i]))

    correct_san = board.san(chess.Move.from_uci(correct_move))

    # normalize moves (ignore + and #)
    if move.strip().lower().replace("+", "").replace("#", "") == correct_san.lower().replace("+", "").replace("#", ""):

        index += 1
        puzzle_state["move_index"] = index

        if index >= len(moves):
            del active_puzzles[user_id]
            #stat
            stats = load_stats()
            uid = str(user_id)

            stats = {**stats, **load_stats()}
            

            if uid not in stats:
                stats[uid] = {"solved":0, "best":0}

            stats[uid]["solved"] += 1

            if puzzle_state["rating"] > stats[uid]["best"]:
                stats[uid]["best"] = puzzle_state["rating"]

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