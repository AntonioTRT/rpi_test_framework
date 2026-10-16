"""Synchronous FNIRSI DPS-150 USB serial interface (requires pyserial).

Values use volts, amperes and watts. Instances are not thread-safe.
connect() and disconnect() do not change the output-enable setting.
Always call output_off() explicitly when the load must be de-energized.

Protocol references (reverse-engineered, not an official FNIRSI specification):
https://github.com/cho45/fnirsi-dps-150/blob/main/dps-150.js
https://github.com/cho45/fnirsi-dps-150#protocol
https://github.com/KochC/DPS-150-python-library/blob/main/dps150/constants.py
https://github.com/KochC/DPS-150-python-library/blob/main/dps150/device.py
https://github.com/svenk123/dps150tool/blob/main/dps150tool.c

Frames: header, command, field, payload length, payload, checksum.
Host header is F1; device header is F0. Checksum is the sum of the
field, length and payload bytes modulo 256. Floats are little-endian float32.
GET=A1, SET=B1; fields C1/C2 are voltage/current setpoints, DB is output
enable (0/1), C3 is measured V/I/P, DD is mode (0=CC, 1=CV).

This implementation reads the FF full-state response: float32 values at
offsets 4/8 (setpoints), 12/16/20 (actual V/I/P), and bytes 107/109
(output enabled/mode). Unknown trailing fields are not interpreted.

The C1 session-start/end and B0 baud-selector sequence follows cho45 and
KochC: field 0, one-byte values 1/0 and 5 respectively. svenk123 uses a
different baud index/end payload; firmware compatibility needs hardware
verification. No write-acknowledgement format is assumed: setters read
back state and raise if the requested value was not reported. Firmware
rounding/clamping can therefore raise after a write has taken effect.
"""

from contextlib import contextmanager
import math
import struct
import time
from typing import Iterator, TypedDict

import serial


class DPS150Error(Exception):
    """Base class for power-supply communication errors."""


class DPS150TimeoutError(DPS150Error, TimeoutError):
    """The serial operation or device response exceeded its deadline."""


class DPS150ProtocolError(DPS150Error):
    """A response was malformed or a setting could not be confirmed."""


class DPS150DisconnectedError(DPS150Error, ConnectionError):
    """The port is closed, unavailable or disconnected."""


class DPS150State(TypedDict):
    voltage_set: float
    current_set: float
    voltage_actual: float
    current_actual: float
    power_actual: float
    output_enabled: bool
    mode: str


