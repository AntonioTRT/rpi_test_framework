"""Execute commands through the hub MCU utility."""

import shlex
import subprocess

from csv_manager import CsvManager


class UtilityCommand:
    @staticmethod
    def build_command(port: str, target: str, command: str) -> list[str]:
        return [
            "hub_mcu_utility", "-u", port, "-t", target, *shlex.split(command),
        ]

    @classmethod
    def run(
        cls, run_id: str, test_name: str, port: str, target: str, command: str,
        iteration: int | None = None,
    ) -> str:
        command_text = subprocess.list2cmdline(cls.build_command(port, target, command))
        csv_manager = CsvManager()
        print(
            f"{'=' * 40}\nTest: {test_name}\nRunID: {run_id}\n{'=' * 40}",
            flush=True,
        )
        print(f"\nTX:\n{command_text}", flush=True)
        csv_manager.log_tx(run_id, test_name, command_text, iteration=iteration)

        try:
            completed = subprocess.run(
                cls.build_command(port, target, command), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=True,
            )
            response = completed.stdout + completed.stderr
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            output = []
            for part in (error.stdout, error.stderr):
                if isinstance(part, bytes):
                    part = part.decode("utf-8", errors="replace")
                if part:
                    output.append(part)
            response = "".join(output)
            if response and not response.endswith("\n"):
                response += "\n"
            response += f"{type(error).__name__}: {error}"
        except KeyboardInterrupt:
            response = "Interrupted by user"
        except Exception as error:
            response = f"{type(error).__name__}: {error}"

        print("\nRX:", flush=True)
        print(response, end="" if response.endswith("\n") else "\n", flush=True)
        csv_manager.log_rx(run_id, test_name, command_text, response, iteration=iteration)
        return response