# Codex prompts — Ask the Satellite

How to use:

1. Put `AGENTS.md` at the repo root and `docs/IMPLEMENTATION_PLAN.md` in `docs/`, then commit.
2. Paste **one prompt at a time**, in order. Review the diff, run the listed check, commit, then move on.
3. If Codex runs without internet, live tests are skipped automatically; run `make test-live` yourself.
4. Each prompt is self-contained and points Codex at the plan section it must follow.

---

## P0 — Scaffold the repo

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md (sections 4 and 5).

Create the repository scaffold exactly as in section 4:
- pyproject.toml (package name "ask-the-satellite", Python 3.11, packages satcore, node, ground) with the
  runtime and dev dependencies from section 5; pytest config with a "live" marker skipped by default
  (addopts = "-m 'not live'").
- Makefile with targets: setup, test, test-live, dev, flight, lint (see AGENTS.md).
  "dev" runs `uvicorn ground.main:app --reload --port 8000` and `npm --prefix web run dev` together.
- .env.example from section 5; .gitignore covering .env, var/, node_modules, __pycache__, .venv.
- config/thresholds.yaml with the exact YAML in section 6.7; config/demo_cases.yaml from section 10.
- Empty modules with docstrings for every file listed under satcore/, node/, ground/ (incl. ground/api and
  ground/llm), scripts/.
- ground/main.py: FastAPI app with CORS for http://localhost:5173 and GET /api/health returning {"ok": true}.
- ground/settings.py: pydantic-settings class reading every variable in .env.example.
- satcore/config.py: load thresholds.yaml, expose THRESHOLDS and THRESHOLDS_VERSION (sha256 of the file, first 8 chars).
- web/: Vite + React + TypeScript (strict) + Tailwind; dark background; App renders "Ask the Satellite"
  and a full-screen MapLibre map with the OpenFreeMap liberty style.
- tests/test_smoke.py: imports every package and checks THRESHOLDS["water"]["mndwi"] == 0.0.

Acceptance: `make setup && make test` passes; `make dev` shows the map at http://localhost:5173 and
http://localhost:8000/api/health returns {"ok": true}. Report files created and commands run.
```

---

## P1 — Engine core: models, geo, masks, indices, change, trust (offline)

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 6.1, 6.2, 6.5, 6.6, 6.7, 6.9.

Implement, with full type hints:
1. satcore/models.py — every model in 6.1 exactly (names, fields, Literals).
2. satcore/geo.py — validate_aoi (geodesic km² with pyproj.Geod, raise ValueError if > MAX_AOI_KM2),
   utm_epsg, build_grid (snap to 10 m), grid_transform(grid) -> Affine, aoi_mask(aoi, grid).
3. satcore/masks.py — INVALID_SCL = {0,1,3,8,9,10}; valid_mask, aoi_cloud_pct, joint_valid,
   valid_overlap_pct, cloud_edge(scl_a, scl_b, px=2) using scipy.ndimage.binary_dilation.
4. satcore/indices.py — ndvi, ndwi, mndwi, ndbi, nbr; float32; NaN where denominator == 0 or input NaN;
   suppress numpy warnings locally.
5. satcore/change.py —
   detect(intent, bands_a, bands_b, joint_valid, thresholds) -> bool array (rules table in 6.7);
   cleanup(mask, thresholds) -> labeled components >= min_patch_ha;
   to_polygons(mask, grid, cloud_edge_mask) -> GeoJSON FeatureCollection in EPSG:4326 with properties
   {kind: "loss"|"gain", ha, artefact}; simplify in UTM before reprojecting;
   compute_stats(change, joint_valid, aoi_mask, cloud_edge_mask) -> partial ChangeStats (from_to empty).
6. satcore/trust.py — score(...) -> TrustReport following the table in 6.9 exactly, including caveat
   sentences filled with real numbers, level boundaries from thresholds.yaml "levels".

Tests (offline, synthetic numpy arrays, 100x100 grids):
- tests/test_geo.py, test_masks.py, test_indices.py, test_change.py, test_trust.py as described in
  section 11. In test_change include: a 20x20 water block appearing in B gives water_gain of 4.0 ha;
  swapping A and B gives water_loss 4.0 ha; a 5x5 patch (0.25 ha) is removed by min_patch_ha.
- test_trust: each flag fires exactly at its threshold; levels at scores 49, 50, 74, 75.

Do not touch the network. Acceptance: `make test` green. Report coverage of each rule.
```

