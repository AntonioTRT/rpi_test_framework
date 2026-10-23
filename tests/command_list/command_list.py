"""Run configured commands once and record whether each expected value appears."""

from datetime import datetime
from pathlib import Path
import math
import re
import time
from uuid import uuid4

import yaml

from csv_manager import CsvManager
from utility_command2 import UtilityCommand


CSV_HEADERS = (
    "Timestamp",
    "TestId",
    "QuestionNumber",
    "Group",
    "Command",
    "Comparison",
    "Expected",
    "ActualValue",
    "Response",
    "Result",
)
NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(?![+-]?0[xX])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
)
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def evaluate_response(item: dict, response: str) -> tuple[str, str]:
    comparison = item.get("comparison", "contains")
    if comparison == "contains":
        passed = item["expected"] in response
        return ("PASS" if passed else "FAIL"), ""
    if comparison == "exact":
        passed = response.strip() == item["expected"]
        return ("PASS" if passed else "FAIL"), ""

    match = NUMBER_PATTERN.search(response)
    if match is None:
        return "FAIL", ""
    actual_text = match.group(0)
    actual_value = float(actual_text)
    passed = item["min"] <= actual_value <= item["max"]
    return ("PASS" if passed else "FAIL"), actual_text


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

        enabled_groups = None
        if "enabled_groups" in config:
            configured_groups = config["enabled_groups"]
            if not isinstance(configured_groups, list) or not all(
                isinstance(group, str) for group in configured_groups
            ):
                raise ValueError("Config value 'enabled_groups' must be a list of strings")
            enabled_groups = set(configured_groups)

        selected_commands = []
        for question_number, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Command {question_number} must be a mapping")
            group = item.get("group", "")
            if not isinstance(group, str):
                raise ValueError(f"Command {question_number} group must be a string")
            if enabled_groups is not None and group not in enabled_groups:
                continue

            command = item.get("command")
            comparison = item.get("comparison", "contains")
            if not isinstance(command, str):
                raise ValueError(f"Command {question_number} must have a string 'command'")
            if not isinstance(comparison, str) or comparison not in {
                "contains", "exact", "range",
            }:
                raise ValueError(
                    f"Command {question_number} has unsupported comparison {comparison!r}"
                )
            if comparison in {"contains", "exact"} and not isinstance(
                item.get("expected"), str
            ):
                raise ValueError(
                    f"Command {question_number} comparison '{comparison}' requires "
                    "a string 'expected' value"
                )
            if comparison == "range":
                bounds = (item.get("min"), item.get("max"))
                if not all(
                    isinstance(bound, (int, float))
                    and not isinstance(bound, bool)
                    and (isinstance(bound, int) or math.isfinite(bound))
                    for bound in bounds
                ) or bounds[0] > bounds[1]:
                    raise ValueError(
                        f"Command {question_number} range requires finite numeric "
                        "'min' and 'max' values with min <= max"
                    )
            selected_commands.append((question_number, item, group, comparison))

        csv_manager = CsvManager(headers=CSV_HEADERS)
        pass_count = 0

        for command_index, (question_number, item, group, comparison) in enumerate(
            selected_commands
        ):
            test_id = item["id"]
            command = item["command"]
            expected = item.get("expected", "")
            if not isinstance(test_id, str):
                raise ValueError(f"Command {question_number} must have a string 'id'")

            print("\033[2J\033[H", end="", flush=True)
            response = UtilityCommand.run(
                run_id=run_id,
                test_name="CommandListTest",
                command=command,
                display_id=test_id,
            )
            result, actual_value = evaluate_response(item, response)
            if result == "PASS":
                pass_count += 1
            csv_manager.append_record(
                "command_list.csv",
                {
                    "Timestamp": datetime.now().astimezone().strftime("%d-%m %H:%M"),
                    "TestId": test_id,
                    "QuestionNumber": question_number,
                    "Group": group,
                    "Command": command,
                    "Comparison": comparison,
                    "Expected": expected,
                    "ActualValue": actual_value,
                    "Response": response,
                    "Result": result,
                },
            )
            color = GREEN if result == "PASS" else RED
            print(f"{color}{result}{RESET}", flush=True)
            if command_index < len(selected_commands) - 1:
                time.sleep(command_delay_s)

        total = len(selected_commands)
        print(f"Total tests: {total}")
        print(f"Pass count: {pass_count}")
        print(f"Fail count: {total - pass_count}")


if __name__ == "__main__":
    LoopTest().run(str(uuid4()))
