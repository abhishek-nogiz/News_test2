from pathlib import Path

# Folders to merge
ROOT_DIRS = [
    "app",
    "news_agent", 
    "scripts",
]

OUTPUT_FILE = "combined_codebase.txt"

IGNORE_DIRS = {
    "__pycache__",
    "backup",
    ".git",
    ".venv",
    "venv",
    "node_modules",
}

INCLUDE_EXTENSIONS = {
    ".py"
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as out:

    for root in ROOT_DIRS:

        for file_path in sorted(Path(root).rglob("*")):

            if not file_path.is_file():
                continue

            if file_path.suffix not in INCLUDE_EXTENSIONS:
                continue

            if any(part in IGNORE_DIRS for part in file_path.parts):
                continue

            relative_path = file_path.as_posix()

            out.write("\n")
            out.write("=" * 100 + "\n")
            out.write(f"FILE: {relative_path}\n")
            out.write("=" * 100 + "\n\n")

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    out.write(f.read())
            except Exception as e:
                out.write(f"ERROR READING FILE: {e}")

            out.write("\n\n")

print(f"Done! Output written to {OUTPUT_FILE}")