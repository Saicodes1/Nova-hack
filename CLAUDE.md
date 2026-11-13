# Ask the Satellite

Hackathon prototype. A user picks an area (≤ 50 km²), two dates and a question. A "satellite node"
compares two Sentinel-2 scenes and returns a compact answer packet (change patches, hectares,
confidence score, caveats). A ground station (FastAPI + React/MapLibre) streams progress, shows a
before/after swipe, and uses OpenAI to plan the question and write the explanation.

## Layout

- `satcore/`: change engine (scene search, masks, indices, seed-and-grow detection, trust, render).
  Pure Python, will run on a Jetson Orin Nano.
- `node/`: satellite node. `LocalNode` runs in-process; `flight_app.py` is the HTTP node (port 8100).
- `ground/`: FastAPI ground station (port 8000): SSE events, SQLite cache, link simulator, orbit, `llm/`.
- `web/`: React + MapLibre frontend, built into `web/dist/` and served by the ground station.
- `config/thresholds.yaml`: every detection and trust threshold. `config/demo_cases.yaml`: demos.
- `scripts/`: model setup, benchmarks, model comparisons. `docs/`: plans.
- `var/` (gitignored): analyses, scene cache, SQLite cache, models, logs.

## Hard rules

1. **Free data only.** Sentinel-2 via Element 84 Earth Search (primary) or Planetary Computer
   (backup), ESA WorldCover, CelesTrak, Open-Meteo, OpenFreeMap. OpenAI is the only paid service.
2. **The LLM never produces numbers.** Every figure shown comes from the packet. Keep the number guard
   (`ground/llm/guard.py`) in every LLM path.
3. **Thresholds live in `config/thresholds.yaml`.** Never hard-code one in Python. Bump `version`
   whenever a detection setting changes, so cached results are recomputed.
4. **`satcore/` stays pure Python** (numpy/scipy/rasterio/shapely). No FastAPI, OpenAI or web code.
   It must run headless on ARM64.
5. **Tests run offline.** Network tests are marked `@pytest.mark.live`. Use synthetic arrays or saved
   scenes from `var/scenes/` otherwise.
6. **No secrets in code or logs.** `.env` stays out of git; never print the API key.
7. **Do not regress accuracy.** Urban-growth baseline on the Dubai case: ≈154 ha in ≈10 patches over
   the new villa district (thresholds v5). Revert any change that lowers district coverage or brings
   back scattered desert patches.
8. **Ask first** before deleting files, rewriting git history, or changing more than 3 thresholds at once.

## Commands (macOS)

```bash
make setup    # create .venv if missing, install Python deps (+dev), npm ci in web/
make check    # pytest, ruff, web build: must pass before every commit
make dev      # build web, run ground station on http://127.0.0.1:8000
make flight   # run the HTTP node on :8100 (then NODE_MODE=remote NODE_URL=http://127.0.0.1:8100)
make test-live  # tests that need public data services
python -m scripts.eval_patches fetch|run|report [--case ID]  # accuracy harness (run/report offline)
```

Accuracy cases: `config/eval_cases.yaml` (pinned scenes), AOIs in `config/eval_aois/`, hand-drawn
references in `config/eval_refs/`. Outputs go to `var/eval/` (`report.html`); the committed
summary is `docs/eval_baseline.md`. Re-run all cases after any detection change.

`run_local.command` builds the web app and starts the server, logging to `var/logs/`.
Configuration comes from `.env` (copy `.env.example`); OpenAI is optional when the AOI, dates and
analysis type are given explicitly.

## Detection in one paragraph

Each intent has a strict *seed* rule and a looser *grow* rule (`satcore/change.py`: `water_change`,
`vegetation_change`, `urban_evidence`, `burn_change`); `grow_from_seeds` keeps grown regions that
contain a seed. Pixels within the cloud buffer are skipped, then patches get gap closing, speckle
removal, hole filling and a minimum patch size (`cleanup` in the thresholds file).

## Workflow

- Work one phase at a time from `docs/CLAUDE_CODE_PROMPT.md`; plan first and wait for approval.
- Commit each phase on `feat/<phase-name>` with a message that says what changed and why.
- Report files changed, commands run, test results and anything left undone.
