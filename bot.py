# Copyright (c) 2026 Night Wing. All Rights Reserved.
import json
import discord
from discord.ext import commands
import chess
import chess.svg
import cairosvg
import asyncio
import os
import io

from puzzles import (
    start_puzzle, get_random_puzzle, check_move, active_puzzles,
    finish_puzzle, forfeit_puzzle, get_hint, get_user_stats,
    get_server_leaderboard, get_global_leaderboard,
    get_bot_stats, record_interaction, record_guild,
    import_legacy_to_guild, export_all_data, get_puzzle_source, format_display_name,
    get_locked_channel, set_locked_channel,
    SUPPORTED_THEMES, THEME_ALIASES
)

# ── Token ─────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

LEVELS = ["easy", "medium", "hard", "insane", "master"]

# { user_id: {"header": msg, "board": msg, "status": msg} }
puzzle_messages: dict = {}

# { user_id: int } wrong move counter, reset each puzzle
wrong_attempts: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────────────────────────────────────

# Admin ID loaded from environment variable — never hardcoded in source
_admin_id_str = os.getenv("ADMIN_ID", "")
ADMIN_ID      = int(_admin_id_str) if _admin_id_str.isdigit() else None
bot_sleeping = False                 # True = bot ignores all non-admin input

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID

def admin_only():
    """Command check decorator — silently ignores if not admin."""
    async def predicate(ctx):
        return is_admin(ctx.author.id)
    return commands.check(predicate)



# ─────────────────────────────────────────────────────────────────────────────
# BOARD RENDERING
# ─────────────────────────────────────────────────────────────────────────────

async def render_board(fen: str, last_move_uci: str = None,
                       flipped: bool = None) -> discord.File:
    board     = chess.Board(fen)
    last_move = None
    if last_move_uci:
        try:
            last_move = chess.Move.from_uci(last_move_uci)
        except Exception:
            last_move = None
    if flipped is None:
        flipped = (board.turn == chess.BLACK)
    svg_data  = chess.svg.board(
        board    = board,
        flipped  = flipped,
        lastmove = last_move,
        size     = 560,
    )
    loop      = asyncio.get_running_loop()
    png_bytes = await loop.run_in_executor(
        None, lambda: cairosvg.svg2png(bytestring=svg_data.encode("utf-8"))
    )
    return discord.File(io.BytesIO(png_bytes), filename="board.png")
# ─────────────────────────────────────────────────────────────────────────────
# PUZZLE HEADER  (plain text above board, exactly like original Oslo)
# ─────────────────────────────────────────────────────────────────────────────

def build_header(rating: int, themes: list, blunder: str, fen: str,
                 display_theme: str = None) -> str:
    # Use precomputed display_theme from JSON if available (faster),
    # otherwise fall back to last theme in list
    theme_str = display_theme or (themes[-1] if themes else "tactic")
    board_obj = chess.Board(fen)
    side      = "White \u265a" if board_obj.turn == chess.WHITE else "Black \u265a"
    return (
        "\u25fc\ufe0f **Puzzle Rating:** " + str(rating) + "\n"
        + "\u25fb\ufe0f **Theme:** " + theme_str + "\n"
        + "\u25fc\ufe0f **Opponent plays:** " + blunder + "\n"
        + "\u25fb\ufe0f **" + side + " to move** \u2014 Your move?"
    )


# ─────────────────────────────────────────────────────────────────────────────
# STATUS EMBED  (tiny embed below board, buttons + one footer line only)
# ─────────────────────────────────────────────────────────────────────────────

def build_status_embed(
    status: str = None,
    color: discord.Color = None
) -> discord.Embed:
    # Minimal shell — only holds the buttons.
    # No footer in normal flow; status only shown for terminal states.
    embed = discord.Embed(
        description = status or "​",
        color       = color or discord.Color.from_rgb(200, 200, 200)
    )
    return embed

# ─────────────────────────────────────────────────────────────────────────────
# SCORE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def score_display(rating: int, hint_used: bool,
                  wrong_moves: int = 0, clean: bool = False) -> int:
    """Mirror of calculate_score in puzzles.py — for chat display only."""
    if   rating < 1000: base = 10
    elif rating < 1500: base = 20
    elif rating < 2000: base = 30
    elif rating < 2400: base = 50
    elif rating < 2600: base = 60
    else:               base = 75
    if hint_used:
        base = int(base * 0.7)
    base -= wrong_moves * 3
    if clean:
        base += 5
    return max(0, base)


# ─────────────────────────────────────────────────────────────────────────────
# SEND PUZZLE  (used by !puzzle and resume feature)
# ─────────────────────────────────────────────────────────────────────────────

