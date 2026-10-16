from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import subprocess
import time

import yaml


@dataclass(frozen=True)
class BatteryReading:
    level: str
    voltage: str
    remaining_capacity: str
    full_capacity: str
    current: str
    cycle_count: str
    battery_health: str
    charging_status: str
    internal_temperature: str
    external_temperature: str
    timestamp: str


FIELD_ALIASES = {
    "level": ("level", "batterylevel", "chargelevel"),
    "voltage": ("voltage", "batteryvoltage"),
    "remaining_capacity": ("remainingcapacity", "remainingcap", "capacityremaining"),
    "full_capacity": ("fullcapacity", "designcapacity", "capacityfull"),
    "current": ("current", "batterycurrent"),
    "cycle_count": ("cyclecount", "cycles"),
    "battery_health": ("batteryhealth", "health"),
    "charging_status": ("chargingstatus", "chargestatus", "charging"),
    "internal_temperature": ("internaltemperature", "internaltemp"),
    "external_temperature": ("externaltemperature", "externaltemp"),
}

DISPLAY_FIELDS = (
    ("Battery Level", "level", "%"),
    ("Voltage", "voltage", "mV"),
    ("Remaining Capacity", "remaining_capacity", "mAh"),
    ("Full Capacity", "full_capacity", "mAh"),
    ("Current", "current", "mA"),
    ("Cycle Count", "cycle_count", ""),
    ("Battery Health", "battery_health", "%"),
    ("Charging Status", "charging_status", ""),
    ("Internal Temp", "internal_temperature", "C"),
    ("External Temp", "external_temperature", "C"),
)

CLEAR_SCREEN = "\033[2J\033[H"
RED = "\033[31m"
RESET = "\033[0m"


def parse_battery_status(output: str) -> BatteryReading:
    """Parse labeled values from the utility's battery-status response."""
    parsed: dict[str, str] = {}

    for line in output.splitlines():
        match = re.match(r"^\s*([^:=]+?)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue

        label = re.sub(r"[^a-z0-9]", "", match.group(1).lower())
        value = match.group(2).strip()
        if not value:
            continue

        for field, aliases in FIELD_ALIASES.items():
            if any(alias in label for alias in aliases):
                parsed[field] = value
                break

    missing = [field for field in FIELD_ALIASES if field not in parsed]
    if missing:
        raise ValueError("Missing battery fields: " + ", ".join(missing))

    return BatteryReading(
        **parsed,
        timestamp=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    )


def with_unit(value: str, unit: str) -> str:
    if not unit or re.search(rf"\s*{re.escape(unit)}\s*$", value, re.IGNORECASE):
        return value
    return f"{value} {unit}"


class LoopTest:
    def __init__(self) -> None:
        config_path = Path(__file__).with_name("config.yaml")
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)

        if not isinstance(config, dict):
            raise ValueError(f"Expected a YAML mapping in {config_path}")

        self.port = str(config["port"]).strip()
        self.target = str(config["target"]).strip()
        self.polling_interval_s = int(config["polling_interval_s"])
        if not self.port or not self.target:
            raise ValueError("Config values 'port' and 'target' must not be empty")
        if self.polling_interval_s < 1:
            raise ValueError("'polling_interval_s' must be at least 1")

        self.latest_reading: BatteryReading | None = None
        self.last_query_failed = False

    def query_battery(self) -> None:
        command = [
            "hub_mcu_utility", "-u", self.port, "-t", self.target,
            "bpcp", "status",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.last_query_failed = True
            self.failure_detail = f"{type(error).__name__}: {error}"
            return

        output = result.stdout + result.stderr
        if result.returncode != 0 or re.search(r"\bERROR\s*:", output, re.IGNORECASE):
            self.last_query_failed = True
            self.failure_detail = output.strip() or f"Utility exited with status {result.returncode}"
            return

        try:
            reading = parse_battery_status(output)
        except ValueError as error:
            self.last_query_failed = True
            self.failure_detail = str(error)
            return

        self.latest_reading = reading
        self.last_query_failed = False
        self.failure_detail = ""

    def display(self, seconds_until_update: int) -> None:
        failed = self.last_query_failed
        color = RED if failed else ""
        reset = RESET if failed else ""

        print(CLEAR_SCREEN, end="")
        print("=" * 40)
        print("Battery Status Monitor")
        print("=" * 40)

        if failed:
            print(f"{RED}LAST VALID READING{RESET}")
            print(f"{RED}DEVICE NOT RESPONDING{RESET}")
            detail = getattr(self, "failure_detail", "")
            if detail:
                print(f"{RED}Query error: {detail.splitlines()[0][:160]}{RESET}")

        reading = self.latest_reading
        timestamp = reading.timestamp if reading else "Unavailable"
        print(f"\n{color}Timestamp: {timestamp}{reset}\n")

        for label, field, unit in DISPLAY_FIELDS:
            value = getattr(reading, field) if reading else "Unavailable"
            formatted_value = with_unit(value, unit) if reading else value
            print(f"{color}{label:<20}: {formatted_value}{reset}")

        print(f"\nNext update in: {seconds_until_update}s", flush=True)

    def run(self, run_id: str) -> None:
        while True:
            self.query_battery()
            remaining = self.polling_interval_s
            while remaining > 0:
                self.display(remaining)
                time.sleep(1)
                remaining -= 1


if __name__ == "__main__":
    LoopTest().run("")
