"""Capture decoded I2C transactions from a Tektronix oscilloscope."""

import csv
import fcntl
import io
import math
import os
import re
import select
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

USBTMC_IOC_NR = 91
USBTMC_IOCTL_CLEAR = (USBTMC_IOC_NR << 8) | 2
USBTMC_IOCTL_MSG_IN_ATTR = (2 << 30) | (1 << 16) | (USBTMC_IOC_NR << 8) | 24


class _USBTMCScope:
    def __init__(self, device: str) -> None:
        self._stream = open(device, "r+b", buffering=0)
        self._closed = False
        self.timeout = 10000
        self.clear(context="open")

    def clear(self, context: str = "operation") -> None:
        try:
            fcntl.ioctl(self._stream.fileno(), USBTMC_IOCTL_CLEAR)
            print(f"USBTMC clear on {context}: device output queue reset")
        except Exception as error:
            print(f"USBTMC clear on {context} failed: {error}")

    def write(self, command: str) -> None:
        data = command.encode("ascii")
        if not data.endswith((b"\n", b"\r")):
            data += b"\n"
        self._stream.write(data)
        print(f"USBTMC command written: {command}")

    def query(self, command: str) -> str:
        try:
            self.write(command)
            time.sleep(0.5)
            response = bytearray()
            while not response.endswith(b"\n"):
                chunk = self._stream.read(1024)
                if not chunk:
                    break
                response.extend(chunk)
            response_bytes = bytes(response)
            if not response_bytes:
                raise TimeoutError(f"Empty response from USBTMC device for command: {command}")
            decoded = response_bytes.decode("utf-8", errors="replace").strip()
            print(f"USBTMC bytes received: {response_bytes!r}")
            print(f"USBTMC decoded response: {decoded!r}")
            return decoded
        except Exception as error:
            print(f"USBTMC query error on {command!r}: {error}")
            self.clear(context="query failure")
            raise

    def read_raw(self) -> bytes:
        response = bytearray()
        expected_length = None
        attr_buf = bytearray(1)
        try:
            while True:
                if expected_length is not None and len(response) >= expected_length:
                    break
                read_size = 65536
                if expected_length is not None:
                    read_size = min(read_size, expected_length - len(response))
                chunk = self._stream.read(read_size)
                if not chunk:
                    break
                response.extend(chunk)

                try:
                    fcntl.ioctl(self._stream.fileno(), USBTMC_IOCTL_MSG_IN_ATTR, attr_buf)
                    eom = bool(attr_buf[0] & 1)
                except Exception:
                    eom = False

                if expected_length is None and len(response) >= 2 and response[:1] == b"#":
                    digit_count = response[1] - ord("0")
                    if 1 <= digit_count <= 9 and len(response) >= 2 + digit_count:
                        data_length = int(response[2 : 2 + digit_count])
                        expected_length = 2 + digit_count + data_length

                if expected_length is not None and len(response) >= expected_length:
                    break
                if expected_length is None and eom:
                    break
            return bytes(response)
        except Exception as error:
            print(f"USBTMC read_raw error: {error}")
            self.clear(context="read_raw failure")
            raise

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True