---

## P2 — Engine live: STAC, loading, store, land cover, render, packet, pipeline

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 6.3, 6.4, 6.8, 6.10, 6.11, 6.12.

Implement:
1. satcore/stac.py — search_candidates, group same-overpass items, coverage check with shapely,
   rank_candidates (read SCL for top 5 only, downsampled ~60 m via out_shape), select_scene with the
   ±15 → ±30 day retry, Earth Search primary and Planetary Computer backup (planetary_computer.sign),
   band-name mapping table from 6.4. 10 s timeouts. Return SceneRef with asset_hrefs per engine band.
2. satcore/load.py — read_band via rasterio WarpedVRT onto the Grid (nearest for scl, bilinear otherwise),
   multi-href mosaic, to_reflectance using raster:bands scale/offset (default 0.0001/0), GDAL env from
   section 5, load_scene(scene, grid, bands).
3. satcore/store.py — save/load AOI-cropped bands as COGs under SCENE_STORE/<item_id>/<band>.tif;
   load only if the stored grid equals the requested grid.
4. satcore/landcover.py — WorldCover 2021 via Planetary Computer STAC "esa-worldcover" (signed), fallback
   empty list + flag "no_landcover"; from_to(change_mask, wc_array).
5. satcore/render.py — true-colour PNGs (shared 2–98 percentile stretch), change PNG with the RGBA
   colours in 6.10, image_bounds corners, 256 px WebP thumbnails as base64.
6. satcore/packet.py — build_packet, packet_bytes, raw_equivalent_bytes (file:size or cached HEAD).
7. satcore/pipeline.py — run_analysis(cmd, emit, verdict) emitting the stage list in 6.12 with timings;
   refused_cloudy path when overlap < 40%; save indices for both dates to
   ANALYSES_DIR/<id>/indices.npz (float16) for later point queries.
8. A minimal TemplateVerdict in node/verdict.py (section 7.5) so the pipeline can run.

Tests:
- tests/fixtures/: 3–4 small STAC item JSONs (hand-written, realistic fields) for ranking/grouping tests.
- tests/test_stac_rank.py, tests/test_packet.py (offline).
- tests/test_pipeline_offline.py: monkeypatch select_scene/load_scene to return synthetic arrays and
  assert stage order, packet status, bytes > 0.
- tests/test_live.py (@pytest.mark.live): 2 km x 2 km AOI in Dubai (centre 55.27, 25.20), dates
  2024-04-08 and 2024-04-21, intent water_gain; assert a packet with two scenes and PNGs on disk.
Also add scripts/inspect_item.py that prints asset keys and raster:bands scale/offset for one item.

Acceptance: `make test` green offline. I will run `make test-live` and scripts/inspect_item.py myself.
Report anything you could not verify without network.
```

---

## P3 — Satellite node: protocol, LocalNode, flight API, RemoteNode

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 1 (Jetson context), 7.

This code will later run unchanged on a Jetson Orin Nano (ARM64). Keep it free of laptop assumptions.

Implement:
1. node/protocol.py — NodeEvent, Telemetry, SatelliteNode Protocol, NodeUnavailable exception.
2. node/local_node.py — LocalNode.uplink runs satcore.pipeline.run_analysis in asyncio.to_thread and
   yields NodeEvents from an asyncio.Queue fed by emit (thread-safe via loop.call_soon_threadsafe);
   tracks state idle/processing; telemetry via node/telemetry.py (psutil; power/temp None if unavailable).
3. node/telemetry.py — read_telemetry(); leave a clearly marked TODO hook for tegrastats on Jetson.
4. node/flight_app.py — FastAPI app: POST /uplink (SSE stream of NodeEvent JSON using sse-starlette),
   GET /telemetry, GET /artifacts/{analysis_id}/{name}.png, GET /health. Uses LocalNode internally.
5. node/remote_node.py — RemoteNode(NODE_URL) streaming httpx client parsing SSE into NodeEvents;
   60 s timeout; raise NodeUnavailable on connection errors.
6. ground/settings.py: add get_node() factory → LocalNode if NODE_MODE=local else RemoteNode.
7. Makefile "flight": `uvicorn node.flight_app:app --port 8100`.

Tests:
- tests/test_node.py: LocalNode with monkeypatched pipeline yields events in order and a final packet.
- tests/test_flight_app.py: TestClient POST /uplink streams the same events; RemoteNode against the app
  via httpx ASGITransport yields identical events.

Acceptance: `make test` green; `make flight` starts; curl -N POST /uplink with a fixture command streams
events. Report the exact curl command.
```

