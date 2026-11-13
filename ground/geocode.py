"""Place search on free services.

Open-Meteo answers first: it is fast and knows towns and districts. When it
has nothing (it does not list states or regions, for example "Telangana"),
OpenStreetMap's Nominatim is asked instead.

Each result carries `broad`: true for a country, state or other region far
larger than the 50 km² the satellite analyses, so the caller can say that the
answer only covers a sample at its centre.
"""

import httpx

BROAD_CODES = ("PCL", "ADM1", "ADM2", "RGN", "CONT")
BROAD_TYPES = {"country", "state", "region", "province", "state_district", "county"}


async def _open_meteo(client, query, language):
    response = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": query, "count": 5, "language": language, "format": "json"},
    )
    response.raise_for_status()
    return [
        dict(
            name=result["name"],
            country=result.get("country", ""),
            lat=result["latitude"],
            lon=result["longitude"],
            broad=str(result.get("feature_code", "")).startswith(BROAD_CODES),
        )
        for result in response.json().get("results", [])
    ]


async def _nominatim(client, query, language):
    response = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
            "accept-language": language,
        },
        headers={"User-Agent": "ask-the-satellite-demo"},
    )
    response.raise_for_status()
    found = []
    for result in response.json():
        south, north, west, east = map(float, result["boundingbox"])
        country = result.get("address", {}).get("country", "")
        name = result.get("name") or result["display_name"].split(",")[0]
        found.append(
            dict(
                name=name,
                country="" if country == name else country,
                lat=float(result["lat"]),
                lon=float(result["lon"]),
                broad=result.get("addresstype") in BROAD_TYPES
                or max(north - south, east - west) > 0.5,
            )
        )
    return found


async def search(query, language="en"):
    async with httpx.AsyncClient(timeout=8) as client:
        found = await _open_meteo(client, query, language)
        if not found:
            try:
                found = await _nominatim(client, query, language)
            except (httpx.HTTPError, ValueError, KeyError):
                found = []
    return found