async def send_puzzle(channel, user_id: int, state: dict,
                      last_uci: str = None, blunder: str = "") -> dict:
    """
    Sends the 3-part puzzle UI:
      1. Plain text header  (puzzle info, like original Oslo)
      2. Full-size board PNG
      3. Tiny status embed + buttons
    Returns dict stored in puzzle_messages[user_id].
    """
    fen        = state["fen"]
    file       = await render_board(fen, last_uci)
    header_txt = build_header(
        state["rating"], state["themes"], blunder, fen,
        display_theme=state.get("display_theme")
    )
    view       = PuzzleView(user_id)
    header_msg = await channel.send(header_txt)
    board_msg  = await channel.send(file=file)
    status_msg = await channel.send(embed=build_status_embed(), view=view)
    return {"header": header_msg, "board": board_msg, "status": status_msg}


# ─────────────────────────────────────────────────────────────────────────────
# SOLVE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_solve(user_id: int, state: dict, msgs: dict,
                       channel, reply_to=None) -> None:
    """Updates UI and posts solve summary.
    reply_to: if a discord.Message, the summary replies to it (threads to
    the user's final move). Falls back to a plain channel.send otherwise."""
    wrongs   = wrong_attempts.pop(user_id, 0)
    h_used   = state.get("hint_used", False)
    rating   = state["rating"]
    moves    = state["moves"]
    last_uci = moves[-1] if moves else None

    # Reconstruct final FEN by replaying all moves.
    # We also capture the perspective (whose turn it was) BEFORE the last
    # move so the board stays oriented the same way the user was solving it.
    final_board = chess.Board(state["initial_fen"])
    perspective = chess.WHITE  # default
    for idx, uci in enumerate(moves):
        try:
            if idx == len(moves) - 1:
                perspective = final_board.turn  # capture before pushing last move
            final_board.push(chess.Move.from_uci(uci))
        except Exception:
            break
    fen = final_board.fen()

    guild_id = str(channel.guild.id) if channel.guild else None
    await finish_puzzle(user_id, wrong_moves=wrongs, guild_id=guild_id)
    puzzle_messages.pop(user_id, None)

    # Update board to final position with last move highlighted
    board_msg = msgs.get("board")
    if board_msg and fen:
        try:
            file = await render_board(fen, last_uci, flipped=(perspective == chess.BLACK))
            await board_msg.edit(attachments=[file])
        except Exception as e:
            print(f"[Oslo] board edit failed: {e}")

    # Disable buttons, mark green
    status_msg = msgs.get("status")
    if status_msg:
        view = PuzzleView(user_id)
        for child in view.children:
            child.disabled = True
        await status_msg.edit(
            embed=build_status_embed("\u2705 Puzzle solved!", discord.Color.green()),
            view=view
        )


    # Solve summary — clean, no ping, no "Well done"
    clean      = (not h_used) and (wrongs == 0)
    pts        = score_display(rating, h_used, wrongs, clean)
    wrong_line = (
        "❌ " + str(wrongs) + " mistake" + ("s" if wrongs != 1 else "")
        if wrongs else "✅ 0 mistakes"
    )
    hint_line  = "💡 Hint used (−30%)" if h_used else "💡 No hints"
    pts_line   = "⭐ +" + str(pts) + " pts"
    bonus_line = "\n✅ +5 clean solve bonus" if clean else ""
    summary = (
        "✅ **Puzzle solved!**\n"
        + wrong_line + "  •  " + hint_line + "  •  " + pts_line + bonus_line
    )
    # Reply to the user's final move message if available — no extra ping,
    # threads cleanly to their last move in chat
    try:
        if reply_to is not None:
            await reply_to.reply(summary, mention_author=False)
        else:
            await channel.send(summary)
    except Exception as e:
        print(f"[Oslo] summary send failed: {e}")
        try:
            await channel.send(summary)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# RESIGN HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def do_resign(user_id: int, channel) -> None:
    """Pops session, posts solution. Resets streak via forfeit_puzzle."""
    if user_id not in active_puzzles:
        await channel.send("No active puzzle to resign.")
        return
    # Save state BEFORE forfeit_puzzle pops the session
    state = active_puzzles.get(user_id, {})
    await forfeit_puzzle(user_id)   # pops session, resets streak, 0 pts
    wrong_attempts.pop(user_id, None)
    board     = chess.Board(state["initial_fen"])
    san_moves = []
    for uci in state["moves"]:
        try:
            san_moves.append(board.san(chess.Move.from_uci(uci)))
            board.push(chess.Move.from_uci(uci))
        except Exception:
            break
    solution = " \u2192 ".join(san_moves)
    await channel.send("\U0001f3f3\ufe0f You resigned.\n**Solution:** " + solution)


# ─────────────────────────────────────────────────────────────────────────────
# PUZZLE VIEW  (buttons)
# ─────────────────────────────────────────────────────────────────────────────