---

## P4 — Ground backend: link simulator, event bus, cache, API

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 3 (lifecycle), 8.1–8.4, 8.7.

Implement:
1. ground/link.py — LinkSimulator(node, mode) with the three modes in 8.1; emits "uplink" and "downlink"
   stages; fills packet.link (LinkReport) including raw_simulated_seconds.
2. ground/events.py — EventBus with per-analysis replay buffer and subscriber queues; SSE generator with
   15 s keep-alive pings.
3. ground/cache.py — SQLite schema in 8.3, cache_key(), save/load analysis, replay of stored events with
   original timings and a "replayed": true flag in each event detail.
4. ground/geocode.py — Open-Meteo geocoding; 5 km box helper.
5. ground/api/ask.py — POST /api/ask (form mode now: requires aoi, date_a, date_b, intent; planner comes
   in P5 — leave a call site `plan = await plan_question(...)` behind `if req.question and not
   (req.aoi and req.intent and req.date_a and req.date_b)` that raises 501 for now);
   GET /api/analyses/{id}/events (SSE), GET /api/analyses/{id}, GET /api/analyses/{id}/images/{name}
   (read from ANALYSES_DIR locally, or proxy NODE_URL/artifacts in remote mode).
6. ground/api/misc.py — /api/geocode, /api/telemetry (from node), /api/demo-cases, /api/health
   (checks node, STAC primary HEAD with 3 s timeout, OpenAI key present).
7. Background task per analysis: link.uplink → bus.publish each event → store packet → publish "done".
   Errors publish an "error" event with a user-readable message and are stored.

Tests (offline, LocalNode with monkeypatched pipeline):
- tests/test_link.py — bitrate maths.
- tests/test_api.py — POST /api/ask form mode → read SSE until done → GET packet; second identical
  request is a cache hit with replayed events.

Acceptance: `make test` green; with `make dev` running, a curl sequence (give it to me) runs a real
analysis end to end in form mode.
```

---

## P5 — OpenAI: planner, explainer, number guard, chat

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md section 8.5 carefully. The LLM must never be the source
of a number.

Implement:
1. ground/llm/client.py — LLM wrapper over the OpenAI Python SDK Responses API:
   parse(model, system, user, schema) using client.responses.parse(text_format=schema);
   chat(model, messages, tools) using client.responses.create with function tools; one retry on
   429/5xx; 20 s timeout. FakeLLM for tests (queue of canned responses). Models from settings
   OPENAI_MODEL_FAST / OPENAI_MODEL_MAIN.
2. ground/llm/schemas.py — AnalysisPlan, Explanation exactly as in 8.5.
3. ground/llm/prompts.py — PLANNER_SYSTEM and EXPLAINER_SYSTEM implementing the rules in 8.5 (include
   today's date, the intent keyword mapping, relative-date resolution, language handling, banned words).
4. ground/llm/planner.py — plan_question(question, aoi, today) -> AnalysisPlan; wire into /api/ask so a
   question alone works (geocode place_name if no AOI; return {clarification} if set).
5. ground/llm/guard.py — extract_numbers (handles 1,234.5 / 8.1% / Arabic-Indic and Devanagari digits),
   allowed_numbers(packet) recursive, check(text, packet, extra=None) -> (ok, bad_numbers).
6. ground/llm/explainer.py — explain(packet, language) -> Explanation; guard; one regeneration naming the
   bad numbers; fallback template_explanation(packet, language) with EN/AR/HI templates.
   Run after the "downlink" stage as stage "explain"; store explanation with the analysis.
7. ground/llm/chat.py + ground/api/chat.py — POST /api/chat with tools run_analysis, get_next_clear_pass
   (stub returning 501 until P6), explain_point (reads indices.npz at lon/lat → nearest pixel). Guard
   every reply against packet + tool outputs.
8. Never send images to OpenAI; send only packet JSON without thumbnails (strip base64 fields).

Tests: tests/test_guard.py, tests/test_planner.py, tests/test_explainer.py (FakeLLM: one bad number →
regenerated; two bad → template), tests/test_chat.py (tool call path).

Acceptance: `make test` green. Give me 3 example questions and the curl to try them with my key.
```

