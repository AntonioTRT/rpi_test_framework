"""Run configured commands once and record whether each expected value appears."""

from datetime import datetime
from pathlib import Path
import time
from uuid import uuid4

import yaml

from csv_manager import CsvManager
from utility_command2 import UtilityCommand


CSV_HEADERS = (
    "Timestamp",
    "TestId",
    "QuestionNumber",
    "Command",
    "Expected",
    "Response",
    "Result",
)
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


class LoopTest:
    def run(self, run_id: str) -> None:
        config_path = Path(__file__).with_name("config.yaml")
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)

        if not isinstance(config, dict):
            raise ValueError(f"Expected a YAML mapping in {config_path}")

        commands = config["commands"]
        command_delay_s = float(config.get("command_delay_s", 1))
        if not isinstance(commands, list):
            raise ValueError("Config value 'commands' must be a list")
        if command_delay_s < 0:
            raise ValueError("Config value 'command_delay_s' must not be negative")

        csv_manager = CsvManager(headers=CSV_HEADERS)
        pass_count = 0

        for question_number, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Command {question_number} must be a mapping")
            test_id = item["id"]
            command = item["command"]
            expected = item["expected"]
            if not all(isinstance(value, str) for value in (test_id, command, expected)):
                raise ValueError(
                    f"Command {question_number} must have string "
                    "'id', 'command', and 'expected' values"
                )

            print("\033[2J\033[H", end="", flush=True)
            response = UtilityCommand.run(
                run_id=run_id,
                test_name="CommandListTest",
                command=command,
                display_id=test_id,
            )
            result = "PASS" if expected in response else "FAIL"
            if result == "PASS":
                pass_count += 1
            csv_manager.append_record(
                "command_list.csv",
                {
                    "Timestamp": datetime.now().astimezone().strftime("%d-%m %H:%M"),
                    "TestId": test_id,
                    "QuestionNumber": question_number,
                    "Command": command,
                    "Expected": expected,
                    "Response": response,
                    "Result": result,
                },
            )
            color = GREEN if result == "PASS" else RED
            print(f"{color}{result}{RESET}", flush=True)
            if question_number < len(commands):
                time.sleep(command_delay_s)

        total = len(commands)
        print(f"Total tests: {total}")
        print(f"Pass count: {pass_count}")
        print(f"Fail count: {total - pass_count}")


if __name__ == "__main__":
    LoopTest().run(str(uuid4()))
