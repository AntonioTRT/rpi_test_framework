import sys

if sys.platform == "win32":
    from tektronix_i2c import TektronixI2C
elif sys.platform.startswith("linux"):
    from tektronix_i2cRp import TektronixI2C
else:
    raise RuntimeError(f"Unsupported platform for Tektronix capture: {sys.platform}")

__all__ = ["TektronixI2C"]