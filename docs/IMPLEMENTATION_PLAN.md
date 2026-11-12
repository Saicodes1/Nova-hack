# Ask the Satellite — Implementation Plan (software build)

Version 1.0 · 2026-11-12 · Scope: everything except the Jetson hardware build.

---

## 1. Product in one paragraph

A user opens the ground station, draws or searches an area (max 50 km²), picks two dates (or types a
question such as "Did the April 2024 storm flood this part of Dubai?"), and presses Ask. The ground
backend turns the question into an `AnalysisPlan` with OpenAI, sends an uplink command to the satellite
node, and streams progress stages to the UI. The node selects the clearest Sentinel-2 scenes near each
date, masks clouds, computes indices, detects change, scores confidence, and returns an `AnswerPacket`.
The ground station shows a before/after swipe, the change overlay, stats, confidence and caveats, and an
OpenAI-written explanation that is checked so it only uses numbers from the packet.

### What the Jetson Orin Nano will do later (context only — not built in this plan)

- Run the **same** `node/flight_app.py` (FastAPI) + `satcore` in a Docker container on the Jetson.
- Hold the "onboard storage": cropped scene COGs in `var/scenes/` on its SSD.
- Replace `TemplateVerdict` with `OllamaVerdict` (llama3.2:3b) for the short onboard verdict.
- Optionally run a Prithvi flood model as a second opinion.
- Report real power/temperature from `tegrastats` instead of `psutil`.
- Drive LEDs/OLED from node state events.

To switch, set `NODE_MODE=remote` and `NODE_URL=http://<jetson-ip>:8100`. Nothing else changes.
That is why every node interaction goes through the `SatelliteNode` protocol (section 7).

---

## 2. Scope

| In scope (this plan) | Out of scope (later / hardware track) |
| --- | --- |
| Change engine (`satcore`) | Jetson flashing, Docker on ARM64 |
| Local + remote node, flight API, link simulator | Ollama verdict, Prithvi, tegrastats |
| Ground API, SSE, cache, OpenAI planner/explainer/chat | LEDs, OLED, CubeSat frame |
| Orbit passes + cloud-forecast re-tasking | Sentinel-1 radar fallback (stretch) |
| React ground station (map, swipe, chat, evidence, orbit, packet) | Season-baseline for live (non-demo) areas (stretch) |
| Demo preparation + replay cache | |
| Tests, benchmarks, README | |

---

## 3. Architecture

```
┌──────────────── Ground station (laptop) ────────────────┐        ┌──── Satellite node ─────┐
│  web/ (React)  ──HTTP/SSE──▶  ground/ (FastAPI :8000)    │ uplink │ node/ (LocalNode in-proc │
│                                 │  ├─ llm/ (OpenAI)      │ ─────▶ │  or flight API :8100)    │
│                                 │  ├─ orbit.py           │        │   └─ satcore/ engine     │
│                                 │  ├─ link.py (throttle) │ ◀───── │   └─ var/scenes/ store   │
│                                 │  └─ cache (SQLite)     │ packet └──────────────────────────┘
└──────────────────────────────────────────────────────────┘
Free sources: Earth Search STAC, Planetary Computer STAC (backup), ESA WorldCover,
CelesTrak TLE, Open-Meteo forecast + geocoding, OpenFreeMap basemap.
```

### Request lifecycle (one question)

1. `POST /api/ask {question, aoi?, date_a?, date_b?, language}`
2. Ground calls OpenAI planner → `AnalysisPlan` (or returns a clarification question).
3. If no AOI: geocode `plan.place_name` (Open-Meteo) → 5 km × 5 km box around it.
4. Cache lookup by `cache_key(aoi, date_a, date_b, intent, thresholds_version)`.
5. Miss → create `analysis_id`, start background task, return `{analysis_id}` immediately.
6. Background task: `link.uplink(cmd)` → node streams `NodeEvent`s → ground re-emits as SSE.
7. Node returns `AnswerPacket` → link simulator records packet bytes and simulated transfer time.
8. Ground calls OpenAI explainer with packet JSON only → `Explanation` → number guard.
9. Store packet + explanation + timings; emit `done`.
10. UI fetches `GET /api/analyses/{id}` and image URLs; renders.

---

## 4. Repository layout and files

