import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import time

import httpx
import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from satcore.config import ROOT

SATELLITES = {"Sentinel-2A": 40697, "Sentinel-2B": 42063, "Sentinel-2C": 60989}
TS = load.timescale(builtin=True)


async def _satellite(client, name, catalog):
    path: Path = ROOT / "var/tle" / f"{catalog}.txt"
    if path.exists() and time() - path.stat().st_mtime < 43200:
        content = path.read_text(encoding="utf-8")
    else:
        response = await client.get(
            "https://celestrak.org/NORAD/elements/gp.php",
            params={"CATNR": catalog, "FORMAT": "TLE"},
        )
        response.raise_for_status()
        content = response.text
    lines = content.strip().splitlines()
    tle = [line.strip() for line in lines if line.startswith(("1 ", "2 "))]
    if len(tle) != 2:
        raise ValueError(f"Orbital elements unavailable for {name}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return EarthSatellite(*tle, name, TS)


async def satellites(client):
    results = await asyncio.gather(
        *(_satellite(client, name, cat) for name, cat in SATELLITES.items()),
        return_exceptions=True,
    )
    available = [result for result in results if isinstance(result, EarthSatellite)]
    if not available:
        raise ValueError("Sentinel-2 orbital elements are unavailable.")
    return available


async def forecast(client, lat, lon):
    try:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "cloud_cover",
                "forecast_days": 16,
                "timezone": "UTC",
            },
        )
        response.raise_for_status()
        hours = response.json()["hourly"]
        return dict(zip(hours["time"], hours["cloud_cover"], strict=True))
    except (httpx.HTTPError, KeyError, ValueError):
        return {}


def predict(sats, lat, lon, days, clouds, start):
    count = days * 24 * 120
    moments = [start + timedelta(seconds=30 * i) for i in range(count)]
    times = TS.from_datetimes(moments)
    solar = (
        np.array([m.hour + m.minute / 60 + m.second / 3600 for m in moments]) + lon / 15
    ) % 24
    found = []
    for sat in sats:
        positions = wgs84.subpoint(sat.at(times))
        sublat, sublon = positions.latitude.degrees, positions.longitude.degrees
        a = (
            np.sin(np.radians(sublat - lat) / 2) ** 2
            + np.cos(np.radians(lat))
            * np.cos(np.radians(sublat))
            * np.sin(np.radians(sublon - lon) / 2) ** 2
        )
        distance = 12742 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        near = np.flatnonzero(
            (distance <= 145)
            & (solar >= 9.5)
            & (solar <= 11.5)
            & (np.gradient(sublat) < 0)
        )
        for group in np.split(near, np.flatnonzero(np.diff(near) != 1) + 1):
            if not len(group):
                continue
            i = group[np.argmin(distance[group])]
            moment = moments[i]
            hour = (moment + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:00")
            found.append(
                dict(
                    satellite=sat.name,
                    time_utc=moment.isoformat(),
                    cross_track_km=round(float(distance[i]), 1),
                    cloud_pct=clouds.get(hour),
                )
            )
    return sorted(found, key=lambda row: row["time_utc"])


async def passes(lat, lon, days=10):
    async with httpx.AsyncClient(timeout=10) as client:
        sats, clouds = await asyncio.gather(
            satellites(client), forecast(client, lat, lon)
        )
    return await asyncio.to_thread(
        predict, sats, lat, lon, days, clouds, datetime.now(timezone.utc)
    )


async def tracks(hours):
    async with httpx.AsyncClient(timeout=10) as client:
        sats = await satellites(client)
    start = datetime.now(timezone.utc)
    times = TS.from_datetimes(
        [start + timedelta(minutes=i) for i in range(hours * 60 + 1)]
    )
    result = {}
    for sat in sats:
        points = wgs84.subpoint(sat.at(times))
        lines = [[]]
        for lon, lat in zip(
            points.longitude.degrees, points.latitude.degrees, strict=True
        ):
            if lines[-1] and abs(lines[-1][-1][0] - lon) > 180:
                lines.append([])
            lines[-1].append([float(lon), float(lat)])
        result[sat.name] = [line for line in lines if len(line) >= 2]
    return result