---

## P6 — Orbit passes and re-tasking

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md section 8.6.

Implement ground/orbit.py and ground/api/orbit.py:
- fetch_tle(norad_id) with 12 h file cache in var/tle/ (CelesTrak GP API, FORMAT=TLE).
- passes(lat, lon, days=10) using skyfield: 30 s steps, within 145 km of the point, local solar time
  09:30–11:30, one entry per contiguous run (closest approach), for NORAD 40697, 42063, 60989.
- cloud_at(lat, lon, times) from Open-Meteo hourly cloud_cover (16-day forecast, UTC).
- next_clear_pass(lat, lon) = first pass with cloud < 20%.
- tracks(hours=3): sub-satellite points every 60 s, split at the antimeridian.
- GET /api/orbit/passes, GET /api/orbit/tracks. Label payloads with "predicted": true.
- Wire get_next_clear_pass into chat tools and into the explainer input when status is refused_cloudy
  (add packet-adjacent field "next_clear_pass" passed to the explainer as allowed numbers/dates).

Tests: tests/test_orbit.py with a fixture TLE file and a fixed "now" (no network).

Acceptance: `make test` green; I will check /api/orbit/passes?lat=25.2&lon=55.27 live.
```

---

## P7 — Web: layout, map, drawing, search, top bar

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 9.1–9.4.

Build the shell of the ground station in web/:
- Layout from 9.1 (dark "mission control"), Tailwind, responsive down to 1280 px wide.
- Zustand store with the state in 9.3; TanStack Query client; typed API client in web/src/api.ts
  generated by hand from the Pydantic models (keep names identical).
- MapPanel: MapLibre + OpenFreeMap liberty style; Terra Draw (rectangle + polygon) via
  terra-draw-maplibre-gl-adapter; AOI area in km² (turf-free: compute with a small geodesic helper);
  block Ask if > 50 km² with a clear message.
- PlaceSearch using /api/geocode with a "use 5 km box" action.
- TopBar polling /api/telemetry every 2 s and /api/health every 30 s.
- Footer with static attribution lines (section 13).
- SidePanel tabs (Ask, Evidence, Orbit, Link) as placeholders.

Acceptance: `npm --prefix web run build` and `npm --prefix web run lint` pass; drawing an AOI shows its
area; search flies to Dubai.
```

---

## P8 — Web: ask flow, stages, swipe, change layer

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 8.4 and 9.2.

Implement:
- AskTab: question input, optional date pickers + intent select (form mode), language EN/AR/HI,
  suggested questions from /api/demo-cases (clicking one sets AOI, dates, intent and question).
- On Ask: POST /api/ask → handle {clarification} (show as an assistant message) or {analysis_id} →
  open EventSource on /api/analyses/{id}/events.
- StageTimeline: one row per stage with status icon and ms; "replayed" badge; error state.
- When the "render" stage is done (or on done), fetch images a/b/change and the packet.
- SwipeCompare: two synced MapLibre maps stacked; top map clipped with CSS clip-path by swipeRatio;
  draggable divider; image sources from image_bounds; date labels on each side.
- ChangeLayer: change.png image source + GeoJSON outlines from packet.change_geojson; click → popup with
  kind, ha, artefact flag; toggle.
- AnswerCard: headline, body, caveats, confidence pill (High green, Medium amber, Low red);
  dir="rtl" for Arabic.

Acceptance: with `make dev`, a demo-case question runs end to end in the browser; overlay appears before
the explanation.
```

---

## P9 — Web: evidence, orbit, link, chat follow-ups

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md section 9.2.

Implement:
- EvidenceTab: StatCards (changed ha, % of valid area, valid overlap %), FromToBars (Recharts horizontal
  bars, ha by WorldCover class), ConfidenceCard (score bar 0–100 with 50/75 marks, flags as chips,
  thresholds used), SceneCards (scene id, acquisition local time + UTC tooltip, AOI cloud %, requested
  vs used date, source).
- OrbitTab: fetch /api/orbit/passes for the AOI centroid; table (satellite, local time, distance km,
  forecast cloud %); highlight next clear pass; toggle ground tracks layer from /api/orbit/tracks.
  Label "predicted".
- LinkTab: packet bytes vs raw-equivalent bytes (human-readable), saved %, simulated seconds for the
  current mode vs raw; mode selector calling a new endpoint POST /api/link/mode (add it in ground,
  stores mode in memory).
- Chat thread under the AnswerCard: POST /api/chat; if the reply returns new_analysis_id, subscribe to
  its events and update the map.

Acceptance: build + lint pass; all four tabs show real data for a completed analysis.
```