```
ask-the-satellite/
├── AGENTS.md
├── Makefile
├── pyproject.toml
├── .env.example
├── config/
│   ├── thresholds.yaml
│   └── demo_cases.yaml
├── satcore/
│   ├── __init__.py
│   ├── models.py          # Pydantic: AOI, SceneRef, Grid, ChangeStats, TrustReport, AnswerPacket...
│   ├── config.py          # load thresholds.yaml, THRESHOLDS_VERSION hash
│   ├── geo.py             # AOI validation, area km², UTM EPSG, Grid build
│   ├── stac.py            # search, rank, select scenes; Earth Search → PC fallback
│   ├── store.py           # local scene store (var/scenes) read/write
│   ├── load.py            # read bands onto the common Grid (WarpedVRT), scale/offset
│   ├── masks.py           # SCL masks, valid overlap, cloud-edge buffer
│   ├── indices.py         # NDVI, NDWI, MNDWI, NDBI, NBR
│   ├── change.py          # rules per intent, clean-up, polygons, stats
│   ├── landcover.py       # ESA WorldCover from→to breakdown
│   ├── trust.py           # flags, score, caveats
│   ├── render.py          # true-colour + change PNGs, 256 px WebP thumbnails
│   ├── packet.py          # build AnswerPacket, size it, raw-equivalent bytes
│   └── pipeline.py        # run_analysis(cmd, emit) orchestrates all of the above
├── node/
│   ├── __init__.py
│   ├── protocol.py        # SatelliteNode Protocol, UplinkCommand, NodeEvent, Telemetry
│   ├── local_node.py      # LocalNode: runs satcore in a worker thread
│   ├── remote_node.py     # RemoteNode: HTTP + SSE client to the flight API
│   ├── verdict.py         # VerdictWriter protocol + TemplateVerdict (OllamaVerdict later)
│   ├── telemetry.py       # psutil now; tegrastats later
│   └── flight_app.py      # FastAPI app exposing the node over HTTP (:8100)
├── ground/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, routers, CORS, static artefacts
│   ├── settings.py        # env vars (pydantic-settings)
│   ├── api/ask.py         # /api/ask, /api/analyses/*
│   ├── api/chat.py        # /api/chat
│   ├── api/orbit.py       # /api/orbit/*
│   ├── api/misc.py        # /api/geocode, /api/health, /api/telemetry, /api/demo-cases
│   ├── events.py          # in-memory event bus per analysis_id → SSE
│   ├── cache.py           # SQLite cache + analysis store
│   ├── link.py            # LinkSimulator wrapping a SatelliteNode
│   ├── orbit.py           # CelesTrak TLE, skyfield passes, Open-Meteo cloud
│   ├── geocode.py         # Open-Meteo geocoding
│   └── llm/
│       ├── client.py      # LLM wrapper (OpenAI) + FakeLLM for tests
│       ├── schemas.py     # AnalysisPlan, Explanation
│       ├── prompts.py     # system prompts
│       ├── planner.py
│       ├── explainer.py
│       ├── guard.py       # number guard
│       └── chat.py        # follow-up chat with tools
├── web/                   # Vite + React + TS
├── scripts/
│   ├── prep_demo.py
│   └── bench.py
├── tests/
└── var/                   # git-ignored
```

---

## 5. Environment and dependencies

### `.env.example`

```
OPENAI_API_KEY=
OPENAI_MODEL_FAST=          # pick from OpenAI's current model list; needs structured outputs
OPENAI_MODEL_MAIN=          # needs structured outputs + function calling
NODE_MODE=local             # local | remote
NODE_URL=http://localhost:8100
LINK_MODE=demo-fast         # demo-fast | sband-2m | uhf-9k6
STAC_PRIMARY=https://earth-search.aws.element84.com/v1
STAC_PRIMARY_COLLECTION=sentinel-2-c1-l2a
STAC_BACKUP=https://planetarycomputer.microsoft.com/api/stac/v1
STAC_BACKUP_COLLECTION=sentinel-2-l2a
SCENE_STORE=var/scenes
ANALYSES_DIR=var/analyses
CACHE_DB=var/cache.sqlite
MAX_AOI_KM2=50
```

### Python (`pyproject.toml`)

Runtime: `fastapi`, `uvicorn[standard]`, `sse-starlette`, `pydantic>=2`, `pydantic-settings`, `httpx`,
`pystac-client`, `planetary-computer`, `rasterio`, `numpy`, `scipy`, `shapely>=2`, `pyproj`,
`pillow`, `pyyaml`, `skyfield`, `openai`, `psutil`.
Dev: `pytest`, `pytest-asyncio`, `ruff`, `respx` (mock httpx).

GDAL environment for remote COG reads (set in `satcore/load.py` with `rasterio.Env`):
`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`, `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.tiff`,
`GDAL_HTTP_MULTIRANGE=YES`, `GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES`, `VSI_CACHE=TRUE`.

### Web (`web/package.json`)

`react`, `react-dom`, `maplibre-gl`, `terra-draw`, `terra-draw-maplibre-gl-adapter`, `zustand`,
`@tanstack/react-query`, `recharts`, `clsx`, `tailwindcss`. Dev: `vite`, `typescript`, `eslint`.
Swipe: implement with two synced MapLibre maps and a CSS `clip-path` (no plugin dependency).

---

## 6. `satcore` — change engine specification

### 6.1 Models (`satcore/models.py`)

