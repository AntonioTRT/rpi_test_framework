"""Optional real-time Joulescope voltage and current graphs."""

from collections import deque
import ast
import logging
import math
import queue
import threading
import time
from pathlib import Path
from typing import TypeAlias

from joulescope_reader import JoulescopeReader


logger = logging.getLogger(__name__)

GRAPH_WINDOW_SECONDS = 5.0
VOLTAGE_MIN = 0.0
VOLTAGE_MAX = 5.0
CURRENT_MIN = 0.0
CURRENT_MAX = .5
VOLTAGE_LOWER_LIMIT = 3.0
VOLTAGE_WARNING_LIMIT = 4.5
CURRENT_LOWER_LIMIT = 0.2
CURRENT_WARNING_LIMIT = 0.32
DISPLAY_SAMPLE_RATE_HZ = 200.0
CAPTURE_DURATION_SECONDS = 0.05
SAMPLE_INTERVAL_SECONDS = CAPTURE_DURATION_SECONDS
PLOT_REFRESH_MILLISECONDS = 33
MAX_BUFFERED_SAMPLES = int(GRAPH_WINDOW_SECONDS * DISPLAY_SAMPLE_RATE_HZ) + 2
LIVE_CONFIGURATION_INTERVAL_SECONDS = 1.0

Measurement: TypeAlias = tuple[float, float, float]
AcquisitionMessage: TypeAlias = Measurement | Exception | str
GraphConfiguration: TypeAlias = dict[str, float]

CONFIGURATION_NAMES = (
    "VOLTAGE_MIN",
    "VOLTAGE_MAX",
    "CURRENT_MIN",
    "CURRENT_MAX",
    "VOLTAGE_LOWER_LIMIT",
    "VOLTAGE_WARNING_LIMIT",
    "CURRENT_LOWER_LIMIT",
    "CURRENT_WARNING_LIMIT",
)


def _parse_configuration(source: str) -> GraphConfiguration:
    """Parse and validate editable numeric settings from this module's source."""
    values: GraphConfiguration = {
        name: float(globals()[name]) for name in CONFIGURATION_NAMES
    }
    module = ast.parse(source)
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value_node = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value_node = statement.value
        else:
            continue

        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in values:
                continue
            if value_node is None:
                raise ValueError(f"{target.id} must have a numeric value")
            value = ast.literal_eval(value_node)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{target.id} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"{target.id} must be finite")
            values[target.id] = float(value)

    if values["VOLTAGE_MIN"] >= values["VOLTAGE_MAX"]:
        raise ValueError("VOLTAGE_MIN must be less than VOLTAGE_MAX")
    if values["CURRENT_MIN"] >= values["CURRENT_MAX"]:
        raise ValueError("CURRENT_MIN must be less than CURRENT_MAX")
    if values["VOLTAGE_LOWER_LIMIT"] >= values["VOLTAGE_WARNING_LIMIT"]:
        raise ValueError("VOLTAGE_LOWER_LIMIT must be less than VOLTAGE_WARNING_LIMIT")
    if values["CURRENT_LOWER_LIMIT"] >= values["CURRENT_WARNING_LIMIT"]:
        raise ValueError("CURRENT_LOWER_LIMIT must be less than CURRENT_WARNING_LIMIT")
    return values


class _LiveConfiguration:
    """Reload editable settings when this source file's modification time changes."""

    def __init__(self, source_path: Path) -> None:
        self._source_path = source_path
        self.values = _parse_configuration(source_path.read_text(encoding="utf-8"))
        self._mtime_ns = source_path.stat().st_mtime_ns
        self._last_check = time.monotonic()

    def reload_if_changed(self, now: float) -> bool:
        """Reload settings at most once per second, and only after a file edit."""
        if now - self._last_check < LIVE_CONFIGURATION_INTERVAL_SECONDS:
            return False
        self._last_check = now

        try:
            modified_ns = self._source_path.stat().st_mtime_ns
        except OSError as error:
            logger.warning("Cannot check graph configuration file: %s", error)
            return False
        if modified_ns == self._mtime_ns:
            return False
        self._mtime_ns = modified_ns

        try:
            values = _parse_configuration(
                self._source_path.read_text(encoding="utf-8")
            )
        except (OSError, SyntaxError, TypeError, ValueError) as error:
            logger.warning("Ignoring invalid graph configuration edit: %s", error)
            return False

        if values == self.values:
            return False
        self.values = values
        logger.info("Reloaded graph limits and warning thresholds")
        return True


