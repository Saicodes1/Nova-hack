import httpx


async def search(query, language="en"):
    async with httpx.AsyncClient(timeout=8) as client:
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
        )
        for result in response.json().get("results", [])
    ]