```python
Intent = Literal["water_gain", "water_loss", "veg_loss", "veg_gain", "urban_growth", "burn"]

class AOI(BaseModel):
    geometry: dict            # GeoJSON Polygon, EPSG:4326
    name: str | None = None

class Grid(BaseModel):         # common pixel grid for every band and date
    crs_epsg: int              # UTM zone of AOI centroid
    bounds: tuple[float, float, float, float]   # minx, miny, maxx, maxy in crs
    resolution: float = 10.0
    width: int
    height: int

class SceneRef(BaseModel):
    role: Literal["A", "B"]
    item_ids: list[str]        # 1 item normally; >1 if mosaicked same-day tiles
    datetime: datetime
    requested_date: date
    days_from_requested: int
    aoi_cloud_pct: float       # from SCL inside AOI
    source: Literal["earth-search", "planetary-computer", "local-store"]
    asset_hrefs: dict[str, list[str]]

class UplinkCommand(BaseModel):
    analysis_id: str
    aoi: AOI
    date_a: date
    date_b: date
    intent: Intent
    window_days: int = 15
    season_baseline: bool = False

class FromTo(BaseModel):
    from_class: str
    ha: float

class ChangeStats(BaseModel):
    changed_ha: float
    changed_pct: float          # of valid analysis area
    valid_overlap_pct: float    # of AOI
    aoi_ha: float
    n_polygons: int
    largest_polygon_ha: float
    from_to: list[FromTo]
    artefact_ha: float          # change pixels flagged as cloud-edge

class TrustReport(BaseModel):
    score: int                  # 0–100
    level: Literal["High", "Medium", "Low"]
    flags: list[str]            # machine codes, e.g. "low_overlap", "season_mismatch"
    caveats: list[str]          # plain-language sentences, generated from flags
    thresholds: dict            # thresholds actually applied

class LinkReport(BaseModel):
    mode: str
    packet_bytes: int
    raw_equivalent_bytes: int
    saved_pct: float
    simulated_seconds: float    # time this packet takes at the mode's bitrate
    raw_simulated_seconds: float

class AnswerPacket(BaseModel):
    analysis_id: str
    intent: Intent
    aoi: AOI
    scenes: list[SceneRef]
    stats: ChangeStats | None   # None if refused (e.g. too cloudy)
    trust: TrustReport
    status: Literal["ok", "no_change", "refused_cloudy", "no_scenes"]
    change_geojson: dict | None # simplified polygons, EPSG:4326, props: kind, ha, artefact
    thumbnails: dict[str, str]  # "a","b","change" → base64 WebP 256 px
    image_bounds: list[list[float]]  # 4 corners lon/lat for MapLibre image source
    onboard_verdict: str
    timings_ms: dict[str, int]
    attribution: list[str]
    link: LinkReport | None = None   # filled by ground link simulator
```

### 6.2 Grid (`satcore/geo.py`)

- `validate_aoi(aoi) -> float`: polygon valid, area ≤ `MAX_AOI_KM2`, returns km² (geodesic via pyproj).
- `utm_epsg(lon, lat)`: 326xx north / 327xx south.
- `build_grid(aoi) -> Grid`: transform AOI bounds to UTM, snap to 10 m, width/height = extent / 10.
- `aoi_mask(aoi, grid) -> np.ndarray[bool]`: rasterise polygon on the grid (`rasterio.features.geometry_mask`).

### 6.3 Scene search and selection (`satcore/stac.py`)

```python
def search_candidates(aoi: AOI, target: date, window_days: int) -> list[pystac.Item]
def rank_candidates(items, aoi, grid, target) -> list[ScoredItem]   # needs SCL read per candidate
def select_scene(aoi, grid, target, role, window_days=15) -> SceneRef | None
```

1. Search `STAC_PRIMARY` collection, `intersects=aoi.geometry`, `datetime=target±window_days`,
   `query={"eo:cloud_cover": {"lt": 80}}`, `max_items=40`.
2. Group items by acquisition date + platform (same overpass). For a group, keep it if the union of
   footprints covers the AOI (shapely). One group = one candidate (mosaic same-day tiles, first valid wins).
3. Pre-sort candidates by `eo:cloud_cover` then `|days from target|`; read SCL **only** for the top 5 at
   60 m (overview level / `out_shape` downsample) to get AOI cloud %.
4. Score = `aoi_cloud_pct + 0.5 * days_from_target`. Lowest wins.
5. If best AOI cloud % > 60 or nothing found: retry with `window_days=30`. Still nothing → `None`.
6. On HTTP error / timeout (10 s) from primary: repeat with `STAC_BACKUP` (sign items with
   `planetary_computer.sign`). Map band names (see 6.4).
7. If `var/scenes/<item_id>/` exists, prefer local store (`source="local-store"`).

### 6.4 Band access and scale (`satcore/load.py`)

Band keys used by the engine and their asset names:

| Engine band | Earth Search c1 asset | Planetary Computer asset | Native res |
| --- | --- | --- | --- |
| blue | `blue` | `B02` | 10 m |
| green | `green` | `B03` | 10 m |
| red | `red` | `B04` | 10 m |
| nir | `nir` | `B08` | 10 m |
| swir16 | `swir16` | `B11` | 20 m |
| swir22 | `swir22` | `B12` | 20 m |
| scl | `scl` | `SCL` | 20 m |