def _append_colored_segment(
    x_values: list[float],
    y_values: list[float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    """Append a connected segment, inserting a break between disjoint regions."""
    if x_values and (x_values[-1], y_values[-1]) == start:
        x_values.append(end[0])
        y_values.append(end[1])
        return
    if x_values:
        x_values.append(math.nan)
        y_values.append(math.nan)
    x_values.extend((start[0], end[0]))
    y_values.extend((start[1], end[1]))


def _range_color(value: float, lower_limit: float, warning_limit: float) -> str:
    """Return the display color for a value relative to its configured limits."""
    if value < lower_limit:
        return "gray"
    if value > warning_limit:
        return "red"
    return "blue"


def _split_trace(
    samples: deque[tuple[float, float]], lower_limit: float, warning_limit: float
) -> tuple[list[float], list[float], list[float], list[float], list[float], list[float]]:
    """Split a trace into below-range, in-range, and above-range segments."""
    points = list(samples)
    below_x: list[float] = []
    below_y: list[float] = []
    normal_x: list[float] = []
    normal_y: list[float] = []
    alarm_x: list[float] = []
    alarm_y: list[float] = []
    segments = (
        (below_x, below_y),
        (normal_x, normal_y),
        (alarm_x, alarm_y),
    )

    for (time_0, value_0), (time_1, value_1) in zip(points, points[1:]):
        split_points = [(time_0, value_0), (time_1, value_1)]
        if value_0 != value_1:
            for limit in (lower_limit, warning_limit):
                if min(value_0, value_1) < limit < max(value_0, value_1):
                    fraction = (limit - value_0) / (value_1 - value_0)
                    split_points.append(
                        (time_0 + fraction * (time_1 - time_0), limit)
                    )
        split_points.sort(key=lambda point: point[0])

        for start, end in zip(split_points, split_points[1:]):
            midpoint_value = (start[1] + end[1]) / 2.0
            if midpoint_value < lower_limit:
                target = 0
            elif midpoint_value > warning_limit:
                target = 2
            else:
                target = 1
            _append_colored_segment(*segments[target], start, end)

    return below_x, below_y, normal_x, normal_y, alarm_x, alarm_y


def _queue_latest(
    messages: queue.Queue[AcquisitionMessage], message: AcquisitionMessage
) -> None:
    """Keep the handoff queue bounded, dropping the oldest stale item if needed."""
    try:
        messages.put_nowait(message)
    except queue.Full:
        try:
            messages.get_nowait()
        except queue.Empty:
            pass
        messages.put_nowait(message)


def _acquire_measurements(
    reader: JoulescopeReader,
    messages: queue.Queue[AcquisitionMessage],
    stop_event: threading.Event,
    start_time: float,
) -> None:
    """Acquire voltage and current in the background until stopped."""
    try:
        reader.connect()
        _queue_latest(messages, "Connected: JS320")
        while not stop_event.is_set():
            cycle_start = time.monotonic()
            samples = reader.read_samples(CAPTURE_DURATION_SECONDS)
            sample_count = samples.shape[0]
            capture_end = time.monotonic()
            display_sample_count = min(
                sample_count,
                max(1, round(CAPTURE_DURATION_SECONDS * DISPLAY_SAMPLE_RATE_HZ)),
            )
            sample_denominator = max(1, sample_count - 1)
            display_denominator = max(1, display_sample_count - 1)
            for display_index in range(display_sample_count):
                sample_index = round(
                    display_index * sample_denominator / display_denominator
                )
                sample_time = (
                    capture_end
                    - CAPTURE_DURATION_SECONDS
                    + sample_index / sample_denominator * CAPTURE_DURATION_SECONDS
                )
                _queue_latest(
                    messages,
                    (
                        sample_time - start_time,
                        float(samples[sample_index, 1]),
                        float(samples[sample_index, 0]),
                    ),
                )
            stop_event.wait(
                max(0.0, SAMPLE_INTERVAL_SECONDS - (time.monotonic() - cycle_start))
            )
    except Exception as error:
        logger.exception("Joulescope acquisition stopped")
        _queue_latest(messages, error)
        stop_event.set()
    finally:
        try:
            reader.disconnect()
        except Exception as error:
            logger.exception("Failed to disconnect Joulescope")
            _queue_latest(messages, error)


def main() -> int:
    """Launch the optional live graph window."""
    try:
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except ModuleNotFoundError as error:
        if error.name == "matplotlib" or (error.name or "").startswith("matplotlib."):
            print(
                "Graphing support is not installed.\n"
                "Install with:\n\n"
                "    pip install matplotlib"
            )
            return 0
        raise

    logging.basicConfig(level=logging.WARNING)
    messages: queue.Queue[AcquisitionMessage] = queue.Queue(
        maxsize=MAX_BUFFERED_SAMPLES
    )
    stop_event = threading.Event()
    start_time = time.monotonic()
    reader = JoulescopeReader()
    configuration = _LiveConfiguration(Path(__file__).resolve())
    settings = configuration.values

    figure, (voltage_axes, current_axes) = plt.subplots(2, 1, sharex=True)
    figure.suptitle("Joulescope JS320")
    voltage_axes.set_ylabel("Voltage (V)")
    current_axes.set_ylabel("Current (A)")
    current_axes.set_xlabel("Time (s)")
    voltage_axes.set_ylim(settings["VOLTAGE_MIN"], settings["VOLTAGE_MAX"])
    current_axes.set_ylim(settings["CURRENT_MIN"], settings["CURRENT_MAX"])
    voltage_axes.set_xlim(0.0, GRAPH_WINDOW_SECONDS)
    for axes in (voltage_axes, current_axes):
        axes.set_autoscale_on(False)
    for axes in (voltage_axes, current_axes):
        axes.grid(True)

    voltage_lower_line = voltage_axes.axhline(
        settings["VOLTAGE_LOWER_LIMIT"],
        color="gray",
        linestyle="--",
        linewidth=0.8,
    )
    voltage_warning_line = voltage_axes.axhline(
        settings["VOLTAGE_WARNING_LIMIT"], color="red", linestyle="--", linewidth=0.8
    )
    current_lower_line = current_axes.axhline(
        settings["CURRENT_LOWER_LIMIT"],
        color="gray",
        linestyle="--",
        linewidth=0.8,
    )
    current_warning_line = current_axes.axhline(
        settings["CURRENT_WARNING_LIMIT"], color="red", linestyle="--", linewidth=0.8
    )
    voltage_below_line, = voltage_axes.plot(
        [], [], label="Below minimum (gray)", color="gray", linewidth=1.0
    )
    voltage_line, = voltage_axes.plot(
        [], [], label="Within limits (blue)", color="blue", linewidth=1.0
    )
    voltage_alarm_line, = voltage_axes.plot(
        [], [], label="Above warning (red)", color="red", linewidth=1.2
    )
    current_below_line, = current_axes.plot(
        [], [], label="Below minimum (gray)", color="gray", linewidth=1.0
    )
    current_line, = current_axes.plot(
        [], [], label="Within limits (blue)", color="blue", linewidth=1.0
    )
    current_alarm_line, = current_axes.plot(
        [], [], label="Above warning (red)", color="red", linewidth=1.2
    )
    voltage_axes.legend(loc="upper left")
    current_axes.legend(loc="upper left")
    voltage_value_text = voltage_axes.set_title("Voltage: -- V", loc="right")
    current_value_text = current_axes.set_title("Current: -- A", loc="right")
    voltage_data: deque[tuple[float, float]] = deque(maxlen=MAX_BUFFERED_SAMPLES)
    current_data: deque[tuple[float, float]] = deque(maxlen=MAX_BUFFERED_SAMPLES)

    def update(_frame: int) -> tuple[object, object]:
        nonlocal settings
        if configuration.reload_if_changed(time.monotonic()):
            settings = configuration.values
            voltage_axes.set_ylim(settings["VOLTAGE_MIN"], settings["VOLTAGE_MAX"])
            current_axes.set_ylim(settings["CURRENT_MIN"], settings["CURRENT_MAX"])
            voltage_lower_line.set_ydata([settings["VOLTAGE_LOWER_LIMIT"]] * 2)
            voltage_warning_line.set_ydata(
                [settings["VOLTAGE_WARNING_LIMIT"]] * 2
            )
            current_lower_line.set_ydata([settings["CURRENT_LOWER_LIMIT"]] * 2)
            current_warning_line.set_ydata(
                [settings["CURRENT_WARNING_LIMIT"]] * 2
            )

        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break

            if isinstance(message, str):
                figure.suptitle(f"Joulescope JS320 | {message}")
                continue
            if isinstance(message, Exception):
                logger.error("Joulescope graph stopped: %s", message)
                plt.close(figure)
                break

            timestamp, voltage, current = message
            voltage_data.append((timestamp, voltage))
            current_data.append((timestamp, current))

        elapsed = time.monotonic() - start_time
        cutoff = elapsed - GRAPH_WINDOW_SECONDS
        while voltage_data and voltage_data[0][0] < cutoff:
            voltage_data.popleft()
        while current_data and current_data[0][0] < cutoff:
            current_data.popleft()

        voltage_value = voltage_data[-1][1] if voltage_data else None
        current_value = current_data[-1][1] if current_data else None
        voltage_value_text.set_text(
            f"Voltage: {voltage_value:.3f} V "
            f"(Limits: {settings['VOLTAGE_LOWER_LIMIT']:.3f} - "
            f"{settings['VOLTAGE_WARNING_LIMIT']:.3f} V)"
            if voltage_value is not None
            else "Voltage: -- V"
        )
        current_value_text.set_text(
            f"Current: {current_value:.6f} A "
            f"(Limits: {settings['CURRENT_LOWER_LIMIT']:.3f} - "
            f"{settings['CURRENT_WARNING_LIMIT']:.3f} A)"
            if current_value is not None
            else "Current: -- A"
        )
        voltage_value_text.set_color(
            _range_color(
                voltage_value,
                settings["VOLTAGE_LOWER_LIMIT"],
                settings["VOLTAGE_WARNING_LIMIT"],
            )
            if voltage_value is not None
            else "blue"
        )
        current_value_text.set_color(
            _range_color(
                current_value,
                settings["CURRENT_LOWER_LIMIT"],
                settings["CURRENT_WARNING_LIMIT"],
            )
            if current_value is not None
            else "blue"
        )
        (
            voltage_below_x,
            voltage_below_y,
            voltage_normal_x,
            voltage_normal_y,
            voltage_alarm_x,
            voltage_alarm_y,
        ) = _split_trace(
            voltage_data,
            settings["VOLTAGE_LOWER_LIMIT"],
            settings["VOLTAGE_WARNING_LIMIT"],
        )
        (
            current_below_x,
            current_below_y,
            current_normal_x,
            current_normal_y,
            current_alarm_x,
            current_alarm_y,
        ) = _split_trace(
            current_data,
            settings["CURRENT_LOWER_LIMIT"],
            settings["CURRENT_WARNING_LIMIT"],
        )
        voltage_below_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in voltage_below_x],
            voltage_below_y,
        )
        voltage_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in voltage_normal_x],
            voltage_normal_y,
        )
        voltage_alarm_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in voltage_alarm_x],
            voltage_alarm_y,
        )
        current_below_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in current_below_x],
            current_below_y,
        )
        current_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in current_normal_x],
            current_normal_y,
        )
        current_alarm_line.set_data(
            [x - elapsed + GRAPH_WINDOW_SECONDS for x in current_alarm_x],
            current_alarm_y,
        )

        return (
            voltage_below_line,
            voltage_line,
            voltage_alarm_line,
            current_below_line,
            current_line,
            current_alarm_line,
        )

    worker = threading.Thread(
        target=_acquire_measurements,
        args=(reader, messages, stop_event, start_time),
        name="joulescope-acquisition",
    )
    animation = FuncAnimation(
        figure,
        update,
        interval=PLOT_REFRESH_MILLISECONDS,
        cache_frame_data=False,
    )

    try:
        worker.start()
        plt.show()
    except KeyboardInterrupt:
        logger.info("Joulescope graph interrupted")
    except Exception:
        logger.exception("Unable to run Joulescope graph")
        return 1
    finally:
        stop_event.set()
        if worker.is_alive():
            worker.join()
        del animation
        plt.close(figure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())