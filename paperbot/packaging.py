"""Bundle PaperBot for a colleague who also has institute access and wants to run their own:
a zip of the program only - never your settings, passwords, database or downloaded PDFs."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .config import home_dir

INCLUDE = ["pyproject.toml", "README.md", "LICENSE", "config.example.toml",
           "Install PaperBot.cmd", ".gitignore"]
INCLUDE_DIRS = ["paperbot", "scripts", "tests"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", "data", ".venv"}
SKIP_SUFFIX = {".pyc", ".pyo"}
SKIP_NAMES = {"config.toml", "paperbot.lock"}


def bundle_files(root: Path | None = None) -> list[Path]:
    root = root or home_dir()
    files = [root / name for name in INCLUDE if (root / name).exists()]
    for d in INCLUDE_DIRS:
        for p in sorted((root / d).rglob("*")):
            rel = p.relative_to(root)
            if (p.is_file() and not SKIP_PARTS.intersection(rel.parts)
                    and p.suffix not in SKIP_SUFFIX and p.name not in SKIP_NAMES
                    and not any(part.endswith(".egg-info") for part in rel.parts)):
                files.append(p)
    return files


def make_zip(dest: Path, root: Path | None = None) -> Path:
    root = root or home_dir()
    dest = Path(dest)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in bundle_files(root):
            z.write(p, Path("PaperBot") / p.relative_to(root))
    return dest
