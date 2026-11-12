# Ask the Satellite — implementation plan for Claude Code

Paste everything below the line into Claude Code, opened in the `Nova-hack` folder.
Work through one phase at a time. At the end of each phase, stop, report, and wait for "continue".

---

## Your role and the ground rules

You are finishing **Ask the Satellite**, a hackathon prototype. A user picks an area, two dates and a
question. A "satellite node" compares two Sentinel-2 scenes and returns a compact answer packet:
change patches, hectares, a confidence score and caveats. A ground station (FastAPI + React/MapLibre)
shows a before/after swipe and uses OpenAI to plan the question and write the explanation.

Read these files first, before changing anything:
`README.md` (especially "How change detection works"), `config/thresholds.yaml`, `satcore/change.py`,
`satcore/pipeline.py`, `satcore/masks.py`, `ground/main.py`, `web/src/main.tsx`, `web/src/MapPanel.tsx`,
`tests/test_patch_accuracy.py`.

Hard rules:
1. **Free data only.** Sentinel-2 via Element 84 Earth Search (primary) or Planetary Computer (backup),
   ESA WorldCover, CelesTrak, Open-Meteo, OpenFreeMap. OpenAI is the only paid service. Never add another.
2. **The LLM never produces numbers.** Every figure shown comes from the packet. Keep the number guard
   (`ground/llm/guard.py`) in every LLM path.
3. **Thresholds live in `config/thresholds.yaml`.** Never hard-code a threshold in Python. Bump `version`
   whenever a detection setting changes, so cached results are recomputed.
4. **`satcore/` stays pure Python** (numpy/scipy/rasterio/shapely). No FastAPI, OpenAI or web code. It must
   run headless on ARM64, because it will later run on a Jetson Orin Nano.
5. **Tests run offline.** Anything that needs the network is marked `@pytest.mark.live`. Use synthetic
   arrays or saved scenes from `var/scenes/` for everything else.
6. **No secrets in code or logs.** `.env` stays out of git, and the API key is never printed.
7. **Do not regress accuracy.** The urban-growth baseline on the Dubai case (Phase 1) is **≈154 ha in
   ≈10 patches** covering the new villa district. Any change that lowers coverage of the district or
   brings back scattered desert patches must be reverted.
8. **Before deleting files, rewriting git history, or changing more than 3 thresholds at once, ask me.**

Workflow for every phase:
- Start in plan mode: list the files you'll touch and the tests you'll add, then wait for approval.
- Use a todo list and keep it current.
- Commit at the end of the phase on branch `feat/<phase-name>`, with a message that says what changed and why.
- Finish each phase with: files changed (with line numbers), commands run, test results, screenshots or
  numbers where relevant, and anything left undone.

---

## Phase 0 — Make the current state reproducible

Goal: a clean, committed baseline that anyone on the team can run.

1. `git status`. Commit the current working tree on `main` as "Baseline: seeded-growth detection, review fixes, UI refresh".
2. Check `.gitignore` covers `.env`, `.venv/`, `var/`, `web/node_modules/`, `web/dist/`, `*.egg-info/`.
   Delete `var/transfer/` if it exists: it holds an old copy of the repo that includes `.env`.
3. Run `python -m pytest -q`, `python -m ruff check .`, `npm --prefix web run build`. Everything must pass.
4. Add a `Makefile` target `check` that runs all three.
5. Create `CLAUDE.md` at the root: a short project summary, the hard rules above, the commands, and where
   thresholds live. Keep it under 80 lines.

Acceptance: `make check` passes on a fresh clone; `CLAUDE.md` exists; working tree clean.

---

## Phase 1 — Accuracy evaluation harness (measure before tuning)

Goal: a repeatable way to see and score patches on **real** imagery for every analysis type.