class PuzzleView(discord.ui.View):

    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "These buttons aren't yours!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="\U0001f4a1 Hint", style=discord.ButtonStyle.secondary)
    async def hint_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await record_interaction()
        hint = get_hint(self.user_id)
        if hint is None:
            await interaction.response.send_message("No active puzzle.", ephemeral=True)
            return
        # Respond to interaction FIRST — Discord requires response within 3s
        await interaction.response.send_message(
            "\U0001f4a1 " + hint + "\nPenalty: -30%",
            ephemeral=True
        )
        # Then update status embed (non-critical, can fail silently)
        msgs       = puzzle_messages.get(self.user_id, {})
        status_msg = msgs.get("status")
        if status_msg:
            try:
                await status_msg.edit(
                    embed=build_status_embed("\U0001f4a1 " + hint + "  (Penalty: -30%)"),
                    view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="\U0001f3f3\ufe0f Resign", style=discord.ButtonStyle.danger)
    async def resign_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        await record_interaction()
        user_id = self.user_id
        if user_id not in active_puzzles:
            await interaction.response.send_message("No active puzzle.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        msgs       = puzzle_messages.pop(user_id, None) or {}
        status_msg = msgs.get("status")
        if status_msg:
            await status_msg.edit(
                embed=build_status_embed("\U0001f3f3\ufe0f Resigned.", discord.Color.red()),
                view=self
            )
        await do_resign(user_id, interaction.channel)
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="\U0001f4dc Moves", style=discord.ButtonStyle.primary)
    async def moves_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        await record_interaction()
        user_id = self.user_id
        if user_id not in active_puzzles:
            await interaction.response.send_message("No active puzzle.", ephemeral=True)
            return
        state     = active_puzzles[user_id]
        board     = chess.Board(state["initial_fen"])
        san_trail = []
        for i, uci in enumerate(state["moves"][:state["move_index"]]):
            try:
                san   = board.san(chess.Move.from_uci(uci))
                label = "Opponent" if i % 2 == 0 else "You"
                san_trail.append("`" + str(i + 1) + ".` **" + label + ":** " + san)
                board.push(chess.Move.from_uci(uci))
            except Exception:
                break
        if not san_trail:
            await interaction.response.send_message("No moves yet.", ephemeral=True)
            return
        trail = "\n".join(san_trail)
        await interaction.response.send_message(
            "**\U0001f4dc Move trail:**\n" + trail, ephemeral=True
        )


# ─────────────────────────────────────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def setup_hook():
    """Called before bot connects — perfect place for async DB init."""
    from puzzles import init_db
    await init_db()


@bot.event
async def on_ready():
    print("[Oslo] Logged in as " + str(bot.user) + " | Servers: " + str(len(bot.guilds)))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    user_id = message.author.id

    # Sleep guard: ignore all non-admin input when bot is sleeping
    # Admin commands (!wake, !sleep, !adminstatus) still work
    if bot_sleeping and not is_admin(user_id):
        admin_cmds = ['!wake', '!sleep', '!adminstatus']
        if not any(message.content.strip().lower().startswith(c) for c in admin_cmds):
            return

    # Move input: starts with "-", user has active puzzle
    # Change 2: NO deletion. Chat stays natural like original Oslo.
    if user_id in active_puzzles and message.content.startswith("-"):
        await record_interaction()
        move             = message.content[1:].strip()
        result, complete = check_move(user_id, move)

        # Re-read state AFTER check_move so FEN reflects the updated position
        state = active_puzzles.get(user_id)
        msgs  = puzzle_messages.get(user_id, {})

        if complete:
            if state is None:
                # Puzzle data error already cleared session — ignore
                return
            await handle_solve(user_id, state, msgs, message.channel,
                               reply_to=message)
        else:
            if "Incorrect" in result:
                wrong_attempts[user_id] = wrong_attempts.get(user_id, 0) + 1
                await message.reply("\u274c Incorrect move. Try again.",
                                    mention_author=False)
            else:
                # state["fen"] is now the UPDATED position after opponent response
                fen        = state["fen"] if state else None
                board_msg  = msgs.get("board")
                status_msg = msgs.get("status")
                if board_msg and fen and state:
                    idx      = state["move_index"] - 1
                    last_uci = state["moves"][idx] if idx >= 0 else None
                    file = await render_board(fen, last_uci)
                    await board_msg.edit(attachments=[file])
                if status_msg and fen:
                    view = PuzzleView(user_id)
                    await status_msg.edit(embed=build_status_embed(result), view=view)
                await message.reply(result, mention_author=False)

    await bot.process_commands(message)


# ─────────────────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

@bot.command()
async def ping(ctx):
    await ctx.send("\U0001f3d3 Pong! `" + str(round(bot.latency * 1000)) + "ms`")


# ── Channel lock guard ────────────────────────────────────────────────────────

async def is_allowed_channel(ctx) -> bool:
    """Return True if the command is allowed in this channel."""
    if not ctx.guild:
        return True  # DMs always allowed
    locked = await get_locked_channel(str(ctx.guild.id))
    if locked is None:
        return True  # No lock set — all channels allowed
    return str(ctx.channel.id) == locked