```python
def read_band(hrefs: list[str], grid: Grid, resampling: Resampling) -> np.ndarray   # float32 or uint8
def to_reflectance(arr, asset_meta) -> np.ndarray
def load_scene(scene: SceneRef, grid: Grid, bands: list[str]) -> dict[str, np.ndarray]
```

- Read each href through `rasterio.vrt.WarpedVRT(src, crs=grid.crs, transform=grid_transform,
  width=grid.width, height=grid.height, resampling=...)` so every band/date lands on the same grid.
  Mosaic multiple hrefs: fill nodata pixels of the first with the second.
- Resampling: `nearest` for `scl`, `bilinear` for reflectance bands.
- Reflectance: read `raster:bands[0].scale` and `.offset` from the asset metadata; default
  `scale=0.0001, offset=0`. `refl = dn * scale + offset`; nodata (0) → NaN. **Day-0 check: print these
  for one Earth Search item and confirm.**
- Store: `store.py` writes the AOI-cropped arrays as COGs to `var/scenes/<item_id>/<band>.tif` with the
  grid in the file, and reads them back if the grid matches.

### 6.5 Masks (`satcore/masks.py`)

- Invalid SCL classes: 0 no data, 1 saturated/defective, 3 cloud shadow, 8 cloud medium, 9 cloud high,
  10 thin cirrus. 11 snow is invalid unless intent is snow (not in MVP).
- `valid_mask(scl) -> bool array`; `aoi_cloud_pct(scl, aoi_mask)`.
- `joint_valid = valid_a & valid_b & aoi_mask`; `valid_overlap_pct = joint_valid.sum() / aoi_mask.sum()`.
- `cloud_edge(scl_a, scl_b, px=2)`: binary dilation of (invalid_a | invalid_b) by 2 px → used by trust.

### 6.6 Indices (`satcore/indices.py`)

All return float32 with NaN where denominator is 0 or inputs NaN.

```
NDVI  = (nir − red)    / (nir + red)
NDWI  = (green − nir)  / (green + nir)
MNDWI = (green − swir16) / (green + swir16)
NDBI  = (swir16 − nir) / (swir16 + nir)
NBR   = (nir − swir22) / (nir + swir22)
```

### 6.7 Change rules (`satcore/change.py`) — thresholds from `config/thresholds.yaml`

```yaml
version: 1
water:        { mndwi: 0.0, ndwi_fallback: 0.0 }
veg_loss:     { ndvi_before_min: 0.4, delta_max: -0.2 }
veg_gain:     { ndvi_after_min: 0.4, delta_min: 0.2 }
urban_growth: { ndbi_delta_min: 0.1, ndvi_after_max: 0.2 }
burn:         { dnbr_min: 0.27 }
cleanup:      { opening_px: 3, min_patch_ha: 0.5, simplify_m: 10 }
trust:
  overlap_warn_pct: 70
  overlap_low_pct: 40
  date_drift_warn_days: 15
  date_drift_bad_days: 30
  season_gap_days: 45
  cloud_edge_warn_frac: 0.2
  haze_shift: 0.1
  min_change_ha: 0.5
levels: { high: 75, medium: 50 }
```

| Intent | Change pixel = | Bands needed |
| --- | --- | --- |
| water_gain | `water_b & ~water_a` where `water = MNDWI > t` | green, swir16 (+nir for NDWI fallback), scl |
| water_loss | `water_a & ~water_b` | same |
| veg_loss | `NDVI_a > 0.4 & (NDVI_b − NDVI_a) < −0.2` | red, nir, scl |
| veg_gain | `NDVI_b > 0.4 & (NDVI_b − NDVI_a) > 0.2` | red, nir, scl |
| urban_growth | `(NDBI_b − NDBI_a) > 0.1 & NDVI_b < 0.2` | red, nir, swir16, scl |
| burn | `(NBR_a − NBR_b) > 0.27` | nir, swir22, scl |

Then: `change &= joint_valid` → `binary_opening(3×3)` → label connected components → drop components
< `min_patch_ha` (50 px) → polygons via `rasterio.features.shapes` → reproject to EPSG:4326 →
`simplify(10 m in UTM before reprojecting)`. Each feature: `{kind, ha, artefact: bool}` where artefact =
>50% of its pixels inside `cloud_edge`.

Stats: `changed_ha = pixels × 0.01`, `changed_pct = change.sum() / joint_valid.sum() × 100`.

### 6.8 Land cover from→to (`satcore/landcover.py`)

- Read ESA WorldCover 2021 v200 map tile(s) covering the AOI onto the grid (nearest). Source: Planetary
  Computer STAC collection `esa-worldcover` (signed) or `s3://esa-worldcover` (unsigned, via
  `AWS_NO_SIGN_REQUEST=YES`).
- Classes: 10 Tree cover, 20 Shrubland, 30 Grassland, 40 Cropland, 50 Built-up, 60 Bare / sparse
  vegetation, 70 Snow and ice, 80 Permanent water, 90 Herbaceous wetland, 95 Mangroves, 100 Moss and lichen.
