"""Centralized monthly CSV logging for test runs."""

import csv
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_HEADERS = (
    "RunID", "Timestamp", "Iteration", "TestName", "Direction", "Command", "Response",
)

def sanitize_multiline_text(value: object) -> str:
    return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


class CsvManager:
    def __init__(
        self, logs_dir: Path | None = None, headers: Sequence[str] = DEFAULT_HEADERS,
    ) -> None:
        self.logs_dir = Path(logs_dir) if logs_dir else Path(__file__).resolve().parent / "logs"
        self.headers = tuple(headers)

    def last_iteration(self, test_name: str) -> int:
        latest_iteration = 0
        for path in self.logs_dir.glob("*.csv"):
            try:
                with path.open("r", newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    fieldnames = set(reader.fieldnames or ())
                    if not {"Iteration", "TestName", "Direction", "Command"}.issubset(fieldnames):
                        continue
                    for row in reader:
                        if row.get("TestName") != test_name:
                            continue
                        try:
                            latest_iteration = max(latest_iteration, int(row["Iteration"]))
                        except (TypeError, ValueError):
                            continue
            except (OSError, csv.Error):
                continue
        return latest_iteration

    def log_tx(
        self, run_id: str, test_name: str, command: str,
        iteration: int | None = None,
    ) -> None:
        self._append("TX", run_id, test_name, command, iteration=iteration)

    def log_rx(
        self, run_id: str, test_name: str, command: str, response: str,
        iteration: int | None = None,
    ) -> None:
        self._append("RX", run_id, test_name, command, response, iteration)

    def append_record(self, filename: str, values: Mapping[str, object]) -> None:
        if Path(filename).name != filename:
            raise ValueError("CSV filename must not include a directory")

        path = self.logs_dir / filename
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size:
            with path.open("r", newline="", encoding="utf-8") as stream:
                existing_headers = next(csv.reader(stream), [])
            if existing_headers != list(self.headers):
                raise ValueError(f"Unexpected CSV headers in {path}")

        with path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            if stream.tell() == 0:
                writer.writerow(self.headers)
            writer.writerow(values.get(header, "") for header in self.headers)

    def _append(
        self, direction: str, run_id: str, test_name: str, command: str,
        response: str = "",
        iteration: int | None = None,
    ) -> None:
        timestamp = datetime.now().astimezone()
        path = self.logs_dir / f"{timestamp:%Y-%m}.csv"
        values = {
            "RunID": run_id,
            "Timestamp": timestamp.isoformat(timespec="milliseconds"),
            "Iteration": "" if iteration is None else iteration,
            "TestName": test_name,
            "Direction": direction,
            "Command": command,
            "Response": response,
        }
        row = tuple(sanitize_multiline_text(values.get(header, "")) for header in self.headers)

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size:
            self._upgrade_schema(path)
        with path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            if stream.tell() == 0:
                writer.writerow(sanitize_multiline_text(header) for header in self.headers)
            writer.writerow(row)

    def _upgrade_schema(self, path: Path) -> None:
        with path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            existing_headers = list(reader.fieldnames or ())
            if existing_headers == list(self.headers):
                return
            existing_rows = list(reader)

        headers = list(self.headers)
        headers.extend(header for header in existing_headers if header not in headers)
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=path.parent,
            delete=False, suffix=".tmp",
        ) as temporary:
            writer = csv.DictWriter(temporary, fieldnames=headers)
            writer.writeheader()
            writer.writerows(existing_rows)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
        self.headers = tuple(headers)