@bot.command()
async def lockbotchannel(ctx):
    """Lock Oslo to the current channel only. Requires Manage Channels permission."""
    if not is_admin(ctx.author.id) and not ctx.author.guild_permissions.manage_channels:
        await ctx.send("❌ You need Manage Channels permission to use this command.")
        return
    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await set_locked_channel(str(ctx.guild.id), str(ctx.channel.id))
    await ctx.send(
        f"🔒 Oslo is now locked to <#{ctx.channel.id}>.\n"
        f"Puzzle commands will only work in this channel.\n"
        f"Use `!unlockbotchannel` to allow Oslo everywhere again."
    )


@bot.command()
async def unlockbotchannel(ctx):
    """Unlock Oslo so it works in all channels."""
    if not is_admin(ctx.author.id) and not ctx.author.guild_permissions.manage_channels:
        await ctx.send("❌ You need Manage Channels permission to use this command.")
        return
    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await set_locked_channel(str(ctx.guild.id), None)
    await ctx.send("🔓 Oslo is now unlocked and works in all channels.")


@bot.command()
async def botlockstatus(ctx):
    """Show which channel Oslo is locked to (if any)."""
    if not ctx.guild:
        await ctx.send("❌ This command can only be used in a server.")
        return
    locked = await get_locked_channel(str(ctx.guild.id))
    if locked:
        await ctx.send(f"🔒 Oslo is locked to <#{locked}>.")
    else:
        await ctx.send("🔓 Oslo is unlocked and works in all channels.")


@bot.command()
async def puzzle(ctx, arg1: str = "medium", arg2: str = None):
    """
    Usage:
      !puzzle                    -> medium, any theme
      !puzzle easy               -> easy, any theme
      !puzzle hard sacrifice     -> hard, sacrifice theme
      !puzzle sacrifice          -> medium, sacrifice theme
      !puzzle easy mateIn1       -> easy, mateIn1 theme
    """
    await record_interaction()
    if not await is_allowed_channel(ctx):
        return
    user_id = ctx.author.id

    # Parse level and theme from args (can be in either order)
    level = "medium"
    theme = None
    for arg in [arg1, arg2]:
        if arg is None:
            continue
        if arg.lower() in LEVELS:
            level = arg.lower()
        elif arg.lower() in {t.lower() for t in SUPPORTED_THEMES}:
            # Use canonical casing from SUPPORTED_THEMES
            theme = next(t for t in SUPPORTED_THEMES if t.lower() == arg.lower())
        else:
            await ctx.send(
                "Invalid argument: `" + arg + "`\n"
                "Levels: `easy` `medium` `hard` `insane` `master`\n"
                "Themes: `sacrifice` `endgame` `middlegame` `opening` "
                "`fork` `pin` `mate` `mateIn1` `mateIn2` `mateIn4` `longMate` `promotion`"
            )
            return

    # Addition 1: active puzzle exists, remind AND re-send from current position
    if user_id in active_puzzles:
        state      = active_puzzles[user_id]
        moves      = state["moves"]
        move_index = state["move_index"]
        last_uci   = moves[move_index - 1] if move_index > 0 else None
        try:
            init_board  = chess.Board(state["initial_fen"])
            blunder_san = init_board.san(chess.Move.from_uci(moves[0]))
        except Exception:
            blunder_san = moves[0] if moves else "?"
        await ctx.send(
            "\u26a0\ufe0f " + ctx.author.mention
            + " You have an unfinished puzzle! Here's where you left off:"
        )
        msgs = await send_puzzle(
            ctx.channel, user_id, state,
            last_uci=last_uci, blunder=blunder_san
        )
        puzzle_messages[user_id] = msgs
        return

    level = level.lower()
    if level not in LEVELS:
        await ctx.send("Choose a difficulty: `easy` | `medium` | `hard` | `insane` | `master`")
        return

    try:
        puzz    = await get_random_puzzle(level, theme=theme)
        # If user requested a theme and it's in this puzzle, show it
        # instead of the priority-picked display_theme
        if theme and theme.lower() in [t.lower() for t in puzz.get('themes', [])]:
            puzz['display_theme'] = theme
        blunder = start_puzzle(user_id, puzz)
    except Exception as e:
        print(f"[Oslo] puzzle start error: {e}")
        await ctx.send("Failed to load puzzle. Try again!")
        active_puzzles.pop(user_id, None)
        return
    wrong_attempts[user_id] = 0
    state   = active_puzzles[user_id]
    msgs    = await send_puzzle(
        ctx.channel, user_id, state,
        last_uci=puzz["moves"][0], blunder=blunder
    )
    puzzle_messages[user_id] = msgs