- `from_to(change_mask, wc) -> list[FromTo]`: hectares per class, sorted desc, drop < 0.1 ha.
- Also used by trust (stable classes for the haze check). If WorldCover is unavailable, return `[]` and
  add flag `no_landcover` (no score change).

### 6.9 Trust (`satcore/trust.py`)

Start at 100. Apply in order; each rule adds a flag and a caveat sentence (fill with real numbers).

| Flag | Condition | Effect | Caveat template |
| --- | --- | --- | --- |
| `low_overlap` | overlap < 70% | −25 | "Only {overlap}% of the area was clear in both images." |
| `very_low_overlap` | overlap < 40% | status `refused_cloudy`, level Low | "Too much cloud to answer reliably." |
| `date_drift` | max drift > 15 d | −10 (−20 if > 30 d) | "Used {date} instead of {requested} because of cloud." |
| `season_mismatch` | day-of-year gap > 45 d and no season baseline | −20 | "The two images are from different seasons ({mA} vs {mB})." |
| `cloud_edge` | artefact_ha / changed_ha > 0.2 | −10 | "{artefact_ha} ha of the change sits at cloud edges and may be an artefact." |
| `haze_shift` | median |Δindex| over pixels whose WorldCover class is stable (built-up, bare) > 0.1 | −15 | "The whole scene shifted; haze or lighting may affect the result." |
| `tiny_change` | changed_ha < 0.5 | status `no_change` | "No change above the 0.5 ha detection floor." |
| `resolution_note` | always | 0 | "Sentinel-2 pixels are 10 m; smaller features are not visible." |

Level: ≥75 High, 50–74 Medium, <50 Low. Clamp 0–100.

### 6.10 Render (`satcore/render.py`)

- True-colour PNG per date: RGB reflectance, clip to 2–98 percentile of the AOI (same stretch for A and B),
  gamma 1.2, transparent outside AOI. Full grid size (≤ ~1000 px a side at 50 km²).
- Change PNG: RGBA, loss = red (220,38,38,200), gain = green (22,163,74,200), artefact = grey (120,120,120,160).
- `image_bounds`: four corners of the grid in lon/lat, order top-left, top-right, bottom-right, bottom-left.
- Thumbnails: 256 px longest side WebP quality 60, base64.
- Files to `var/analyses/<analysis_id>/{a.png,b.png,change.png}`.

### 6.11 Packet (`satcore/packet.py`)

- `build_packet(...) -> AnswerPacket`, `packet_bytes(packet) = len(json.dumps(packet, separators=(",",":")))`.
- `raw_equivalent_bytes(scenes)`: sum over both scenes of the 6 band assets' full-file sizes — use STAC
  `file:size` if present, else one `HEAD` per href (cache results in `var/sizes.json`). Label in UI:
  "full scenes the satellite would otherwise downlink".

### 6.12 Pipeline (`satcore/pipeline.py`)

```python
Emit = Callable[[str, str, dict], None]   # (stage, status, detail)

def run_analysis(cmd: UplinkCommand, emit: Emit, verdict: VerdictWriter) -> AnswerPacket
```

Stages emitted in order (each `started` then `done` with `ms`): `scene_select_a`, `scene_select_b`,
`load`, `cloud_check`, `indices`, `change`, `landcover`, `trust`, `render`, `verdict`, `packet`.
If `cloud_check` finds overlap < 40% → emit `refused`, build a `refused_cloudy` packet and stop.
Total budget on local store for 25 km²: < 3 s.

---

## 7. Satellite node (`node/`)

### 7.1 Protocol (`node/protocol.py`)

```python
class NodeEvent(BaseModel):
    analysis_id: str
    stage: str
    status: Literal["started", "done", "failed", "refused"]
    ms: int | None = None
    detail: dict = {}
    packet: AnswerPacket | None = None    # only on the final event (stage="packet", status="done")

class Telemetry(BaseModel):
    mode: Literal["local", "remote"]
    state: Literal["idle", "uplink", "processing", "downlink"]
    power_w: float | None
    temp_c: float | None
    mem_used_mb: int
    last_packet_bytes: int | None

class SatelliteNode(Protocol):
    async def uplink(self, cmd: UplinkCommand) -> AsyncIterator[NodeEvent]: ...
    async def telemetry(self) -> Telemetry: ...
```

### 7.2 LocalNode
Runs `run_analysis` in `asyncio.to_thread`, bridges `emit` to an `asyncio.Queue`, yields events.
Telemetry from `psutil` (power/temp `None` on laptops without sensors).

### 7.3 Flight API (`node/flight_app.py`, port 8100) — the code that later runs on the Jetson
- `POST /uplink` (body `UplinkCommand`) → SSE stream of `NodeEvent` JSON.
- `GET /telemetry` → `Telemetry`.
- `GET /artifacts/{analysis_id}/{name}.png` → rendered images (display only, not downlink).
- `GET /health`.

### 7.4 RemoteNode
`httpx` streaming POST to `NODE_URL/uplink`, parses SSE lines into `NodeEvent`. Timeout 60 s.
On connection error raise `NodeUnavailable` so ground can report "link lost".

