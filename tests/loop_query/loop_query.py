from pathlib import Path
import time
import yaml

from csv_manager import CsvManager
from tektronix_i2c_platform import TektronixI2C
from utility_command import UtilityCommand


class LoopTest:

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
        passive_monitor_s = float(config.get("passive_monitor_s", 45))
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

            # 45 second delay before passive monitoring
            time.sleep(passive_delay_s)

            # Capture #2 (Passive monitoring)
            capture_session_2 = TektronixI2C.start_capture(
                run_id=run_id,
                test_name="rodi5",
                iteration=count,
                phase="PassiveMonitor",
                scope_capture_mode=scope_capture_mode,
                scope_trigger_timeout_s=passive_monitor_s,
                scope_export_delay_ms=scope_export_delay_ms,
                save_raw_export=save_raw_export,
            )
            try:
                pass
            finally:
                capture_session_2.stop_and_save(duration_s=passive_monitor_s)

            # Exit if a finite number of iterations was configured
            if iterations != -1 and count - previous_iteration >= int(iterations):
                print("\nLoop finished")
                break