@bot.command()
async def hint(ctx):
    """!hint  —  same as the hint button."""
    await record_interaction()
    if not await is_allowed_channel(ctx):
        return
    user_id = ctx.author.id
    h = get_hint(user_id)
    if h is None:
        await ctx.send("No active puzzle.")
        return
    msgs       = puzzle_messages.get(user_id, {})
    status_msg = msgs.get("status")
    if status_msg:
        view = PuzzleView(user_id)
        await status_msg.edit(
            embed=build_status_embed("\U0001f4a1 " + h + "  (\u221230% score penalty)"),
            view=view
        )
    await ctx.send("\U0001f4a1 " + h + "\n*\u221230% score penalty applied*")


@bot.command(name="move")
async def move_cmd(ctx, *, notation: str):
    """!move Nf3  —  alternative to typing  -Nf3"""
    await record_interaction()
    user_id = ctx.author.id
    if user_id not in active_puzzles:
        await ctx.send("No active puzzle. Start one with `!puzzle`.")
        return
    result, complete = check_move(user_id, notation.strip())
    # Re-read state after check_move — FEN is now updated
    state = active_puzzles.get(user_id)
    msgs  = puzzle_messages.get(user_id, {})
    if complete:
        if state is None:
            return  # Puzzle data error already cleared session
        await handle_solve(user_id, state, msgs, ctx.channel, reply_to=ctx.message)
    else:
        if "Incorrect" in result:
            wrong_attempts[user_id] = wrong_attempts.get(user_id, 0) + 1
        await ctx.reply(result, mention_author=False)
        if "Incorrect" not in result and state:
            fen        = state["fen"]  # updated after opponent response
            board_msg  = msgs.get("board")
            status_msg = msgs.get("status")
            if board_msg and fen:
                idx      = state["move_index"] - 1
                last_uci = state["moves"][idx] if idx >= 0 else None
                file = await render_board(fen, last_uci)
                await board_msg.edit(attachments=[file])
            if status_msg:
                view = PuzzleView(user_id)
                await status_msg.edit(embed=build_status_embed(result), view=view)


@bot.command()
async def resign(ctx):
    """!resign  —  give up and see the full solution."""
    await record_interaction()
    if not await is_allowed_channel(ctx):
        return
    user_id = ctx.author.id
    if user_id not in active_puzzles:
        await ctx.send("No active puzzle to resign.")
        return
    msgs       = puzzle_messages.pop(user_id, None) or {}
    status_msg = msgs.get("status")
    if status_msg:
        view = PuzzleView(user_id)
        for child in view.children:
            child.disabled = True
        await status_msg.edit(
            embed=build_status_embed("\U0001f3f3\ufe0f Resigned.", discord.Color.red()),
            view=view
        )
    await do_resign(user_id, ctx.channel)


@bot.command()
async def solution(ctx):
    """!solution  —  reveals full answer, ends puzzle with 0 pts."""
    await record_interaction()
    user_id = ctx.author.id
    if user_id not in active_puzzles:
        await ctx.send("No active puzzle.")
        return

    state     = active_puzzles[user_id]
    board     = chess.Board(state["initial_fen"])
    san_moves = []
    for uci in state["moves"]:
        try:
            san_moves.append(board.san(chess.Move.from_uci(uci)))
            board.push(chess.Move.from_uci(uci))
        except Exception:
            await ctx.send("Could not reconstruct solution.")
            return

    # End the puzzle with 0 pts — peeking the solution is a forfeit
    await forfeit_puzzle(user_id)   # resets streak, 0 pts
    active_puzzles.pop(user_id, None)   # forfeit already pops, safety net
    wrong_attempts.pop(user_id, None)
    msgs       = puzzle_messages.pop(user_id, None) or {}
    status_msg = msgs.get("status")
    if status_msg:
        view = PuzzleView(user_id)
        for child in view.children:
            child.disabled = True
        await status_msg.edit(
            embed=build_status_embed("\U0001f4d6 Solution revealed — 0 pts.",
                                     discord.Color.orange()),
            view=view
        )

    await ctx.send(
        "\U0001f4d6 **Solution:** " + " \u2192 ".join(san_moves) + "\n"
        "*Puzzle ended — 0 pts awarded for revealing the solution.*"
    )


@bot.command()
async def profile(ctx):
    await record_interaction()
    stats = await get_user_stats(str(ctx.author.id))
    if not stats:
        await ctx.send("No puzzles solved yet. Try `!puzzle`!")
        return
    display = format_display_name(ctx.author.display_name, ctx.author.id)
    embed = discord.Embed(
        title = display + "'s Puzzle Profile",
        color = discord.Color.from_rgb(200, 200, 200)
    )
    embed.add_field(name="✅ Puzzles Solved",    value=str(stats["solved"]),      inline=True)
    embed.add_field(name="⭐ Score",             value=str(stats["total_score"]) + " pts", inline=True)
    embed.add_field(name="🧩 Best Rating",   value=str(stats["best_rating"]), inline=True)
    embed.add_field(name="🔥 Streak",        value=str(stats["streak"]),      inline=True)
    embed.add_field(name="📈 Best Streak",   value=str(stats["best_streak"]), inline=True)
    embed.add_field(name="💡 Hints Used",    value=str(stats["hints_used"]),  inline=True)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def leaderboard(ctx):
    """!leaderboard — server top 10 with toggle buttons for score vs puzzles."""
    await record_interaction()
    if not ctx.guild:
        await ctx.send("This command only works inside a server.")
        return
    await _send_leaderboard(ctx, scope="server", sort_by="score")


