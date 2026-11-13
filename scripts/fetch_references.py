"""Build evaluation references from independent public data (network needed).

    python -m scripts.fetch_references hansen --case rondonia --years 2020-2023 --ignore-years 2001-2019,2024
    python -m scripts.fetch_references prodes --case rondonia --years 2020-2024 --out rondonia_prodes
    python -m scripts.fetch_references osm --case dubai_villas
    python -m scripts.fetch_references wfigs --case palisades_fire --fire 2025-CALFD-000738

hansen: UMD Hansen Global Forest Change loss year (GFC-2025-v1.13, ~30 m). Loss is recorded by
        calendar year; years only partly between the scene dates go in an ignore zone
        (role="ignore") that counts neither for nor against the detector.

prodes: INPE PRODES yearly deforestation increments (TerraBrasilis WFS, Legal Amazon).
        A PRODES year runs August to July, so year 2020 covers Aug 2019 - Jul 2020.
wfigs:  NIFC WFIGS interagency fire perimeter for one fire (by unique fire ID). The burned
        area inside is true change; a band along the perimeter (`--edge-m` wide) is ignored,
        since a perimeter line is only approximate at 10 m pixel scale.
osm:    OpenStreetMap landuse=residential/construction areas and building footprints
        (Overpass API), merged into one area.

Each output is clipped to the case AOI and written to config/eval_refs/<case>.geojson,
with the query and retrieval time stored in the file's `metadata` member.
"""

import argparse
import json
from datetime import datetime, timezone

import httpx
import numpy as np
import rasterio
from rasterio.features import shapes
from rasterio.windows import from_bounds
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union

from satcore.geo import project, transform, utm_epsg
from scripts.eval_patches import ROOT, load_cases

TERRABRASILIS = "https://terrabrasilis.dpi.inpe.br/geoserver/ows"
OVERPASS = "https://overpass-api.de/api/interpreter"
HANSEN_VERSION = "GFC-2025-v1.13"
HANSEN = "https://storage.googleapis.com/earthenginepartners-hansen"
WFIGS = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters/FeatureServer/0/query"
)
HEADERS = {"User-Agent": "ask-the-satellite-eval/0.1"}


def polygons(geometry):
    """Polygon parts of a clipped geometry (drops lines and points from clipping)."""
    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty else []
    return [
        part for part in getattr(geometry, "geoms", []) if isinstance(part, Polygon)
    ]


def save(case, features, metadata, name=None):
    out = ROOT / "config/eval_refs" / f"{name or case['id']}.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    collection = {
        "type": "FeatureCollection",
        "metadata": dict(
            metadata, retrieved=datetime.now(timezone.utc).isoformat(timespec="seconds")
        ),
        "features": features,
    }
    out.write_text(json.dumps(collection, indent=1) + "\n", encoding="utf-8")
    return out


def hansen_tile(lon: float, lat: float) -> str:
    """Name of the 10° Hansen tile containing a point (tiles are named by their NW corner)."""
    top = int(np.ceil(lat / 10) * 10)
    left = int(np.floor(lon / 10) * 10)
    return f"{abs(top):02d}{'N' if top >= 0 else 'S'}_{abs(left):03d}{'E' if left >= 0 else 'W'}"


def hansen(case, true_years, ignore_years, name=None):
    aoi = shape(case["aoi_model"].geometry)
    tile = hansen_tile(aoi.centroid.x, aoi.centroid.y)
    url = f"{HANSEN}/{HANSEN_VERSION}/Hansen_{HANSEN_VERSION}_lossyear_{tile}.tif"
    with (
        rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"),
        rasterio.open(f"/vsicurl/{url}") as src,
    ):
        window = from_bounds(*aoi.buffer(0.001).bounds, transform=src.transform)
        window = window.round_offsets().round_lengths()
        loss = src.read(1, window=window)
        transform = src.window_transform(window)
    features = []
    for role, years in (("true", true_years), ("ignore", ignore_years)):
        for year in years:
            mask = loss == year - 2000
            if not mask.any():
                continue
            parts = [
                shape(geom)
                for geom, _ in shapes(
                    mask.astype(np.uint8), mask=mask, transform=transform
                )
            ]
            for part in polygons(unary_union(parts).intersection(aoi)):
                props = {"source": "Hansen GFC", "year": year, "role": role}
                features.append(
                    {"type": "Feature", "properties": props, "geometry": mapping(part)}
                )
    return save(
        case,
        features,
        {
            "source": f"UMD Hansen Global Forest Change {HANSEN_VERSION} loss year",
            "url": url,
            "true_years": list(true_years),
            "ignore_years": list(ignore_years),
            "note": "Calendar-year loss; years only partly between the scene dates are role=ignore.",
            "attribution": "Hansen/UMD/Google/USGS/NASA, CC BY 4.0",
        },
        name,
    )


