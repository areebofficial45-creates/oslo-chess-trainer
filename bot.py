import discord
from discord.ext import commands
from puzzles import start_puzzle, get_random_puzzle
from puzzles import check_move
from puzzles import active_puzzles
import chess
import chess.svg # visual board
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM
import json
from puzzles import start_puzzle, get_random_puzzle, check_move, active_puzzles, load_stats
import os
os.environ["PATH"] += os.pathsep + os.path.join(os.getcwd(), "cairo/bin")


TOKEN = os.getenv("DISCORD_TOKEN")


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
@bot.event
async def on_message(message):

    if message.author.bot:
        return

    user_id = message.author.id

    if user_id in active_puzzles and message.content.startswith("-"):

        move = message.content[1:].strip()

        result = check_move(user_id, move)

        await message.channel.send(result)

    await bot.process_commands(message)   

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏓")

#this offers solution for the hard ahh puzzles

@bot.command()
async def puzzle(ctx, level="medium"):
    if ctx.author.id in active_puzzles:
        await ctx.send("Finish your current puzzle first.")
        return

    if level not in ["easy", "medium", "hard", "insane"]:
        await ctx.send("Choose difficulty: easy / medium / hard / insane")
        return

    puzzle = get_random_puzzle(level)

    blunder = start_puzzle(ctx.author.id, puzzle)
    first_move = chess.Move.from_uci(puzzle["moves"][0])

    fen = active_puzzles[ctx.author.id]["fen"]
    rating = puzzle["rating"]
    themes = puzzle["themes"][-1]

    side_to_move = "White" if " w " in fen else "Black"

    board = chess.Board(fen)

    svg_board = chess.svg.board(
    board=board,
    flipped=not board.turn,
    lastmove= first_move
)

    with open("board.svg", "w") as f:
        f.write(svg_board)

    drawing = svg2rlg("board.svg")
    renderPM.drawToFile(drawing, "board.png", fmt="PNG")

    message = (
    f"◼️Puzzle Rating: {rating}\n"
    f"◻️Theme: {themes}\n"
    f"◼️Opponent plays {blunder}\n"
    f"◻️{side_to_move} to move\n"
    f"◼️Your move?"
)
    await ctx.send(message)
    await ctx.send(file=discord.File("board.png"))
@bot.command()
async def solution(ctx):

    user_id = ctx.author.id

    if user_id not in active_puzzles:
        await ctx.send("No active puzzle.")
        return

    puzzle_state = active_puzzles[user_id]

    moves = puzzle_state["moves"]

    board = chess.Board(puzzle_state["initial_fen"])

    san_moves = []

    for move in moves:
        chess_move = chess.Move.from_uci(move)

        try:
            san = board.san(chess_move)
        except:
            await ctx.send("Puzzle solution could not be reconstructed.")
            return

        san_moves.append(san)
        board.push(chess_move)

    solution_text = " ".join(san_moves)

    await ctx.send(f"✅Solution:\n{solution_text}")

#stat
@bot.command()
async def profile(ctx):

    stats = load_stats()
    uid = str(ctx.author.id)

    if uid not in stats:
        await ctx.send("No puzzles solved yet.")
        return

    solved = stats[uid]["solved"]
    best = stats[uid]["best"]

    msg = (
        f"{ctx.author.display_name}'s Puzzle Profile\n"
        f"✅Puzzles solved: {solved}\n"
        f"🧩Best puzzle rating: {best}"
    )

    await ctx.send(msg)    
#leaderboard


@bot.command()
async def leaderboard(ctx):

    stats = load_stats()

    if not stats:
        await ctx.send("No puzzle stats yet.")
        return

    top = sorted(stats.items(), key=lambda x: x[1]["solved"], reverse=True)[:10]

    msg = "🏆 Puzzle Leaderboard\n"

    for i, (uid, data) in enumerate(top, 1):
        user = await bot.fetch_user(int(uid))
        msg += f"{i}. {user.name} — {data['solved']} puzzles\n"

    await ctx.send(msg)

# a guide
@bot.command()
async def guide(ctx):

    embed = discord.Embed(
        title="♟️ Oslo Chess Trainer",
        description="Your interactive Discord puzzle trainer.",
        color=0x3498db
    )

    embed.add_field(
        name="🧩 Puzzle Commands",
        value=(
            "`!puzzle [easy | medium | hard | insane]`\n"
            "Start a puzzle\n\n"
            "`!solution`\n"
            "Reveal the solution"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Stats",
        value=(
            "`!profile` – view your stats\n"
            "`!leaderboard` – see top solvers"
        ),
        inline=False
    )

    embed.add_field(
        name="📖 Guides",
        value="`!notation` – learn chess notation",
        inline=False
    )

    embed.set_footer(text="Solve puzzles, improve tactics, climb the leaderboard ♟️")

    await ctx.send(embed=embed)

#notations
@bot.command()
async def notation(ctx):

    embed = discord.Embed(
        title="♟️ Chess Notation Guide",
        description="How to write chess moves for Oslo puzzles",
        color=0xf1c40f
    )

    embed.add_field(
        name="♜ Piece Letters",
        value="K = King\nQ = Queen\nR = Rook\nB = Bishop\nN = Knight",
        inline=False
    )

    embed.add_field(
        name="⚔️ Symbols",
        value="x = capture\n+ = check\n# = checkmate",
        inline=False
    )

    embed.add_field(
        name="🏰 Castling",
        value="O-O = king side\nO-O-O = queen side",
        inline=False
    )

    embed.add_field(
        name="✏️ Examples",
        value="`-Nf3`\n`-Qxh7+`\n`-Rxd8#`",
        inline=False
    )

    embed.set_footer(text="Always start puzzle moves with '-'")

    await ctx.send(embed=embed)

@bot.command()
async def creator(ctx):
    await ctx.send(
        "🌸 I am **Oslo**, your cozy Discord chess trainer.\n"
        "Created with ♟️ and ☕ by **Night Wing** for passionate chess players."
    )    




bot.run(TOKEN)