1. Create `config/eval_cases.yaml`. Each case has `id`, `intent`, `aoi` (GeoJSON polygon, ≤ 25 km²),
   `date_a`, `date_b`, `note`, and optional `reference` (path to a GeoJSON of hand-drawn true-change
   polygons). Start with these candidates. **Before keeping a case, check that clear scenes exist for
   both dates** (preview the thumbnails) and replace any that don't:

   | id | intent | place / dates (verify) |
   | --- | --- | --- |
   | dubai_villas | urban_growth | The AOI and dates from the existing cached Dubai run (2019-07-10 → 2026-11-18). Baseline ≈154 ha |
   | dubai_storm | water_gain | Dubai, around the 16 April 2024 storm: early April 2024 → first clear scene after it |
   | lake_mead | water_loss | Lake Mead, Nevada: a summer 2020 → summer 2022 pair (receding shoreline) |
   | rondonia | veg_loss | Rondônia, Brazil (-63.95, -9.28): Aug 2019 → Aug 2024, same dry season |
   | farm_greening | veg_gain | A new irrigated farm or park the team knows, same season both dates |
   | palisades_fire | burn | Los Angeles Palisades fire (started 7 Jan 2025): late Dec 2024 → late Jan 2025 |
   | dubai_visible | visible_change | Same as dubai_villas (only if the FresUNet model is installed) |

2. Create `scripts/eval_patches.py`:
   - `python -m scripts.eval_patches fetch` runs scene selection and saves bands for each case into
     `var/scenes/` (reusing `satcore.store`) plus a manifest `var/eval/<case>/scenes.json`.
   - `python -m scripts.eval_patches run [--case ID]` runs **only** `satcore` (no server, no OpenAI) from
     the saved bands, reproducing `pipeline.run_analysis` masks exactly. For each case it writes:
     `var/eval/<case>/overlay.png` (before | after with filled patches | seed pixels in a second colour),
     `patches.geojson`, and `metrics.json` (hectares, patch count, largest patch, area within 30 m of
     cloud, seed share = seed pixels / patch pixels).
   - If a case has `reference`, also compute IoU, precision and recall against it, plus the share of
     reference polygons with ≥ 50% of their area covered ("object recall").
   - `python -m scripts.eval_patches report` builds `var/eval/report.html`: one row per case, with
     images, metrics and the thresholds version, so the team can review everything in a browser.
3. Draw reference polygons for at least **dubai_villas, dubai_storm and rondonia** (I'll draw them
   in geojson.io if you give me the AOI as a GeoJSON file). Save to `config/eval_refs/<case>.geojson`.
4. Add `tests/test_eval_harness.py`: run the harness on a synthetic case and check that the metrics are
   computed correctly (IoU of identical masks = 1).

Acceptance: `fetch`, `run` and `report` work. The report shows every case. dubai_villas reproduces
≈154 ha. The metrics for all cases are committed as `docs/eval_baseline.md`.

---

## Phase 2 — Per-analysis accuracy tuning

Goal: raise recall without bringing back false patches, for every analysis type, using Phase 1 numbers.

For each intent, in this order (urban growth, surface water gain, surface water loss, vegetation loss,
vegetation gain, burn-related change, visible change):

1. Look at the overlay and metrics. Write down the main failure mode: missed area, false patches,
   ragged edges, or cloud artefacts.
2. Change **only** settings for that intent in `config/thresholds.yaml`, or that intent's function
   in `satcore/change.py` (`urban_evidence`, `water_change`, `vegetation_change`, `burn_change`).
   Keep the seed-and-grow design: seeds strict, growth looser and connected to a seed.
3. Re-run `eval_patches run` for **all** cases, so a change for one intent can't silently break another.
4. Keep a change only if the target case improves (IoU or object recall up, or false patches down) and
   no other case gets worse by more than 2 IoU points.
5. Add a synthetic regression test to `tests/test_patch_accuracy.py` describing the failure you fixed.

Specific things to try:
- **Vegetation (loss and gain):** scene-wide NDVI shifts between seasons. Subtract the median ΔNDVI of
  pixels that are stable vegetation in WorldCover (tree cover, cropland) before applying thresholds.
  Fall back to no correction if fewer than 500 such pixels exist.