def wfigs(case, fire_id, edge_m=30, name=None):
    aoi = shape(case["aoi_model"].geometry)
    where = f"attr_UniqueFireIdentifier = '{fire_id}'"
    params = {"where": where, "outFields": "*", "outSR": 4326, "f": "geojson"}
    response = httpx.get(WFIGS, params=params, headers=HEADERS, timeout=120)
    response.raise_for_status()
    found = response.json().get("features", [])
    if not found:
        raise SystemExit(f"No WFIGS perimeter for fire {fire_id}.")

    def rank(feature):  # prefer a final perimeter, then the largest
        props = feature["properties"]
        final = "Final" in (props.get("poly_FeatureCategory") or "")
        return final, props.get("poly_GISAcres") or 0

    chosen = max(found, key=rank)
    props = chosen["properties"]
    perimeter = shape(chosen["geometry"]).buffer(0)
    epsg = utm_epsg(aoi.centroid.x, aoi.centroid.y)
    to_m, to_deg = project(4326, epsg), project(epsg, 4326)
    area_m, aoi_m = transform(to_m, perimeter), transform(to_m, aoi)
    band = area_m.boundary.buffer(edge_m / 2)
    pieces = (
        ("true", area_m.difference(band)),
        ("ignore", band.intersection(area_m.buffer(edge_m))),
    )
    features = []
    for role, geometry in pieces:
        for part in polygons(transform(to_deg, geometry.intersection(aoi_m))):
            features.append(
                {
                    "type": "Feature",
                    "properties": {"source": "WFIGS", "role": role, "fire": fire_id},
                    "geometry": mapping(part),
                }
            )
    return save(
        case,
        features,
        {
            "source": "NIFC WFIGS interagency fire perimeter",
            "url": WFIGS,
            "filter": where,
            "incident": props.get("poly_IncidentName"),
            "category": props.get("poly_FeatureCategory"),
            "map_method": props.get("poly_MapMethod"),
            "acres": props.get("poly_GISAcres"),
            "edge_ignore_m": edge_m,
            "note": "Perimeters include unburned islands inside them; the edge band is ignored.",
            "attribution": "National Interagency Fire Center (NIFC), WFIGS",
        },
        name,
    )


def years(text: str) -> list[int]:
    """'2020-2023' or '2019,2024' -> list of years."""
    result = []
    for part in filter(None, text.split(",")):
        first, _, last = part.partition("-")
        result.extend(range(int(first), int(last or first) + 1))
    return result


def prodes(case, first, last, name=None):
    aoi = shape(case["aoi_model"].geometry)
    west, south, east, north = aoi.bounds
    cql = f"year BETWEEN {first} AND {last} AND BBOX(geom,{west},{south},{east},{north},'EPSG:4326')"
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": "prodes-legal-amz:yearly_deforestation",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "CQL_FILTER": cql,
    }
    response = httpx.get(TERRABRASILIS, params=params, headers=HEADERS, timeout=120)
    response.raise_for_status()
    features = []
    for feature in response.json()["features"]:
        props = feature["properties"]
        clipped = shape(feature["geometry"]).buffer(0).intersection(aoi)
        for part in polygons(clipped):
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "source": "PRODES",
                        "year": props["year"],
                        "class_name": props["class_name"],
                        "image_date": props["image_date"],
                    },
                    "geometry": mapping(part),
                }
            )
    return save(
        case,
        features,
        {
            "source": "INPE PRODES yearly deforestation (TerraBrasilis), Legal Amazon",
            "url": TERRABRASILIS,
            "layer": "prodes-legal-amz:yearly_deforestation",
            "filter": cql,
            "attribution": "INPE PRODES / TerraBrasilis",
        },
        name,
    )


def osm(case):
    aoi = shape(case["aoi_model"].geometry)
    west, south, east, north = aoi.bounds
    bbox = f"{south},{west},{north},{east}"
    query = (
        "[out:json][timeout:120];("
        f'way["landuse"~"^(residential|construction)$"]({bbox});'
        f'relation["landuse"~"^(residential|construction)$"]({bbox});'
        f'way["building"]({bbox});'
        f'relation["building"]({bbox});'
        ");out geom;"
    )
    response = httpx.post(OVERPASS, data={"data": query}, headers=HEADERS, timeout=180)
    response.raise_for_status()
    shapes, kinds = [], {}
    for element in response.json()["elements"]:
        tags = element.get("tags", {})
        kind = "building" if "building" in tags else tags.get("landuse", "other")
        rings = []
        if element["type"] == "way":
            rings = [element.get("geometry", [])]
        else:  # multipolygon relation: outer rings only
            rings = [
                m.get("geometry", [])
                for m in element.get("members", [])
                if m.get("role") == "outer"
            ]
        for ring in rings:
            points = [(node["lon"], node["lat"]) for node in ring]
            if len(points) >= 4 and points[0] == points[-1]:
                polygon = Polygon(points).buffer(0)
                if not polygon.is_empty:
                    shapes.append(polygon)
                    kinds[kind] = kinds.get(kind, 0) + 1
    merged = unary_union(shapes).intersection(aoi) if shapes else MultiPolygon()
    features = [
        {"type": "Feature", "properties": {"source": "OSM"}, "geometry": mapping(part)}
        for part in polygons(merged)
    ]
    out = save(
        case,
        features,
        {
            "source": "OpenStreetMap via Overpass API",
            "url": OVERPASS,
            "query": query,
            "elements": kinds,
            "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        },
    )
    return out, kinds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=["hansen", "prodes", "osm", "wfigs"])
    parser.add_argument("--fire", help="wfigs: unique fire ID, e.g. 2025-CALFD-000738")
    parser.add_argument(
        "--edge-m", type=float, default=30, help="wfigs: ignored edge band (m)"
    )
    parser.add_argument("--case", required=True)
    parser.add_argument(
        "--years", default="2020-2024", help="true-change years, e.g. 2020-2023"
    )
    parser.add_argument("--ignore-years", default="", help="hansen: e.g. 2019,2024")
    parser.add_argument(
        "--out", help="output name in config/eval_refs/ (default: case id)"
    )
    args = parser.parse_args(argv)
    (case,) = load_cases(args.case)
    if args.source == "wfigs":
        print("wrote", wfigs(case, args.fire, args.edge_m, args.out))
    elif args.source == "hansen":
        print(
            "wrote", hansen(case, years(args.years), years(args.ignore_years), args.out)
        )
    elif args.source == "prodes":
        chosen = years(args.years)
        print("wrote", prodes(case, min(chosen), max(chosen), args.out))
    else:
        out, kinds = osm(case)
        print("wrote", out, "from", kinds)


if __name__ == "__main__":
    main()