class TektronixI2C:
    DEFAULT_BUS = "B1"
    DEFAULT_SCL_CHANNEL = "CH3"
    DEFAULT_SDA_CHANNEL = "CH1"
    RESOURCE_ENV = "TEKTRONIX_I2C_RESOURCE"
    _capture_files: dict[tuple[str, str], Path] = {}

    @classmethod
    def start_capture(
        cls,
        run_id: str,
        test_name: str,
        iteration: int,
        *,
        phase: str = "Command",
        resource_name: str | None = None,
        scl_channel: str = DEFAULT_SCL_CHANNEL,
        sda_channel: str = DEFAULT_SDA_CHANNEL,
        bus: str = DEFAULT_BUS,
        standalone: bool = False,
        scope_export_delay_ms: int = 500,
        save_raw_export: bool = False,
        scope_capture_mode: str = "time",
        scope_trigger_timeout_s: float = 30,
    ) -> "_I2CCaptureSession":
        scope_capture_mode = scope_capture_mode.strip().casefold()
        if scope_capture_mode not in {"time", "trigger"}:
            raise ValueError("scope_capture_mode must be 'time' or 'trigger'")
        if scope_capture_mode == "trigger" and (
            not math.isfinite(scope_trigger_timeout_s) or scope_trigger_timeout_s <= 0
        ):
            raise ValueError("scope_trigger_timeout_s must be a finite positive number")

        resource_manager, scope, resource, idn = cls._open_scope(
            resource_name, require_mso24_usb=standalone,
        )
        original_stop_after = None
        try:
            if standalone:
                print(f"VISA resource: {resource}")
                print(f"*IDN?: {idn}")
            scope.timeout = 120000
            scope.write("ACQUIRE:STATE STOP")
            scope.write(f"BUS:{bus}:TYPE I2C")
            scope.write(f"BUS:{bus}:I2C:SCL {scl_channel}")
            scope.write(f"BUS:{bus}:I2C:SDA {sda_channel}")
            scope.write(f"BUS:{bus}:STATE ON")
            if scope_capture_mode == "trigger":
                original_stop_after = scope.query("ACQUIRE:STOPAfter?").strip().split()[-1]
                original_stop_after = original_stop_after.rstrip(";").upper()
                if original_stop_after not in {"RUNSTOP", "SEQUENCE"}:
                    raise RuntimeError(
                        f"Unexpected ACQUIRE:STOPAfter? response: {original_stop_after}"
                    )
                scope.write("ACQUIRE:STOPAfter SEQuence")
            capture_started_at = time.monotonic()
            scope.write("ACQUIRE:STATE RUN")
            return _I2CCaptureSession(
                resource_manager=resource_manager,
                scope=scope,
                run_id=run_id,
                test_name=test_name,
                iteration=iteration,
                phase=phase,
                bus=bus,
                standalone=standalone,
                scope_export_delay_ms=scope_export_delay_ms,
                save_raw_export=save_raw_export,
                scope_capture_mode=scope_capture_mode,
                scope_trigger_timeout_s=scope_trigger_timeout_s,
                original_stop_after=original_stop_after,
                capture_started_at=capture_started_at,
            )
        except Exception:
            try:
                scope.write("ACQUIRE:STATE STOP")
            finally:
                try:
                    if original_stop_after is not None:
                        scope.write(f"ACQUIRE:STOPAfter {original_stop_after}")
                finally:
                    try:
                        scope.close()
                    finally:
                        resource_manager.close()
            raise

    @classmethod
    def capture(
        cls,
        run_id: str | None = None,
        test_name: str = "Standalone",
        iteration: int = 1,
        duration_s: float = 5,
        *,
        phase: str = "Standalone",
        resource_name: str | None = None,
        scl_channel: str = DEFAULT_SCL_CHANNEL,
        sda_channel: str = DEFAULT_SDA_CHANNEL,
        bus: str = DEFAULT_BUS,
        standalone: bool = False,
        scope_export_delay_ms: int = 500,
        save_raw_export: bool = False,
    ) -> list[str]:
        if duration_s <= 0:
            raise ValueError("duration_s must be greater than zero")

        session = cls.start_capture(
            run_id=run_id or uuid4().hex,
            test_name=test_name,
            iteration=iteration,
            phase=phase,
            resource_name=resource_name,
            scl_channel=scl_channel,
            sda_channel=sda_channel,
            bus=bus,
            standalone=standalone,
            scope_export_delay_ms=scope_export_delay_ms,
            save_raw_export=save_raw_export,
        )
        transactions = session.stop_and_save(duration_s=duration_s)
        if standalone:
            print(f"CSV: {session.csv_path}")
        return [transaction["DecodedText"] for transaction in transactions]

    @staticmethod
    def _open_scope(
        resource_name: str | None,
        require_mso24_usb: bool = False,
    ) -> tuple[Any, Any, str, str]:
        device = "/dev/usbtmc0"
        scope = None
        try:
            scope = _USBTMCScope(device)
            scope.timeout = 10000
            idn = scope.query("*IDN?").strip()
            is_tektronix = "TEKTRONIX" in idn.upper()
            is_mso24 = "MSO24" in idn.upper()
            if is_tektronix and (not require_mso24_usb or is_mso24):
                return scope, scope, device, idn
            raise RuntimeError(f"Unexpected instrument identification: {idn!r}")
        except Exception as error:
            if scope is not None:
                scope.close()
            target = "Tektronix MSO24" if require_mso24_usb else "Tektronix oscilloscope"
            raise RuntimeError(f"No {target} found at {device}") from error

    @staticmethod
    def _read_bus_events(scope: Any, bus: str) -> bytes:
        bus_number = re.sub(r"[^0-9]", "", bus) or "1"
        instrument_path = f"tektronix_i2c_{uuid4().hex}.csv"
        scope.write(f'SAVE:EVENTtable:BUS{bus_number} "{instrument_path}"')
        scope.query("*OPC?")
        scope.write(f'FILESYSTEM:READFILE "{instrument_path}"')
        try:
            return scope.read_raw()
        finally:
            scope.write(f'FILESYSTEM:DELETE "{instrument_path}"')

    @staticmethod
    def _decode_file_data(raw_data: bytes) -> str:
        if raw_data.startswith(b"#") and len(raw_data) > 2:
            digit_count = int(chr(raw_data[1]))
            if digit_count:
                length_start = 2
                data_start = length_start + digit_count
                data_length = int(raw_data[length_start:data_start])
                raw_data = raw_data[data_start:data_start + data_length]
        return raw_data.decode("utf-8-sig", errors="replace").strip()

    @staticmethod
    def _count_event_rows(response: str) -> int:
        rows = list(csv.reader(io.StringIO(response)))
        header_index = None
        headers: list[str] = []
        for index, row in enumerate(rows):
            if not row or TektronixI2C._is_metadata(",".join(row)):
                continue
            normalized = [TektronixI2C._normalize_header(value) for value in row]
            if "address" in normalized and sum(bool(value) for value in normalized) > 1:
                header_index = index
                headers = normalized
                break
        if header_index is None:
            return 0

        count = 0
        for row in rows[header_index + 1:]:
            if not any(value.strip() for value in row):
                continue
            if TektronixI2C._is_metadata(",".join(row)):
                continue
            if [TektronixI2C._normalize_header(value) for value in row] == headers:
                continue
            count += 1
        return count

    @staticmethod
    def _save_raw_export(run_id: str, test_name: str, raw_data: bytes) -> Path:
        timestamp = datetime.now().astimezone()
        safe_test_name = (
            "standalone" if test_name.casefold() == "standalone" else re.sub(
                r"[^A-Za-z0-9_.-]+", "_", test_name,
            ).strip("._") or "Test"
        )
        filename = f"tektronix_{safe_test_name}_{timestamp:%Y%m%d_%H%M%S}_raw.txt"
        logs_dir = Path(__file__).resolve().parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / filename
        path.write_bytes(raw_data)
        return path

    @staticmethod
    def _parse_transactions(response: str) -> list[dict[str, str]]:
        rows = []
        raw_lines = response.splitlines(keepends=True)
        previous_line = 0
        reader = csv.reader(io.StringIO(response))
        for values in reader:
            end_line = reader.line_num
            raw_row = "".join(raw_lines[previous_line:end_line]).rstrip("\r\n")
            rows.append((values, raw_row))
            previous_line = end_line

        header_index = None
        headers: list[str] = []
        for index, (row, _) in enumerate(rows):
            if not row or TektronixI2C._is_metadata(",".join(row)):
                continue
            normalized = [TektronixI2C._normalize_header(value) for value in row]
            if "address" in normalized and sum(bool(value) for value in normalized) > 1:
                header_index = index
                headers = [
                    TektronixI2C._canonical_field(value, column_index)
                    for column_index, value in enumerate(row)
                ]
                break

        if header_index is None:
            return []

        transactions = []
        normalized_headers = [TektronixI2C._normalize_header(value) for value in headers]
        address_is_hex = "(h)" in rows[header_index][0][headers.index("Address")].lower()
        for row, raw_row in rows[header_index + 1:]:
            row_text = raw_row.strip()
            if (
                not row_text
                or not any(value.strip() for value in row)
                or TektronixI2C._is_metadata(row_text)
            ):
                continue
            if [TektronixI2C._normalize_header(value) for value in row] == normalized_headers:
                continue
            if len(row) < len(headers):
                row = [*row, *("" for _ in range(len(headers) - len(row)))]

            source_values = {
                header: row[index].strip()
                for index, header in enumerate(headers)
                if index < len(row)
            }
            if not source_values:
                continue

            address_value = source_values.get("Address", "")
            address, direction = TektronixI2C._parse_address_direction(address_value)
            direction = source_values.get("Direction", direction)
            transaction = {header: value.strip() for header, value in source_values.items()}
            transaction["StartTime"] = source_values.get("StartTime", "")
            transaction["StopTime"] = source_values.get("StopTime", "")
            transaction["Address"] = address
            transaction["Address7bit"] = TektronixI2C._address_7bit(
                address_value, address_is_hex,
            )
            transaction["Direction"] = direction
            transaction["Data"] = source_values.get("Data", "")
            transaction["Ack"] = source_values.get("Ack", "")
            transaction["Error"] = source_values.get("Error", "")
            transaction["DecodedText"] = raw_row
            transactions.append(transaction)
        return transactions

    @staticmethod
    def _normalize_header(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", re.sub(r"\([^)]*\)", "", value.lower()))

    @staticmethod
    def _canonical_field(value: str, index: int) -> str:
        normalized = TektronixI2C._normalize_header(value)
        known = {
            "starttime": "StartTime",
            "stoptime": "StopTime",
            "address": "Address",
            "direction": "Direction",
            "data": "Data",
            "ack": "Ack",
            "error": "Error",
            "unknownunframed": "UnknownUnframed",
        }
        if normalized in known:
            return known[normalized]
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|$)|[0-9]+", value)
        field = "".join(word[:1].upper() + word[1:] for word in words)
        return field or f"TektronixColumn{index + 1}"

    @staticmethod
    def _is_metadata(line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.casefold().startswith("tekscope version:"):
            return True
        if re.fullmatch(r"bus\d+\(i2c\)", stripped, flags=re.IGNORECASE):
            return True
        try:
            datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _parse_address_direction(value: str) -> tuple[str, str]:
        match = re.match(r"\s*(.*?)\s*:\s*(read|write)\s*$", value, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).capitalize()
        return value.strip(), ""

    @staticmethod
    def _address_7bit(value: str, hexadecimal: bool) -> str:
        address = re.sub(r":(?:read|write)$", "", value.strip(), flags=re.IGNORECASE)
        try:
            integer = int(address, 16 if hexadecimal or address.lower().startswith("0x") else 10)
        except ValueError:
            return ""
        return f"{integer >> 1:X}"

    @staticmethod
    def _save_transactions(
        run_id: str,
        test_name: str,
        iteration: int,
        transactions: list[dict[str, str]],
        phase: str = "Command",
    ) -> Path:
        timestamp = datetime.now().astimezone()
        execution = (run_id, test_name)
        path = TektronixI2C._capture_files.get(execution)
        if path is None:
            safe_test_name = (
                "standalone" if test_name.casefold() == "standalone" else re.sub(
                    r"[^A-Za-z0-9_.-]+", "_", test_name,
                ).strip("._") or "Test"
            )
            filename = f"tektronix_{safe_test_name}.csv"
            logs_dir = Path(__file__).resolve().parent / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            path = logs_dir / filename
            TektronixI2C._capture_files[execution] = path

        base_headers = (
            "RunID", "Timestamp", "TestName", "Iteration", "Phase", "StartTime",
            "StopTime", "Address", "Address7bit", "Direction", "Data", "Ack", "Error",
        )
        extra_headers = list(dict.fromkeys(
            key
            for transaction in transactions
            for key in transaction
            if key not in base_headers and key != "DecodedText"
        ))
        required_headers = [*base_headers, *extra_headers, "DecodedText"]

        if not transactions:
            transactions = [{
                "StartTime": "",
                "StopTime": "",
                "Address": "",
                "Address7bit": "",
                "Direction": "",
                "Data": "",
                "Ack": "",
                "Error": "",
                "DecodedText": "NO_TRANSACTIONS_DETECTED",
            }]

        existing_rows: list[dict[str, str]] = []
        headers = required_headers
        schema_changed = False
        if path.exists() and path.stat().st_size:
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                headers = list(reader.fieldnames or required_headers)
                existing_rows = list(reader)
            for header in required_headers:
                if header not in headers:
                    headers.insert(len(headers) - ("DecodedText" in headers), header)
                    schema_changed = True

        new_rows = [
            {
                "RunID": run_id,
                "Timestamp": timestamp.strftime("%d-%m %H:%M"),
                "TestName": test_name,
                "Iteration": str(iteration),
                "Phase": phase,
                **transaction,
            }
            for transaction in transactions
        ]
        if schema_changed:
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8", dir=path.parent,
                delete=False, suffix=".tmp",
            ) as temporary:
                writer = csv.DictWriter(temporary, fieldnames=headers)
                writer.writeheader()
                writer.writerows(existing_rows)
            Path(temporary.name).replace(path)

        with path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            if stream.tell() == 0:
                writer.writeheader()
            writer.writerows(new_rows)
        return path


