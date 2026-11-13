"""Power and temperature readings for the flight node.

On a Jetson these come from `tegrastats`. Elsewhere the values are None,
apart from temperature when Linux exposes a thermal zone.

With NODE_SIM set (any value except 0/false/off) the readings are simulated,
for demos and development without the board. The source is then reported as
"simulated" so nothing downstream can mistake them for measurements. The
simulation follows what a Jetson Orin Nano 8 GB measured on 2026-11-12 with
llama3.2:3b loaded: 3.5 W idle, 9.7 W mean and 18.9 W peak while analysing,
42 C idle rising towards 50 C under load.
"""

import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

SIM_IDLE_W, SIM_BUSY_W, SIM_PEAK_W = 3.5, 9.7, 18.9
SIM_IDLE_C, SIM_BUSY_C = 42.0, 50.0


def simulated() -> bool:
    return os.getenv("NODE_SIM", "").lower() not in {"", "0", "false", "off", "no"}


POWER = re.compile(r"VDD_IN (\d+)mW")
TEMP = re.compile(r"(?:cpu|CPU)@([\d.]+)C")


class Hardware:
    def __init__(self):
        self.power_w: float | None = None
        self.temp_c: float | None = None
        self.source = "none"
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        if simulated():
            self.source = "simulated"
            self._sim_power, self._sim_temp = SIM_IDLE_W, SIM_IDLE_C
            self._sim_at = time.monotonic()
            return
        binary = shutil.which("tegrastats")
        if binary:
            self.source = "tegrastats"
            threading.Thread(target=self._follow, args=(binary,), daemon=True).start()

    def _follow(self, binary):
        try:
            process = subprocess.Popen(
                [binary, "--interval", "1000"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in process.stdout:
                self.parse(line)
        except OSError:
            self.source = "none"

    def parse(self, line: str):
        if match := POWER.search(line):
            self.power_w = round(int(match[1]) / 1000, 2)
        if match := TEMP.search(line):
            self.temp_c = round(float(match[1]), 1)

    def _simulate(self, busy: bool) -> tuple[float, float]:
        now = time.monotonic()
        elapsed, self._sim_at = min(now - self._sim_at, 5), now
        # Power follows the load within a second or two; the heatsink takes longer.
        self._sim_power += (
            (SIM_BUSY_W if busy else SIM_IDLE_W) - self._sim_power
        ) * min(1, elapsed / 1.5)
        self._sim_temp += ((SIM_BUSY_C if busy else SIM_IDLE_C) - self._sim_temp) * min(
            1, elapsed / 20
        )
        spread = 3.0 if busy else 0.2
        power = self._sim_power + random.uniform(-spread, spread)
        return (
            round(min(max(power, 3.0), SIM_PEAK_W), 2),
            round(self._sim_temp + random.uniform(-0.2, 0.2), 1),
        )

    def read(self, busy: bool = False) -> tuple[float | None, float | None]:
        self.start()
        if self.source == "simulated":
            return self._simulate(busy)
        if self.temp_c is None:
            for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
                try:
                    value = int(zone.read_text().strip()) / 1000
                except (OSError, ValueError):
                    continue
                if 0 < value < 120:
                    return self.power_w, round(value, 1)
        return self.power_w, self.temp_c


hardware = Hardware()