- **Water:** test whether seeds should also require SCL water in one of the two dates for coastal AOIs.
  Tides create false gain/loss along the shore: flag patches that are mostly WorldCover "Permanent
  water" as `tidal` and show them in grey like artefacts.
- **Burn:** add the NDVI-drop requirement to growth pixels as well if false patches appear on bare soil.
- **Urban growth:** check the Rondônia case for false urban patches (cleared forest can look grey).
  If they appear, require `ndvi_before_max` to apply at the seed level only and add a bare-soil test.
- **Per-patch confidence:** add `seed_share` and `mean_score` to every GeoJSON feature's properties.
  Show patches with seed share < 20% as dashed in the UI ("weak").

Acceptance: `docs/eval_baseline.md` updated with before/after metrics for every case. The thresholds
version is bumped. All tests pass. dubai_villas is still ≥ 150 ha with no scattered desert patches.

---

## Phase 3 — Season-aware comparison

Goal: stop seasonal greening or browning from reading as change. The packet already raises a
`season_mismatch` flag; this phase acts on it.

1. In `satcore/pipeline.py`, when `cmd.season_baseline` is true (or the two dates are > 45 days apart in
   day-of-year), find the same ±20-day window in the 2 years before date A. Compute the median index for
   those scenes, cloud-masked.
2. For vegetation and water intents, a pixel counts as change only if date B is outside the normal
   range: below the baseline minimum for loss, above the maximum for gain.
3. Remove the `season_mismatch` penalty when the baseline was used, and add the caveat "Compared against
   the same season in <years>."
4. Cache baseline scenes in `var/scenes/` like the others. Add a stage `season_baseline` to the progress
   events and to the UI pipeline.

Acceptance: on rondonia and farm_greening the seasonal false patches drop. There's a unit test with
synthetic seasonal oscillation. Stage timings stay under 15 s on cached scenes.

---

## Phase 4 — Real demo cases and offline mode

Goal: the four demo cases use **real** pinned Sentinel-2 scenes and play with Wi-Fi off.

1. Create `scripts/prep_demo.py` with the subcommands `list`, `pin`, `fetch`, `run`, `verify`:
   - `list --case ID`: candidate scenes per date with cloud % over the AOI, plus thumbnails saved to `var/prep/`.
   - `pin --case ID --a ITEM --b ITEM`: write the chosen item IDs into `config/demo_cases.yaml` (keep comments; use `ruamel.yaml`).
   - `fetch`: save the AOI-cropped bands to `var/scenes/`.
   - `run`: run the full ground flow for each case in English, Arabic and Hindi and store the results in the cache.
   - `verify`: re-run from the cache and fail if hectares differ by more than 2%.
2. Keep the synthetic demos, but label them "Synthetic" in the data-source dropdown. Real demos become the default.
3. Add `OFFLINE=1`: skip the STAC search and use pinned scenes, serve cached OpenAI explanations, and show
   "Offline demo" in the top bar. Health reports offline.

Acceptance: with the network disconnected and `OFFLINE=1`, every real demo case runs end to end in the browser.

---

## Phase 5 — UI polish for the pitch

Goal: a clean, explainable screen that a judge understands in 10 seconds.

1. **Map:**
   - Add a basemap / imagery-only toggle.
   - Pin the before/after date tags beside the swipe handle.
   - Hovering a patch shows its hectares and confidence.
   - Pressing Escape clears the selected patch.
   - Add an "All patches" button that zooms back to the full image.
2. **Answer card:**
   - Show the confidence as a 0–100 bar.
   - Show a one-line "How we know": "Seeds: N px with strong signal · grown over connected pixels with
     weaker signal · 30 m cloud buffer".
   - Link "See evidence" to the Evidence tab.
3. **Evidence tab:**
   - Add a compact "Signals" panel per analysis type, e.g. urban: colour change, texture change,
     built-up look, as median values inside the patches vs the rest of the area (from the packet; add these numbers to the packet).
   - Show the WorldCover before-class bars.
