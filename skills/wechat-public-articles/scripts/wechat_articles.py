"""Compatibility entry point for the installed Codex skill."""
from pathlib import Path
import sys


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1]
    locator = skill_root / "project-root.txt"
    project = (Path(locator.read_text(encoding="utf-8-sig").strip()) if locator.is_file()
               else skill_root.parents[1])
    if not (project / "src" / "article_reader.py").is_file():
        print('Project reader not found; run scripts/install_skill.ps1 from the project.', file=sys.stderr)
        return 2
    sys.path.insert(0, str(project))
    from src.article_reader import main as read_articles
    return read_articles()


if __name__ == "__main__":
    raise SystemExit(main())