---

## P10 — Demo preparation and offline replay

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md section 10.

Implement scripts/prep_demo.py with subcommands list, pin, fetch, run, verify exactly as in section 10:
- list: candidates for date_a and date_b windows (±15 days) with item id, datetime, eo:cloud_cover and
  AOI cloud %; save quick-look PNGs to var/prep/<case>/.
- pin: write chosen item ids into config/demo_cases.yaml (keep comments; use ruamel.yaml, add it to dev deps).
- fetch: crop all needed bands + WorldCover to var/scenes/ via satcore.store (and the season-baseline
  scenes: same month, 3 prior years, if season_baseline: true on the case).
- run: run the full ground flow for the case in EN, AR and HI and store in the cache.
- verify: re-run each case from the local store and compare changed_ha and changed_pct within 2% of the
  stored values; non-zero exit on mismatch.
Also add an OFFLINE=1 setting: ground serves cached explanations and skips OpenAI; health shows offline.

Acceptance: I can run list → pin → fetch → run → verify for dubai_flood; with Wi-Fi off and OFFLINE=1
the demo cases still play end to end.
```

---

## P11 — Benchmarks, hardening, README

```
Read AGENTS.md and docs/IMPLEMENTATION_PLAN.md sections 11–13.

1. scripts/bench.py: run each pinned demo case 5x from the local store through LocalNode; print a table of
   per-stage median and p95 ms and end-to-end; exit non-zero if median end-to-end > 12 s.
2. Error states end to end: node unreachable (RemoteNode) → "link lost" in UI with a button to switch to
   local mode; Earth Search timeout → backup source used and shown in SceneCards; OpenAI error →
   template explanation with a small "offline explanation" badge.
3. Input validation: AOI > 50 km², date_b <= date_a, dates in the future, dates before 2017 → clear errors.
4. README.md: what it is, architecture diagram (ASCII from the plan), setup, running, demo prep,
   data sources and attribution (section 13), credits (earthchange MIT for index/threshold ideas), and a
   "Running the node on a Jetson" section that only lists: set NODE_MODE=remote and NODE_URL, run
   `make flight` on the device.
5. Make sure `make lint` and `make test` are clean.

Acceptance: bench output pasted in your final message; README complete.
```

---

## Summary — what each prompt builds and how you know it worked

| Prompt | What it builds | What proves it works |
| --- | --- | --- |
| P0 Scaffold | Repo layout, Makefile, config files, blank API + map | `make test` green; map at :5173, health at :8000 |
| P1 Engine core | Models, grid, masks, indices, change rules, trust score (offline) | Synthetic tests: 4.0 ha water block detected; flags fire at thresholds |
| P2 Engine live | Scene search/selection, band loading, store, WorldCover, PNGs, packet, pipeline | `make test-live` returns a real Dubai packet; `inspect_item.py` confirms scale/offset |
| P3 Node | Satellite node protocol, LocalNode, flight API, RemoteNode (Jetson-ready) | Same event stream from LocalNode and from `make flight` |
| P4 Ground API | Link simulator, SSE event bus, SQLite cache, ask/analyses endpoints | Form-mode analysis end to end via curl; second call replays from cache |
| P5 OpenAI | Planner, explainer, number guard, chat with tools | A question alone produces a guarded explanation in EN/AR/HI |
| P6 Orbit | Sentinel-2 passes, cloud forecast, next clear pass, tracks | `/api/orbit/passes` for Dubai lists passes with cloud % |
| P7 Web shell | Layout, map, AOI drawing, search, telemetry bar | Draw AOI shows km²; search flies to Dubai |
| P8 Web flow | Ask flow, live stages, swipe, change overlay, answer card | Demo question runs end to end; overlay before explanation |
| P9 Web tabs | Evidence, orbit, link tabs, chat follow-ups | All tabs show real data for a finished analysis |
| P10 Demo | Pin, fetch, precompute, verify demo cases; offline mode | `verify` passes; demo plays with Wi-Fi off |
| P11 Hardening | Benchmarks, error states, validation, README | Median end-to-end ≤ 12 s; lint and tests clean |