### 7.5 Verdict (`node/verdict.py`)
`TemplateVerdict.write(packet) -> str` e.g. "New surface water detected over 412.3 ha (8.1%); confidence
High." Max 140 chars. Later: `OllamaVerdict` with the same interface.

---

## 8. Ground backend (`ground/`)

### 8.1 Link simulator (`ground/link.py`)

| Mode | Bitrate | Real sleep |
| --- | --- | --- |
| `demo-fast` | 2 Mbps (reported) | none |
| `sband-2m` | 2 Mbps | packet_bytes×8 / 2e6 |
| `uhf-9k6` | 9 600 bps | capped at 6 s, report true value |

`uplink(cmd)` emits `uplink` stage, proxies node events, then emits `downlink` stage and fills
`packet.link = LinkReport(...)`, including `raw_simulated_seconds` for the raw-equivalent bytes.

### 8.2 Event bus + SSE (`ground/events.py`)
Per `analysis_id`: an `asyncio.Queue` list of subscribers and a replay buffer (so a late subscriber
gets all past events). SSE event names = stage; data = JSON. Keep-alive ping every 15 s.

### 8.3 Cache (`ground/cache.py`) — SQLite

```sql
CREATE TABLE analyses (
  analysis_id TEXT PRIMARY KEY, cache_key TEXT, status TEXT, question TEXT, plan_json TEXT,
  packet_json TEXT, explanation_json TEXT, events_json TEXT, created_at TEXT
);
CREATE INDEX ix_cache_key ON analyses(cache_key);
```

`cache_key = sha256(round(aoi coords, 5), date_a, date_b, intent, thresholds.version)`.
Cache hit: create a new `analysis_id` that copies the stored packet and **replays** stored events with
their original timings (UI labels them "replayed").

### 8.4 HTTP API

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| POST | `/api/ask` | `{question, aoi?, date_a?, date_b?, intent?, language}` | `{analysis_id}` or `{clarification}` |
| GET | `/api/analyses/{id}/events` | — | SSE: stages + `explain` + `done` / `error` |
| GET | `/api/analyses/{id}` | — | `{plan, packet, explanation, status}` |
| GET | `/api/analyses/{id}/images/{a\|b\|change}` | — | PNG (proxied from node artefacts) |
| POST | `/api/chat` | `{analysis_id, message, language}` | `{reply, numbers_ok, new_analysis_id?}` |
| GET | `/api/orbit/passes` | `lat, lon, days=10` | `[{satellite, time_utc, cross_track_km, cloud_pct}]` |
| GET | `/api/orbit/tracks` | `hours=3` | `{satellite: [[lon,lat],…]}` |
| GET | `/api/geocode` | `q` | `[{name, country, lat, lon}]` |
| GET | `/api/telemetry` | — | `Telemetry` |
| GET | `/api/demo-cases` | — | contents of `config/demo_cases.yaml` |
| GET | `/api/health` | — | `{node, stac_primary, openai}` reachability |

If the user supplies `aoi`, `date_a`, `date_b` and `intent` directly (form mode), skip the planner.

### 8.5 OpenAI (`ground/llm/`)

**Client.** `LLM` class with `parse(model, system, user, schema)` and `chat(model, messages, tools)`;
implement with the OpenAI Python SDK Responses API (`client.responses.parse(..., text_format=Schema)`
for structured outputs; `client.responses.create(..., tools=[...])` for chat). `FakeLLM` returns canned
objects for tests. Retry once on 429/5xx with 1 s backoff. Timeout 20 s.

**Schemas.**

```python
class AnalysisPlan(BaseModel):
    intent: Intent | None
    place_name: str | None
    date_a: date | None
    date_b: date | None
    language: Literal["en", "ar", "hi"]
    clarification: str | None   # ask the user if intent or dates can't be inferred

class Explanation(BaseModel):
    headline: str               # 1 sentence, includes the main number
    body: str                   # 3–5 sentences
    caveats: list[str]          # 1–3, taken from packet.trust.caveats, rephrased plainly
    numbers_used: list[float]
```

**Planner prompt (summary).** Today's date is {today}. Map the question to one intent. Words like
flood, inundated, lake grew → water_gain; dried, shrank → water_loss; deforestation, trees cut, forest
loss → veg_loss; greening, regrowth → veg_gain; construction, urban, built → urban_growth; fire, burn →
burn. If dates are relative ("last year", "before the storm"), resolve to ISO dates; if one date is
missing, set date_a = date_b − 1 year (same season). Never guess a place if none is given and no AOI is
drawn — ask. Output only the schema.

**Explainer prompt (summary).** You explain a satellite change-analysis packet to a non-expert. Use
only numbers that appear in the packet; round as given. Mention both acquisition dates and the
confidence level. Include the most important caveat. Never say safe, confirmed or guaranteed. If
status is `refused_cloudy` or `no_change`, say so plainly and suggest the next clear pass if provided.
Write in {language}; keep place names as given. Output only the schema.

