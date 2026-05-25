from __future__ import annotations

import logging
from pathlib import Path


class LocalNotifier:
    def __init__(self, log_path: str | Path = "logs/notifications.log") -> None:
        self.log_path = Path(log_path)
        self.logger = logging.getLogger("hushine_strategy.notify")

    def _write(self, level: str, message: str, title: str = "") -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{level.upper()} {title} {message}".strip()
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
        getattr(self.logger, level.lower(), self.logger.info)(line)

    def info(self, message: str, title: str = "") -> None:
        self._write("info", message, title)

    def warn(self, message: str, title: str = "") -> None:
        self._write("warning", message, title)

    def error(self, message: str, title: str = "") -> None:
        self._write("error", message, title)