@bot.command()
async def globalboard(ctx):
    """!globalboard — global top 10 by score across all servers."""
    await record_interaction()
    await _send_leaderboard(ctx, scope="global", sort_by="score")


async def _fetch_display(user_id: int) -> str:
    """Fetch display name using internal cache first, API only if needed."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        return format_display_name(user.display_name, user.id)
    except Exception:
        return "User_" + str(user_id)[-3:]


async def _build_leaderboard_embed(rows: list, title: str,
                                    sort_by: str,
                                    scope: str = "server") -> discord.Embed:
    GREY   = discord.Color.from_rgb(200, 200, 200)
    medals = ["🥇", "🥈", "🥉"]
    embed  = discord.Embed(title=title, color=GREY)

    if not rows:
        embed.description = "*No data yet. Try `!puzzle` to get on the board.*"
        return embed

    # Fetch ALL display names concurrently - 10x faster than sequential
    displays = await asyncio.gather(
        *[_fetch_display(int(row["user_id"])) for row in rows],
        return_exceptions=True
    )

    for i, (row, display) in enumerate(zip(rows, displays), 1):
        if isinstance(display, Exception):
            display = "User_" + str(row["user_id"])[-3:]
        rank = medals[i - 1] if i <= 3 else str(i) + "."
        stat = str(row["total_score"]) + " pts" if sort_by == "score" else str(row["solved"]) + " puzzles"
        embed.add_field(name=rank + " " + display, value=stat, inline=False)

    return embed


async def _send_leaderboard(ctx, scope: str, sort_by: str):
    guild_id = str(ctx.guild.id) if ctx.guild else None

    if scope == "server" and guild_id:
        rows  = await get_server_leaderboard(guild_id, sort_by=sort_by, limit=10)
        title = "🏆 Leaderboard: " + (ctx.guild.name if ctx.guild else "Server")
    else:
        rows  = await get_global_leaderboard(sort_by=sort_by, limit=10)
        title = "🌐 Oslo Global Leaderboard"

    embed = await _build_leaderboard_embed(rows, title, sort_by, scope)
    view  = LeaderboardView(scope=scope, guild_id=guild_id, sort_by=sort_by)
    await ctx.send(embed=embed, view=view)


class LeaderboardView(discord.ui.View):
    """
    Two toggle buttons on the leaderboard embed.
    Pressing a button re-fetches and edits the embed in place.
    Global leaderboard: By Puzzles button is disabled (score only).
    Server leaderboard: both buttons active.
    """

    def __init__(self, scope: str, guild_id: str, sort_by: str):
        super().__init__(timeout=120)
        self.scope    = scope
        self.guild_id = guild_id
        self.sort_by  = sort_by
        # Visually mark the active sort button as disabled
        self.score_btn.disabled  = (sort_by == "score")
        self.solved_btn.disabled = (sort_by == "solved")

    @discord.ui.button(label="By Score \u2b50", style=discord.ButtonStyle.primary)
    async def score_btn(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        self.sort_by = "score"
        self.score_btn.disabled  = True
        self.solved_btn.disabled = (self.scope == "global")

        if self.scope == "server" and self.guild_id:
            rows  = await get_server_leaderboard(self.guild_id, sort_by="score", limit=10)
            title = "🏆 Leaderboard: " + interaction.guild.name
        else:
            rows  = await get_global_leaderboard(sort_by="score", limit=10)
            title = "🌐 Oslo Global Leaderboard"

        embed = await _build_leaderboard_embed(rows, title, "score", self.scope)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="By Puzzles \U0001f9e9", style=discord.ButtonStyle.secondary)
    async def solved_btn(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        self.sort_by = "solved"
        self.score_btn.disabled  = False
        self.solved_btn.disabled = True

        if self.scope == "server" and self.guild_id:
            rows  = await get_server_leaderboard(self.guild_id, sort_by="solved", limit=10)
            title = "🏆 Leaderboard: " + interaction.guild.name
        else:
            rows  = await get_global_leaderboard(sort_by="solved", limit=10)
            title = "🌐 Oslo Global Leaderboard"

        embed = await _build_leaderboard_embed(rows, title, "solved", self.scope)
        await interaction.edit_original_response(embed=embed, view=self)


@bot.command()
async def botstats(ctx):
    await record_interaction()
    stats = await get_bot_stats()
    embed = discord.Embed(title="\U0001f4e1 Oslo Bot Stats", color=discord.Color.from_rgb(200, 200, 200))
    embed.add_field(
        name  = "\U0001f9e9 Puzzles Solved",
        value = "`" + str(stats.get("total_puzzles_solved", 0)) + "`",
        inline=True
    )
    embed.add_field(
        name  = "\U0001f4ac Interactions",
        value = "`" + str(stats.get("total_interactions", 0)) + "`",
        inline=True
    )
    embed.add_field(
        name  = "\U0001f310 Servers",
        value = "`" + str(len(bot.guilds)) + "`",
        inline=True
    )
    embed.set_footer(text="Oslo Chess Trainer \u2022 built by Night Wing")
    await ctx.send(embed=embed)


@bot.command()
async def guide(ctx):
    await record_interaction()
    if not await is_allowed_channel(ctx):
        return
    embed = discord.Embed(
        title       = "♟️ Oslo Chess Trainer",
        color       = discord.Color.from_rgb(200, 200, 200)
    )
    embed.add_field(
        name  = "🧩 Puzzle Commands",
        value = (
            "`!puzzle [level] [theme]` — Start a puzzle\n"
            "`!hint` — Get a hint (Penalty: -30%)\n"
            "`!resign` — Give up & see solution\n"
            "`!solution` — Reveal solution (0 pts)\n"
            "`!move Nf3` — Alt to `-Nf3`"
        ),
        inline=False
    )
    embed.add_field(
        name  = "♟️ Themes",
        value = (
            "```\n"
            "sacrifice   endgame    middlegame\n"
            "opening     fork       pin\n"
            "mate        mateIn1    mateIn2\n"
            "mateIn4     longMate   promotion\n"
            "```"
        ),
        inline=False
    )
    embed.add_field(
        name  = "📊 Stats",
        value = (
            "`!profile` — view your stats\n"
            "`!leaderboard` — server top 10\n"
            "`!globalboard` — global top 10\n"
            "`!botstats` — bot activity stats"
        ),
        inline=False
    )
    embed.add_field(
        name  = "📖 Guides",
        value = "`!notation` — learn chess notation",
        inline=False
    )
    embed.set_footer(text="Oslo is a student-developed project • built with ♟️ and ☕ by Night Wing")
    await ctx.send(embed=embed)


@bot.command()
async def notation(ctx):
    await record_interaction()
    if not await is_allowed_channel(ctx):
        return
    embed = discord.Embed(
        title       = "\u265f\ufe0f Chess Notation Guide",
        description = "How to write moves for Oslo puzzles",
        color       = discord.Color.from_rgb(200, 200, 200)
    )
    embed.add_field(
        name  = "\u265c Piece Letters",
        value = "K = King\nQ = Queen\nR = Rook\nB = Bishop\nN = Knight",
        inline=False
    )
    embed.add_field(
        name  = "\u2694\ufe0f Symbols",
        value = "x = capture\n+ = check\n# = checkmate",
        inline=False
    )
    embed.add_field(
        name  = "\U0001f3f0 Castling",
        value = "O-O = kingside\nO-O-O = queenside",
        inline=False
    )
    embed.add_field(
        name  = "\u270f\ufe0f Examples",
        value = "`-Nf3`  `-Qxh7+`  `-Rxd8#`  `-O-O`",
        inline=False
    )
    embed.set_footer(text="Start every move with  -  (or use  !move Nf3)")
    await ctx.send(embed=embed)


@bot.command()
async def creator(ctx):
    await ctx.send(
        "\U0001f338 I am **Oslo**, your cozy Discord chess trainer.\n"
        "Created with \u265f\ufe0f and \u2615 by **Night Wing** for passionate chess players.\n"
        "\u2615 Help keep Oslo running: https://ko-fi.com/devnightwing"
    )


# ── Run ───────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS  (Night Wing only)
# ─────────────────────────────────────────────────────────────────────────────

@bot.command()
@admin_only()
async def sleep(ctx):
    global bot_sleeping
    if bot_sleeping:
        await ctx.send("\U0001f4a4 Oslo is already sleeping.")
        return
    bot_sleeping = True
    await ctx.send(
        "\U0001f4a4 Oslo is now **sleeping**. All user commands are paused.\n"
        "Use `!wake` to bring Oslo back online."
    )


@bot.command()
@admin_only()
async def wake(ctx):
    global bot_sleeping
    if not bot_sleeping:
        await ctx.send("\u2615 Oslo is already awake and running.")
        return
    bot_sleeping = False
    await ctx.send(
        "\u2615 Oslo is **awake**! Puzzle training resumes.\n"
        "All commands are back online."
    )


@bot.command()
@admin_only()
async def adminstatus(ctx):
    stats  = await get_bot_stats()
    status = "💤 SLEEPING" if bot_sleeping else "🟢 ONLINE"
    puzzle_info = await get_puzzle_source()
    if puzzle_info["source"] == "PostgreSQL":
        bd = puzzle_info.get("breakdown", {})
        puzzle_val = (
            f"PostgreSQL ✅\n"
            f"easy {bd.get('easy',0):,} • medium {bd.get('medium',0):,}\n"
            f"hard {bd.get('hard',0):,} • insane {bd.get('insane',0):,} • master {bd.get('master',0):,}\n"
            f"Fallback: 15,000/level (JSON)"
        )
    else:
        puzzle_val = "JSON fallback ⚠️"
    embed  = discord.Embed(
        title = "🛡️ Oslo Admin Dashboard",
        color = discord.Color.red() if bot_sleeping else discord.Color.green()
    )
    embed.add_field(name="Status",                   value=status,                                    inline=True)
    embed.add_field(name="🌐 Servers",       value=str(len(bot.guilds)),                      inline=True)
    embed.add_field(name="🧩 Active",        value=str(len(active_puzzles)),                  inline=True)
    embed.add_field(name="✅ Total Solved",      value=str(stats.get("total_puzzles_solved", 0)), inline=True)
    embed.add_field(name="💬 Interactions",  value=str(stats.get("total_interactions", 0)),   inline=True)
    embed.add_field(name="🛡 Admin",         value="<@" + str(ADMIN_ID) + ">" if ADMIN_ID else "Not set", inline=True)
    embed.add_field(name="🧩 Puzzle Source", value=puzzle_val,                                inline=False)
    embed.set_footer(text="Oslo Chess Trainer • Admin View")
    await ctx.send(embed=embed)



@bot.command()
@admin_only()
async def importlegacy(ctx):
    """Link all legacy stats.json users to this server's leaderboard."""
    if not ctx.guild:
        await ctx.send("Run this inside a server.")
        return
    guild_id = str(ctx.guild.id)
    count    = await import_legacy_to_guild(guild_id)
    await ctx.send(
        f"✅ Linked **{count}** legacy users to **{ctx.guild.name}** leaderboard.\n"
        "They will now appear in `!leaderboard` ranked by puzzles solved."
    )



