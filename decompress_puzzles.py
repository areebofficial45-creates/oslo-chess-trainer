import zstandard as zstd

input_file = "lichess_db_puzzle.csv.zst"
output_file = "lichess_puzzles.csv"

with open(input_file, "rb") as compressed:
    dctx = zstd.ZstdDecompressor()
    with open(output_file, "wb") as destination:
        dctx.copy_stream(compressed, destination)

print("Decompression complete.")