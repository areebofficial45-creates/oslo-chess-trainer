# ♟️ Oslo

## Interactive Discord Chess Puzzle Trainer

**Oslo** is a free Discord bot that transforms any server into an interactive chess puzzle training environment.

The project was inspired by a chess community where players regularly shared positions and solved puzzles together in a Discord chess channel. Without premium puzzle tools on major chess platforms, this process was often slow and inefficient.

Oslo brings fast, collaborative chess training directly into Discord, allowing players to instantly launch puzzles and solve them interactively through chat using puzzles derived from a massive dataset of real games.

**Free & Open Source • Python Discord Bot • Chess Puzzle Trainer**

---

# 🚀 Features

- ♟️ Launch instant chess puzzles directly inside Discord  
- 🧠 Solve tactics through interactive back-and-forth gameplay  
- 🖼️ View automatically rendered chess boards for every puzzle  
- 📊 Train using skill-based difficulty tiers for all player levels  
- 📚 Access puzzles derived from real chess games  
- 🏆 Track progress with personal puzzle statistics  
- 📈 Compete with friends through a server leaderboard  
- 👥 Support multiple users solving puzzles simultaneously  
- ⚡ Lightweight, efficient, and completely free to use  

---

# 🎯 Puzzle Difficulty Levels

Puzzles are grouped into rating pools so players of all strengths can train effectively.

| Difficulty | Rating Range |
|------------|-------------|
| Easy | 100 – 500 |
| Medium | 500 – 1000 |
| Hard | 1000 – 1500 |
| Insane | 1500 – 2000 |

Each difficulty pool is generated from filtered puzzle datasets to ensure appropriate tactical complexity for different player levels.

---

# 📚 Puzzle Source

All puzzles originate from the **Lichess Puzzle Database**, one of the largest publicly available collections of chess tactics.

Dataset source:  
https://database.lichess.org/

Custom preprocessing scripts were used to:

- decompress the original dataset  
- filter puzzles by rating ranges  
- extract puzzle themes and metadata  
- generate optimized puzzle pools for each difficulty tier  

The final puzzle pools are stored as lightweight JSON files used directly by the bot.

---

# ♟️ Example Puzzle Flow

```
!puzzle medium

Puzzle Rating: 1280
Theme: fork, middlegame

Opponent plays Qe7
White to move
Your move?
```

Users respond using chess notation:

```
-Nf3
-Qxd5
-O-O
```

The bot validates moves, updates the board, and continues the puzzle until the tactic is solved.

---

# 💬 Commands

### Start a puzzle

```
!puzzle [easy | medium | hard | insane]
```

### Reveal the solution

```
!solution
```

### View your puzzle statistics

```
!profile
```

### View the server leaderboard

```
!leaderboard
```

### Help and guides

```
!help
!notation
!creator
```

Moves must be entered using a dash.

Example:

```
-Nf3
-Qxd5
```

---

# 🛠 Technology

Oslo was built using:

- Python  
- discord.py  
- python-chess  
- svglib  
- reportlab (for board rendering)

The architecture is designed to support multiple simultaneous puzzle sessions, enabling many users in the same server to train independently.

---

# 👨‍💻 Author

Made with ♟️ and ☕ by  

**Areeb Jamali**  
Alias: **Night Wing**

Creator of **Oslo — Discord Chess Trainer**

---

# 📜 License

This project is licensed under the **MIT License**.