4. **Loading and empty states:** skeletons instead of blank panels. Every error message says what to do next.
5. **Arabic and Hindi:**
   - Check right-to-left layout for the answer card and chat.
   - Keep numbers left-to-right inside right-to-left text.
6. **Accessibility:**
   - Every icon button has a label.
   - Focus is visible.
   - Colour is never the only signal (patch list shows "edge" or "weak" text too).
7. **Screen sizes:** test at 1280×800 and 1920×1080 with Playwright screenshots saved to `docs/screenshots/`.

Acceptance: Playwright script `scripts/ui_screens.py` produces screenshots of the idle, running, answer,
selected-patch, Evidence and Orbit states for one real demo case. No console errors.

---

## Phase 6 — Performance and robustness

1. Create `scripts/bench.py`: run each real demo case 5× from cached scenes and print the median and 95th-percentile time per stage.
   Targets: change stage < 2 s at 25 km², whole analysis < 12 s median excluding OpenAI.
2. Handle these errors end to end, with a clear UI message and a test for each:
   - Earth Search timeout → falls back to Planetary Computer.
   - Both catalogs down → clear message.
   - Remote node lost → offers to switch to local mode.
   - OpenAI error → template explanation, with a small "offline explanation" badge.
3. Validate inputs: AOI > 50 km², date B ≤ date A, future dates, dates before 2017-03-28 (start of Sentinel-2 L2A coverage) → clear errors.
4. Add GitHub Actions CI running `make check`.

---

## Phase 7 — Jetson readiness (do not deploy, only prepare)

The same `node/flight_app.py` + `satcore` will run on a Jetson Orin Nano. Prepare it without the hardware:

1. Add `node/Dockerfile.jetson` (base: an L4T/JetPack Python image) and `node/requirements-node.txt`
   (no web or OpenAI dependencies).
2. Fill the `tegrastats` hook in `node/telemetry.py` behind an `if shutil.which("tegrastats")` check,
   parsing VDD_IN power and temperature. Keep `psutil` as the fallback.
3. Add `docs/JETSON.md`: build, run, set `NODE_MODE=remote` and `NODE_URL` on the ground station.
4. Make sure `satcore` imports nothing that fails on ARM64. Check with a dry import test that needs no GPU.

---

## Final deliverable

When all phases are done:
- `README.md` describes setup, the detection method, evaluation results (from `docs/eval_baseline.md`), and data attribution.
- `docs/PITCH_NOTES.md` has the demo script: which case, what to say, and expected numbers.
- `make check` passes and `git log` shows one commit per phase.

---

## Summary — what each phase fixes

| Phase | Problem it addresses | What fixes it |
| --- | --- | --- |
| 0 Baseline | Changes aren't committed; no single check command; an old repo copy containing `.env` is still on disk | Commit, `make check`, `CLAUDE.md`, delete `var/transfer/` |
| 1 Evaluation harness | Accuracy is judged by eye on one scene | Real cases for every analysis type, overlays, IoU / recall against hand-drawn references, HTML report |
| 2 Per-analysis tuning | Only urban growth has been tuned on real imagery | Tune each analysis type against the harness, keep only changes that improve without regressions, per-patch confidence |
| 3 Season-aware comparison | Seasonal greening or browning shows up as change | Same-season baseline from prior years for vegetation and water |
| 4 Real demos and offline | Demos use synthetic imagery and need Wi-Fi | Pinned real scenes, `prep_demo.py`, `OFFLINE=1` |
| 5 UI polish | Judges need to understand the result at a glance | Clearer map controls, "How we know", signal evidence, right-to-left checks, screenshots |
| 6 Performance and robustness | Unknown speed; failures not handled everywhere | Benchmarks with targets, fallbacks with tests, input validation, CI |
| 7 Jetson readiness | Node isn't packaged for the device | ARM64 Dockerfile, tegrastats telemetry, Jetson docs |
