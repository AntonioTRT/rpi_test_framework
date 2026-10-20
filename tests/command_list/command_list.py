"""Run configured commands once and record whether each expected value appears."""

from datetime import datetime
from pathlib import Path
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


class LoopTest:
    def run(self, run_id: str) -> None:
        config_path = Path(__file__).with_name("config.yaml")
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)

        if not isinstance(config, dict):
            raise ValueError(f"Expected a YAML mapping in {config_path}")

        commands = config["commands"]
        if not isinstance(commands, list):
            raise ValueError("Config value 'commands' must be a list")

        csv_manager = CsvManager(headers=CSV_HEADERS)
        pass_count = 0

        for question_number, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Command {question_number} must be a mapping")
            command = item["command"]
            expected = item["expected"]
            if not isinstance(command, str) or not isinstance(expected, str):
                raise ValueError(
                    f"Command {question_number} must have string "
                    "'command' and 'expected' values"
                )

            response = UtilityCommand.run(
                run_id=run_id,
                test_name="CommandListTest",
                command=command,
            )
            result = "PASS" if expected in response else "FAIL"
            if result == "PASS":
                pass_count += 1
            csv_manager.append_record(
                "command_list.csv",
                {
                    "Timestamp": datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    ),
                    "TestId": run_id,
                    "QuestionNumber": question_number,
                    "Command": command,
                    "Expected": expected,
                    "Response": response,
                    "Result": result,
                },
            )
            print(f"Question {question_number}: {result}", flush=True)

        total = len(commands)
        print(f"Total tests: {total}")
        print(f"Pass count: {pass_count}")
        print(f"Fail count: {total - pass_count}")


if __name__ == "__main__":
    LoopTest().run(str(uuid4()))
