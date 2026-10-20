"""Command capture followed by a delayed, bounded listen session.

Listen results are appended to logs/listen.csv, preserving multiline messages.
The listen subprocess uses a Linux PTY to avoid losing buffered utility output.
At 20 seconds, send SIGINT, then allow one second each before SIGTERM/SIGKILL.
Startup headers are excluded; listen and CSV errors are reported to the console.
"""

import csv
from datetime import datetime
import errno
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import yaml

from csv_manager import CsvManager
from tektronix_i2c_platform import TektronixI2C
from utility_command import UtilityCommand


class LoopTest:

    LISTEN_DURATION_S = 20

    @staticmethod
    def _stop_listen(process: subprocess.Popen) -> None:
        for stop_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, stop_signal)
            except ProcessLookupError:
                process.wait()
                return
            try:
                process.wait(timeout=1)
                return
            except subprocess.TimeoutExpired:
                print(f"Listen did not exit after {stop_signal.name}", flush=True)
        process.wait()

    @staticmethod
    def _listen_data(output: str) -> str:
        lines = output.splitlines(keepends=True)
        for index, line in enumerate(lines):
            text = line.strip()
            if text == "command: listen":
                data = "".join(lines[index + 1:])
                return data if data.strip() else ""
            if text and not text.startswith(("target:", "uart:", "protocol:")):
                return "".join(lines[index:])
        return ""

    def _listen(self, port: str, target: str, test_name: str, iteration: int) -> None:
        chunks = []
        print(f"\nListening for {self.LISTEN_DURATION_S} seconds", flush=True)
        try:
            master_fd, slave_fd = os.openpty()
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(master_fd, selectors.EVENT_READ)
                    process = subprocess.Popen(
                        UtilityCommand.build_command(port, target, "-dd listen"),
                        stdin=subprocess.DEVNULL,
                        stdout=slave_fd,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        os.close(slave_fd)
                        slave_fd = None
                        deadline = time.monotonic() + self.LISTEN_DURATION_S
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(timeout=remaining):
                                break
                            try:
                                chunk = os.read(master_fd, 65536)
                            except OSError as error:
                                if error.errno == errno.EIO:
                                    break  # PTYs report EIO when the child closes.
                                raise
                            if not chunk:
                                break
                            chunks.append(chunk)
                    finally:
                        self._stop_listen(process)
                    if process.returncode not in (0, -signal.SIGINT):
                        print(
                            f"Listen exited with status {process.returncode}",
                            flush=True,
                        )
            finally:
                if slave_fd is not None:
                    os.close(slave_fd)
                os.close(master_fd)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Listen failed: {type(error).__name__}: {error}", flush=True)
        except KeyboardInterrupt:
            print("Listen interrupted by user; continuing loop", flush=True)

        output = b"".join(chunks).decode("utf-8", errors="replace")
        listen_data = self._listen_data(output.replace("\r\n", "\n"))
        try:
            logs_dir = CsvManager().logs_dir
            logs_dir.mkdir(parents=True, exist_ok=True)
            with (logs_dir / "listen.csv").open(
                "a", newline="", encoding="utf-8"
            ) as stream:
                writer = csv.writer(stream)
                if stream.tell() == 0:
                    writer.writerow(("Timestamp", "TestName", "Iteration", "ListenData"))
                writer.writerow((
                    datetime.now().astimezone().strftime("%d-%m %H:%M"),
                    test_name, iteration, listen_data,
                ))
        except (OSError, csv.Error) as error:
            print(f"Listen CSV write failed: {type(error).__name__}: {error}", flush=True)

    def run(self, run_id: str) -> None:

        test_path = Path(__file__).resolve()

        with test_path.with_name("config.yaml").open(
            encoding="utf-8"
        ) as stream:

            config = yaml.safe_load(stream)

        port = str(config["port"])
        target = str(config["target"])

        interval_s = int(config.get("interval_s", 45))
        passive_delay_s = int(config.get("passive_delay_s", interval_s))
        iterations = config["iterations"]
        wake_delay_s = int(config["wake_delay_s"])
        scope_capture_mode = str(config.get("scope_capture_mode", "time"))
        scope_capture_s = float(config["scope_capture_s"])
        scope_trigger_timeout_s = float(config.get("scope_trigger_timeout_s", 30))
        scope_export_delay_ms = int(config.get("scope_export_delay_ms", 500))
        save_raw_export = bool(config.get("save_raw_export", False))

        count = CsvManager().last_iteration("LoopTest")
        previous_iteration = count

        while True:

            count += 1

            print(f"\nIteration {count}")

            # Capture #1 (Command phase)
            capture_session_1 = TektronixI2C.start_capture(
                run_id=run_id,
                test_name="rodi5",
                iteration=count,
                phase="RealCommand",
                scope_capture_mode=scope_capture_mode,
                scope_trigger_timeout_s=scope_trigger_timeout_s,
                scope_export_delay_ms=scope_export_delay_ms,
                save_raw_export=save_raw_export,
            )
            try:
                # Wake-up command
                UtilityCommand.run(
                    run_id=run_id,
                    test_name="rodi5",
                    port=port,
                    target=target,
                    command="bpcp status",
                    iteration=count,
                )

                # Wait before requesting the real response
                time.sleep(wake_delay_s)

                # Real command
                UtilityCommand.run(
                    run_id=run_id,
                    test_name="rodi5",
                    port=port,
                    target=target,
                    command="bpcp status",
                    iteration=count,
                )
            finally:
                capture_session_1.stop_and_save(duration_s=scope_capture_s)

            # 45 second delay before listening
            time.sleep(passive_delay_s)

            self._listen(port, target, "rodi5", count)

            # Exit if a finite number of iterations was configured
            if iterations != -1 and count - previous_iteration >= int(iterations):
                print("\nLoop finished")
                break