class DPS150:
    """Control one DPS-150 on a configurable serial port.

    timeout is the response/write timeout in seconds. Calls are synchronous.
    No files are written, and connecting never automatically enables output.
    """

    def __init__(self, port: str = "/dev/ttyACM0", timeout: float = 2.0) -> None:
        if not isinstance(port, str) or not port.strip():
            raise ValueError("port must be a non-empty serial device path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        self.port = port
        self.timeout = timeout
        self._serial: serial.Serial | None = None

    @contextmanager
    def _communication(self) -> Iterator[serial.Serial]:
        connection = self._serial
        if connection is None or not connection.is_open:
            raise DPS150DisconnectedError(f"DPS-150 is not connected on {self.port}")
        try:
            yield connection
        except DPS150Error:
            raise
        except serial.SerialTimeoutException as error:
            raise DPS150TimeoutError(
                f"Serial communication timed out on {self.port}: {error}"
            ) from error
        except (serial.SerialException, OSError) as error:
            raise DPS150DisconnectedError(
                f"Serial communication failed on {self.port}: {error}"
            ) from error

    def connect(self) -> None:
        """Open 115200 8N1 with hardware flow control and verify a state response."""
        if self._serial is not None and self._serial.is_open:
            return
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
                rtscts=True,
                xonxoff=False,
                dsrdtr=False,
                exclusive=True,
            )
        except (serial.SerialException, OSError) as error:
            raise DPS150DisconnectedError(
                f"Cannot open DPS-150 on {self.port}: {error}"
            ) from error

        try:
            self._send(0xC1, 0, b"\x01")
            self._send(0xB0, 0, b"\x05")
            self.read_state()
        except DPS150Error:
            try:
                with self._communication() as connection:
                    connection.close()
            finally:
                self._serial = None
            raise

    def disconnect(self) -> None:
        """End the remote session and close the port; output is NOT switched off."""
        if self._serial is None:
            return
        try:
            if self._serial.is_open:
                try:
                    self._send(0xC1, 0, b"\x00")
                finally:
                    with self._communication() as connection:
                        connection.close()
        finally:
            self._serial = None

    def _send(self, command: int, field: int, payload: bytes) -> None:
        body = bytes((field, len(payload))) + payload
        packet = bytes((0xF1, command)) + body + bytes((sum(body) & 0xFF,))
        with self._communication() as connection:
            written = connection.write(packet)
            if written != len(packet):
                raise DPS150TimeoutError(
                    f"Short serial write on {self.port}: {written}/{len(packet)} bytes"
                )
        # dps150tool spaces commands by 50 ms to allow device processing.
        time.sleep(0.05)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        data = bytearray()
        with self._communication() as connection:
            while len(data) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DPS150TimeoutError(
                        f"Timed out waiting for DPS-150 response on {self.port} "
                        f"({len(data)}/{size} bytes in this frame section)"
                    )
                connection.timeout = remaining
                chunk = connection.read(size - len(data))
                if chunk:
                    data.extend(chunk)
        return bytes(data)

    def _query_state(self) -> bytes:
        with self._communication() as connection:
            connection.reset_input_buffer()
        self._send(0xA1, 0xFF, b"\x00")
        deadline = time.monotonic() + self.timeout

        while True:
            # Clearing a live stream may leave the tail of a previous frame.
            skipped = False
            while True:
                try:
                    start = self._read_exact(1, deadline)
                except DPS150TimeoutError as error:
                    if skipped:
                        raise DPS150ProtocolError(
                            f"Received data without a valid response header on {self.port}"
                        ) from error
                    raise
                if start == b"\xF0":
                    break
                skipped = True
            header = start + self._read_exact(3, deadline)
            response = self._read_exact(header[3] + 1, deadline)
            if (sum(header[2:]) + sum(response[:-1])) & 0xFF != response[-1]:
                raise DPS150ProtocolError(f"Invalid response checksum on {self.port}")
            # Individual measurements can be streamed between requested responses.
            if header[1:3] == b"\xA1\xFF":
                return response[:-1]

    def read_state(self) -> DPS150State:
        """Request fresh state; return setpoints, measurements, output flag and mode."""
        payload = self._query_state()
        if len(payload) < 110:
            raise DPS150ProtocolError(
                f"Full-state response is too short: {len(payload)} bytes; need >=110"
            )
        values = struct.unpack_from("<5f", payload, 4)
        if not all(math.isfinite(value) for value in values):
            raise DPS150ProtocolError("State contains non-finite setpoints or measurements")
        output, mode = payload[107], payload[109]
        if output not in (0, 1) or mode not in (0, 1):
            raise DPS150ProtocolError(
                f"Invalid state flags: output_enabled={output}, mode={mode}"
            )
        return {
            "voltage_set": values[0],
            "current_set": values[1],
            "voltage_actual": values[2],
            "current_actual": values[3],
            "power_actual": values[4],
            "output_enabled": bool(output),
            "mode": "CV" if mode else "CC",
        }

    def _set_float(self, field: int, value: float, key: str) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be finite and non-negative")
        try:
            payload = struct.pack("<f", value)
        except (OverflowError, struct.error) as error:
            raise ValueError(f"{key} cannot be represented as float32") from error
        self._send(0xB1, field, payload)
        state = self.read_state()
        actual = state["voltage_set"] if key == "voltage_set" else state["current_set"]
        if not math.isclose(actual, value, rel_tol=1e-6, abs_tol=1e-6):
            raise DPS150ProtocolError(
                f"{key} write not confirmed: requested {value}, device reported {actual}; "
                "the device may have rounded, clamped or rejected the value"
            )

    def set_voltage(self, voltage: float) -> None:
        """Set volts and verify the reported setpoint, not the measured output."""
        self._set_float(0xC1, voltage, "voltage_set")

    def set_current(self, current: float) -> None:
        """Set the current limit in amperes and verify the reported setpoint."""
        self._set_float(0xC2, current, "current_set")

    def _set_output(self, enabled: bool) -> None:
        self._send(0xB1, 0xDB, bytes((int(enabled),)))
        if self.read_state()["output_enabled"] != enabled:
            raise DPS150ProtocolError(
                f"Output {'enable' if enabled else 'disable'} was not confirmed by device"
            )

    def output_on(self) -> None:
        """Enable output and verify the reported flag."""
        self._set_output(True)

    def output_off(self) -> None:
        """Disable output and verify the reported flag."""
        self._set_output(False)

    def read_voltage(self) -> float:
        """Return fresh measured output voltage in volts."""
        return self.read_state()["voltage_actual"]

    def read_current(self) -> float:
        """Return fresh measured output current in amperes."""
        return self.read_state()["current_actual"]

    def read_power(self) -> float:
        """Return fresh measured output power in watts."""
        return self.read_state()["power_actual"]
