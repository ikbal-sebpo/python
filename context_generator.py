import os
from typing import List, Set

# Standard Lib
import os

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
OUTPUT_FILE: str = "project_context.txt"
# Folders to strictly ignore
IGNORE_DIRS: Set[str] = {
    "__pycache__", "migrations", ".git", ".idea", "venv", "env", "static", "media", "node_modules"
}
# File extensions to include (Python, HTML templates, maybe JS)
INCLUDE_EXT: Set[str] = {".py", ".html"}

def is_ignored(path: str) -> bool:
    """
    Check if a path contains ignored directories.
    """
    parts: List[str] = path.split(os.sep)
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    return False

def generate_context(root_dir: str) -> None:
    """
    Walks the directory tree and compiles code into a single text file.
    """
    with open(OUTPUT_FILE, "w", encoding="utf-8") as outfile:
        # Write a project tree structure first for visual context
        outfile.write("PROJECT STRUCTURE:\n")
        outfile.write("==================\n")
        
        for root, dirs, files in os.walk(root_dir):
            # Modify dirs in-place to skip ignored directories during walk
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            
            level = root.replace(root_dir, '').count(os.sep)
            indent = ' ' * 4 * level
            outfile.write(f"{indent}{os.path.basename(root)}/\n")
            subindent = ' ' * 4 * (level + 1)
            for f in files:
                if any(f.endswith(ext) for ext in INCLUDE_EXT):
                    outfile.write(f"{subindent}{f}\n")
        
        outfile.write("\n\nFILE CONTENTS:\n")
        outfile.write("==================\n")

        # Write actual file contents
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            
            for file in files:
                if any(file.endswith(ext) for ext in INCLUDE_EXT):
                    file_path: str = os.path.join(root, file)
                    
                    # Skip the script itself or the output file
                    if file == "context_generator.py" or file == OUTPUT_FILE:
                        continue

                    outfile.write(f"\n# ==========================================\n")
                    outfile.write(f"# FILE: {file_path}\n")
                    outfile.write(f"# ==========================================\n")
                    
                    try:
                        with open(file_path, "r", encoding="utf-8") as infile:
                            content = infile.read()
                            outfile.write(content + "\n")
                    except Exception as e:
                        outfile.write(f"# Error reading file: {e}\n")

    print(f"Context generated successfully in: {OUTPUT_FILE}")

if __name__ == "__main__":
    generate_context(".")