class _I2CCaptureSession:
    def __init__(
        self,
        *,
        resource_manager: Any,
        scope: Any,
        run_id: str,
        test_name: str,
        iteration: int,
        phase: str,
        bus: str,
        standalone: bool,
        scope_export_delay_ms: int,
        save_raw_export: bool,
        scope_capture_mode: str,
        scope_trigger_timeout_s: float,
        original_stop_after: str | None,
        capture_started_at: float,
    ) -> None:
        self.resource_manager = resource_manager
        self.scope = scope
        self.run_id = run_id
        self.test_name = test_name
        self.iteration = iteration
        self.phase = phase
        self.bus = bus
        self.standalone = standalone
        self.scope_export_delay_ms = scope_export_delay_ms
        self.save_raw_export = save_raw_export
        self.scope_capture_mode = scope_capture_mode
        self.scope_trigger_timeout_s = scope_trigger_timeout_s
        self.original_stop_after = original_stop_after
        self.capture_started_at = capture_started_at
        self.csv_path: Path | None = None
        self._closed = False
        self._acquiring = True

    def stop_and_save(self, duration_s: float) -> list[dict[str, str]]:
        if self._closed:
            raise RuntimeError("Capture session is already closed")
        if duration_s < 0:
            raise ValueError("duration_s must not be negative")

        self.scope.timeout = max(self.scope.timeout, int(duration_s * 1000) + 30000)
        try:
            if self.scope_capture_mode == "trigger":
                if not self._wait_for_trigger():
                    self.scope.write("ACQUIRE:STATE STOP")
                    self._acquiring = False
                    return []
            else:
                if duration_s:
                    time.sleep(duration_s)
                self.scope.write("ACQUIRE:STATE STOP")
            self._acquiring = False
            if self.scope_export_delay_ms:
                time.sleep(self.scope_export_delay_ms / 1000)
            raw_data = TektronixI2C._read_bus_events(self.scope, self.bus)
            response = TektronixI2C._decode_file_data(raw_data)
            raw_rows = TektronixI2C._count_event_rows(response)
            print(f"Raw export rows: {raw_rows}")
            if self.save_raw_export and raw_rows > 0:
                TektronixI2C._save_raw_export(self.run_id, self.test_name, raw_data)
            transactions = TektronixI2C._parse_transactions(response)
            print(f"Parsed transactions: {len(transactions)}")
            self.csv_path = TektronixI2C._save_transactions(
                self.run_id,
                self.test_name,
                self.iteration,
                transactions,
                self.phase,
            )
            print(f"Saved transactions: {len(transactions)}")
            if not transactions:
                print("Saved CSV rows: 1 (empty-capture placeholder)")
            return transactions
        finally:
            self.close()

    def _query_before_deadline(self, command: str, deadline: float, base_timeout: int) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        self.scope.timeout = max(1, min(base_timeout, int(remaining * 1000)))
        try:
            response = self.scope.query(command).strip()
            return response or None
        except Exception:
            if time.monotonic() >= deadline:
                return None
            raise
        finally:
            self.scope.timeout = base_timeout

    def _wait_for_trigger(self) -> bool:
        timeout = self.scope_trigger_timeout_s
        trigger_deadline = self.capture_started_at + timeout
        base_timeout = self.scope.timeout
        print("Trigger mode enabled")
        print("Waiting for trigger...")

        mode_response = self._query_before_deadline(
            "TRIGGER:A:MODE?", trigger_deadline, base_timeout,
        )
        if mode_response is None or time.monotonic() >= trigger_deadline:
            print(f"Trigger timeout after {timeout:g} seconds")
            return False
        trigger_mode = mode_response.split()[-1].rstrip(";").upper()
        if trigger_mode == "AUTO":
            print("Trigger mode is AUTO; waiting for a confirmed trigger event")

        triggered = False
        completion_deadline = trigger_deadline
        while True:
            active_deadline = trigger_deadline if not triggered else completion_deadline
            trigger_response = self._query_before_deadline(
                "TRIGger:STATE?", active_deadline, base_timeout,
            )
            if trigger_response is None or time.monotonic() >= active_deadline:
                if triggered:
                    print(f"Acquisition completion timeout after {timeout:g} seconds")
                else:
                    print(f"Trigger timeout after {timeout:g} seconds")
                return False

            trigger_state = trigger_response.split()[-1].rstrip(";").upper()
            if trigger_state == "TRIGGER" and not triggered:
                triggered = True
                completion_deadline = time.monotonic() + timeout
                print("Trigger detected")
            elif trigger_state == "SAVE" and not triggered:
                acquisition_response = self._query_before_deadline(
                    "ACQuire:STATE?", trigger_deadline, base_timeout,
                )
                if acquisition_response is None:
                    print(f"Trigger timeout after {timeout:g} seconds")
                    return False
                acquisition_state = acquisition_response.split()[-1].rstrip(";")
                if acquisition_state == "0":
                    if trigger_mode == "NORMAL":
                        print("Trigger detected")
                        print("Acquisition complete")
                        return True
                    print("Acquisition ended without a confirmed trigger; skipping export")
                    return False

            if triggered:
                acquisition_response = self._query_before_deadline(
                    "ACQuire:STATE?", completion_deadline, base_timeout,
                )
                if acquisition_response is None or time.monotonic() >= completion_deadline:
                    print(f"Acquisition completion timeout after {timeout:g} seconds")
                    return False
                acquisition_state = acquisition_response.split()[-1].rstrip(";")
                if acquisition_state == "0":
                    print("Acquisition complete")
                    return True
                time.sleep(min(0.1, max(0, completion_deadline - time.monotonic())))
            else:
                time.sleep(min(0.1, max(0, trigger_deadline - time.monotonic())))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._acquiring:
                self.scope.write("ACQUIRE:STATE STOP")
        finally:
            try:
                if self.original_stop_after is not None:
                    self.scope.write(f"ACQUIRE:STOPAfter {self.original_stop_after}")
            finally:
                try:
                    self.scope.close()
                finally:
                    self.resource_manager.close()


if __name__ == "__main__":
    capture_session = TektronixI2C.start_capture(
        run_id=uuid4().hex,
        test_name="Standalone",
        iteration=1,
        standalone=True,
        scope_capture_mode="trigger",
    )
    transactions = capture_session.stop_and_save(duration_s=5)
    print(f"CSV: {capture_session.csv_path}")
    print("\nCaptured Transactions:")
    for transaction in transactions:
        print(transaction["DecodedText"])