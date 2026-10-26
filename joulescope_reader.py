import logging
import math
import sys
import time

import joulescope
import numpy as np


logger = logging.getLogger(__name__)


class JoulescopeReaderError(RuntimeError):
    """Raised when the Joulescope cannot be used for a measurement."""


class JoulescopeReader:
    """Read calibrated voltage and current measurements from the first JS320."""

    def __init__(self) -> None:
        self._device = None

    def connect(self) -> None:
        """Find and open the first available Joulescope JS320."""
        if self._device is not None:
            return

        device = None
        try:
            devices = joulescope.scan(specs="js320")
            if not devices:
                raise JoulescopeReaderError("No Joulescope JS320 device was found")

            device = devices[0]
            device.open()
        except JoulescopeReaderError:
            raise
        except Exception as error:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    logger.exception("Failed to clean up a Joulescope after open failed")
            raise JoulescopeReaderError(
                f"Failed to connect to a Joulescope JS320: {error}"
            ) from error

        self._device = device
        logger.info("Connected to Joulescope JS320 %s", device)

    def disconnect(self) -> None:
        """Stop streaming and close the connected device."""
        device = self._device
        if device is None:
            return

        self._device = None
        disconnect_error = None
        try:
            device.stop()
        except Exception as error:
            disconnect_error = error
        try:
            device.close()
        except Exception as error:
            if disconnect_error is None:
                disconnect_error = error

        if disconnect_error is not None:
            raise JoulescopeReaderError(
                f"Failed to disconnect from Joulescope JS320: {disconnect_error}"
            ) from disconnect_error
        logger.info("Disconnected from Joulescope JS320")

    def read_voltage(self, duration_s: float = 0.1) -> float:
        """Capture voltage for duration_s and return its mean in volts."""
        return self._read_mean(duration_s, column=1, signal="voltage")

    def read_current(self, duration_s: float = 0.1) -> float:
        """Capture current for duration_s and return its mean in amps."""
        return self._read_mean(duration_s, column=0, signal="current")

    def read_samples(self, duration_s: float = 0.1) -> np.ndarray:
        """Return calibrated samples with current in column 0 and voltage in column 1."""
        return self._capture_samples(duration_s, signal="samples")

    def _read_mean(self, duration_s: float, column: int, signal: str) -> float:
        samples = self._capture_samples(duration_s, signal)
        value = float(np.mean(samples[:, column]))
        logger.info("Average Joulescope %s: %g", signal, value)
        return value

    def _capture_samples(self, duration_s: float, signal: str) -> np.ndarray:
        if (
            not isinstance(duration_s, (int, float))
            or not math.isfinite(duration_s)
            or duration_s <= 0
        ):
            raise ValueError("duration_s must be a finite number greater than zero")
        if self._device is None:
            raise JoulescopeReaderError("Joulescope is not connected; call connect() first")

        logger.info("Capturing Joulescope %s for %.3f seconds", signal, duration_s)
        try:
            samples = self._device.read(contiguous_duration=duration_s)
            if samples.size == 0:
                raise JoulescopeReaderError(
                    f"Joulescope returned no samples for {signal} capture"
                )
            return samples
        except JoulescopeReaderError:
            raise
        except Exception as error:
            raise JoulescopeReaderError(
                f"Failed to read Joulescope {signal}: {error}"
            ) from error


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    reader = JoulescopeReader()
    try:
        reader.connect()
        sys.stdout.write("\033[2J\033[H")
        next_refresh = time.monotonic()
        while True:
            voltage = reader.read_voltage()
            current = reader.read_current()
            sys.stdout.write(
                "\033[H"
                "\033[2K===================================\n"
                "\033[2K         Joulescope JS320\n"
                "\033[2K===================================\n"
                f"\033[2KVoltage: {voltage:.6f} V\n"
                f"\033[2KCurrent: {current:.6f} A\n"
                "\033[2KPress Ctrl+C to exit"
            )
            sys.stdout.flush()
            next_refresh += 1.0
            time.sleep(max(0.0, next_refresh - time.monotonic()))
    except KeyboardInterrupt:
        sys.stdout.write("\nMonitor stopped.\n")
        return 0
    except JoulescopeReaderError as error:
        logger.error("%s", error)
        return 1
    finally:
        reader.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())