**Number guard (`guard.py`).**
1. Extract numbers from `headline + body` with a regex that handles `1,234.5`, `8.1%`, Arabic-Indic and
   Devanagari digits (normalise to ASCII first). Ignore years 1990–2100 that match a scene/requested
   date, and ignore day-of-month numbers inside a date string.
2. Allowed set = every numeric value in the packet (recursively) plus their roundings to 0 and 1 decimal.
3. Each extracted number must be within ±0.5% relative or ±0.1 absolute of an allowed value.
4. Fail → regenerate once, telling the model which numbers were not in the packet. Fail again → use
   `template_explanation(packet, language)` (deterministic, from packet fields; English template plus
   simple Arabic/Hindi templates).

**Chat (`chat.py`).** System prompt = explainer rules + the packet JSON + previous explanation.
Tools:
- `run_analysis(aoi_name_or_current, date_a, date_b, intent)` → starts a new analysis, returns its id.
- `get_next_clear_pass()` → calls the orbit service for the current AOI centroid.
- `explain_point(lon, lat)` → reads index values at that point for both dates from the stored arrays
  (`var/analyses/<id>/indices.npz`) and returns them.
Every chat reply goes through the number guard against (packet + tool outputs).

### 8.6 Orbit and re-tasking (`ground/orbit.py`)

- TLEs: `https://celestrak.org/NORAD/elements/gp.php?CATNR={id}&FORMAT=TLE` for 40697 (S2A), 42063 (S2B),
  60989 (S2C). Cache 12 h in `var/tle/`.
- `skyfield`: propagate next `days` at 30 s steps; keep samples where the sub-satellite point is within
  145 km of the AOI centroid, local solar time 09:30–11:30 (descending daytime pass). One pass = the
  closest sample of each contiguous run.
- Cloud: Open-Meteo `https://api.open-meteo.com/v1/forecast?latitude=&longitude=&hourly=cloud_cover&forecast_days=16&timezone=UTC`;
  take the hour nearest the pass.
- `next_clear_pass` = first pass with cloud < 20%. Label everything "predicted".
- Tracks: sub-satellite points every 60 s for the next `hours`, split at the antimeridian.

### 8.7 Geocoding (`ground/geocode.py`)
`https://geocoding-api.open-meteo.com/v1/search?name={q}&count=5&language=en&format=json`. Default AOI
for a place = 5 km × 5 km square centred on it.

---

## 9. Frontend (`web/`)

### 9.1 Layout ("mission control", dark theme)

```
┌────────────────────────── TopBar: node state · link mode · power · temp · health ──────────────────────────┐
│ MapPanel (2/3 width)                                   │ SidePanel tabs: Ask | Evidence | Orbit | Link      │
│  - search box (geocode)  - draw tools  - layer toggles │                                                     │
│  - SwipeCompare (A | B) + ChangeLayer                  │                                                     │
├──────────────────────────────────── Footer: attribution ───────────────────────────────────────────────────┤
```

### 9.2 Components

| Component | Responsibility |
| --- | --- |
| `TopBar` | Polls `/api/telemetry` every 2 s; state pill (idle/uplink/processing/downlink), link mode, W, °C |
| `MapPanel` | MapLibre with OpenFreeMap style `https://tiles.openfreemap.org/styles/liberty`; Terra Draw rectangle + polygon; shows AOI km², blocks > 50 km² |
| `PlaceSearch` | `/api/geocode`; fly to result; "use 5 km box" button |
| `SwipeCompare` | Two synced maps; image source A on left, B on right; draggable divider; date labels |
| `ChangeLayer` | Image source `change.png` + GeoJSON outlines from packet; toggle; click polygon → popup (ha, kind, artefact) |
| `AskTab` | Question input, date pickers, intent override, language EN/AR/HI, suggested questions from demo cases, `StageTimeline`, `AnswerCard`, chat thread |
| `StageTimeline` | Subscribes to SSE; row per stage with ms; "replayed" badge on cache hits |
| `AnswerCard` | Headline, body, caveats, confidence pill; RTL layout for Arabic |
| `EvidenceTab` | `StatCards` (ha, %, overlap), `FromToBars` (Recharts), `ConfidenceCard` (score bar, flags, thresholds), `SceneCards` (id, time, cloud %, requested vs used) |
| `OrbitTab` | Ground tracks layer on the main map; table of next passes with cloud %; "next clear pass" highlight |
| `LinkTab` | Packet bytes vs raw-equivalent bytes, saved %, simulated seconds at current mode vs raw; mode selector |
| `Footer` | Attribution strings from packet + static basemap/weather credits |

### 9.3 State (Zustand)
`aoi`, `dates`, `intent`, `language`, `currentAnalysisId`, `stages[]`, `packet`, `explanation`,
`chat[]`, `layers {a, b, change, tracks}`, `swipeRatio`.

### 9.4 UX rules
- Never show a spinner without a stage name.
- Change overlay appears as soon as `render` is done (fetch images then), before the explanation.
- Confidence Low → answer card border amber, caveat first.
- All times shown in the browser's local time with the UTC time in a tooltip.