@bot.command()
@admin_only()
async def exportdata(ctx):
    """Export all PostgreSQL data to JSON for safe migration to any future host."""
    await ctx.send("📦 Exporting data...")
    try:
        data = await export_all_data()
        out  = json.dumps(data, indent=2)
        import io as _io
        fobj = discord.File(_io.BytesIO(out.encode()), filename="oslo_export.json")
        await ctx.send(
            f"✅ Export complete: **{len(data['users'])} users**, "
            f"**{len(data['user_guilds'])} guild links**",
            file=fobj
        )
    except Exception as e:
        await ctx.send(f"❌ Export failed: {e}")



@bot.command()
@admin_only()
async def importlegacystats(ctx):
    """Backfill bot_stats from all user data in DB. Safe to run multiple times."""
    await ctx.send("🔄 Calculating stats from DB...")
    try:
        from puzzles import _pool
        async with _pool.acquire() as conn:
            row = await conn.fetchrow("SELECT SUM(solved) as s FROM users")
            total_solved = int(row["s"] or 0)
            total_interactions = total_solved * 3
            await conn.execute("""
                UPDATE bot_stats
                SET total_puzzles_solved = GREATEST(total_puzzles_solved, $1),
                    total_interactions   = GREATEST(total_interactions,   $2)
                WHERE id = 1
            """, total_solved, total_interactions)
        await ctx.send(
            f"✅ Bot stats updated\n"
            f"🧩 Puzzles: **{total_solved:,}**\n"
            f"💬 Interactions: **{total_interactions:,}** (estimated)"
        )
    except Exception as e:
        await ctx.send(f"❌ Failed: {e}")



@bot.command()
@admin_only()
async def leaveserver(ctx, server_id: str = None):
    """Leave a specific server by ID. Run !leaveserver alone to list all servers."""
    if not server_id:
        lines = [
            f"`{g.id}` — {g.name} ({g.member_count} members)"
            for g in bot.guilds
        ]
        await ctx.send("**Oslo is in these servers:**\n" + "\n".join(lines))
        return
    try:
        guild = bot.get_guild(int(server_id))
        if guild is None:
            await ctx.send(f"❌ Server `{server_id}` not found.")
            return
        name = guild.name
        await guild.leave()
        await ctx.send(f"✅ Oslo has left **{name}** (`{server_id}`).")
    except ValueError:
        await ctx.send("❌ Invalid server ID — must be a number.")
    except Exception as e:
        await ctx.send(f"❌ Failed: {e}")

if not TOKEN:
    print('[Oslo] ERROR: DISCORD_TOKEN is not set!')
else:
    print('[Oslo] Token found, starting bot...')
    try:
        bot.run(TOKEN)
    except Exception as e:
        import traceback
        print('[Oslo] FATAL ERROR:')
        traceback.print_exc()
