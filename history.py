from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent / "history.log"


def append_history(report: dict[str, Any], formatted_report: str, path: Path | None = None) -> None:
    history_path = path or DEFAULT_HISTORY_PATH
    history_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{timestamp}] profile={report.get('profile')} verdict={report.get('overall_verdict')}"
    block = f"{header}\n{formatted_report}\n{'-' * 72}\n"
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(block)


def read_recent_history(path: Path | None = None, max_lines: int = 150) -> str:
    history_path = path or DEFAULT_HISTORY_PATH
    if not history_path.exists():
        return "No history found yet. Run a check first."
    with history_path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    return "".join(lines[-max_lines:]).strip()