---

## 10. Demo preparation (`scripts/prep_demo.py`, `config/demo_cases.yaml`)

```yaml
cases:
  - id: dubai_flood
    title: "Dubai storm, April 2024"
    intent: water_gain
    aoi: null            # fill after choosing; keep <= 25 km2
    date_a: 2024-04-08   # starting guess, verify
    date_b: 2024-04-21   # first clear scene after 16 Apr 2024, verify
    question: "Did the April 2024 storm flood this area?"
  - id: dubai_cloudy
    title: "Cloudy date (trust demo)"
    intent: water_gain
    aoi: null            # same as dubai_flood
    date_a: 2024-04-08
    date_b: 2024-04-17   # pick a cloudy acquisition, verify
  - id: rondonia
    title: "Rondônia deforestation"
    intent: veg_loss
    aoi: null            # frontier block near Porto Velho
    date_a: 2019-08-01
    date_b: 2024-08-01
  - id: dubai_south
    title: "Dubai South / Expo area growth"
    intent: urban_growth
    aoi: null
    date_a: 2017-03-01
    date_b: 2025-03-01
```

Commands:

```
python scripts/prep_demo.py list   --case dubai_flood          # candidate scenes + cloud % + thumbnails to var/prep/
python scripts/prep_demo.py pin    --case dubai_flood --a <item_id> --b <item_id>
python scripts/prep_demo.py fetch  --case dubai_flood          # crop bands to var/scenes/, WorldCover too
python scripts/prep_demo.py run    --case dubai_flood          # full pipeline + explanations en/ar/hi → cache
python scripts/prep_demo.py verify                             # re-run all, compare headline numbers within 2%
```

`pin` writes chosen item IDs back into the YAML. `verify` is part of the pre-demo checklist.

---

## 11. Testing and benchmarks

| Test file | Covers |
| --- | --- |
| `tests/test_geo.py` | AOI area, UTM zone, grid size, >50 km² rejected |
| `tests/test_indices.py` | Formulas on tiny arrays, NaN handling |
| `tests/test_masks.py` | SCL classes, overlap %, cloud-edge dilation |
| `tests/test_change.py` | Each intent on synthetic before/after arrays; min patch; A/B swap flips gain/loss with same ha |
| `tests/test_trust.py` | Each flag fires at its threshold; level boundaries 49/50/74/75 |
| `tests/test_packet.py` | Schema round-trip, bytes computed, thumbnails decodable |
| `tests/test_stac_rank.py` | Ranking with fixture items (JSON in `tests/fixtures/`) |
| `tests/test_guard.py` | Passes packet numbers; catches a planted number; Arabic-Indic digits |
| `tests/test_planner.py` | FakeLLM; clarification path; relative-date resolution |
| `tests/test_link.py` | Bitrate maths for each mode |
| `tests/test_orbit.py` | Pass filter with a fixture TLE and fixed time |
| `tests/test_api.py` | `/api/ask` → SSE events order → `/api/analyses/{id}` with LocalNode on fixture arrays |
| `tests/test_live.py` (`-m live`) | Earth Search search + one real analysis on a 4 km² AOI |

`scripts/bench.py`: runs each demo case 5× from local store, prints per-stage median/p95 ms, end-to-end.
Targets: first visual ≤ 5 s, full answer ≤ 15 s (median ≤ 12 s), follow-up chat ≤ 5 s.

---

## 12. Milestones and acceptance criteria

| Phase | Done when |
| --- | --- |
| P0 Scaffold | `make setup && make test` green; `make dev` serves "hello" API + blank map |
| P1 Engine core | `run_analysis` on synthetic arrays returns a valid packet; unit tests green |
| P2 Engine live | Live test: real AOI in Dubai returns scenes, stats, PNGs in < 20 s cold |
| P3 Node + link | LocalNode and flight API both stream the same events; RemoteNode works against `make flight` |
| P4 Ground API | `/api/ask` with explicit dates → SSE stages → packet stored; cache replay works |
| P5 OpenAI | Question → plan → analysis → guarded explanation in EN/AR/HI; chat follow-up works |
| P6 Orbit | Next passes + cloud % for Dubai; refusal suggests next clear pass |
| P7–P9 Web | Full flow in the browser: draw, ask, stages, swipe, change, evidence, orbit, link tabs |
| P10 Demo | 4 cases pinned, fetched, cached; `verify` passes; offline demo works with Wi-Fi off (cached explanations) |
| P11 Hardening | Bench meets targets; README with architecture, credits, attribution; error states tested |

---

## 13. Attribution strings (packet + footer)

- "Contains modified Copernicus Sentinel data {years}"
- "Imagery accessed via Element 84 Earth Search" (or "Microsoft Planetary Computer")
- "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium"
- "Basemap © OpenStreetMap contributors, OpenFreeMap"
- "Weather and geocoding by Open-Meteo.com"
- "Orbital elements from CelesTrak"
