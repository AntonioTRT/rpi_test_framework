"""Execute commands through hub_mcu_utility without port or target options."""

import shlex
import subprocess

from csv_manager import CsvManager


class UtilityCommand:
    @staticmethod
    def build_command(command: str) -> list[str]:
        return ["hub_mcu_utility", *shlex.split(command)]

    @classmethod
    def run(cls, run_id: str, test_name: str, command: str) -> str:
        command_text = subprocess.list2cmdline(cls.build_command(command))
        csv_manager = CsvManager()
        print(
            f"{'=' * 40}\nTest: {test_name}\nRunID: {run_id}\n{'=' * 40}",
            flush=True,
        )
        print(f"\nTX:\n{command_text}", flush=True)
        csv_manager.log_tx(run_id, test_name, command_text)

        try:
            completed = subprocess.run(
                cls.build_command(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
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
        csv_manager.log_rx(run_id, test_name, command_text, response)
        return response
