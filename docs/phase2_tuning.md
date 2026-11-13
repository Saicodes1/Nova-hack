# Phase 2: per-analysis tuning log

Each change was kept only if `python -m scripts.eval_patches compare` passed against the
thresholds-v5 snapshot: referenced cases may not lose more than 2 IoU points, and unreferenced
cases must pass their guards in `config/eval_cases.yaml`. Rejected attempts were reverted.
Result: thresholds v5 → v6 (one kept detection change).

## Outcome per case

| case | IoU or proxy, v5 | v6 | what changed |
| --- | --- | --- | --- |
| dubai_villas | 154.31 ha, 10 patches | 154.31 ha, 10 patches | nothing (urban skipped; awaiting hand-drawn reference) |
| dubai_storm | clear-cut water detected 82.1% (192.19 ha) | 83.0% (197.92 ha) | water 2x2 speckle filter: +4.1 ha open ground, +1.6 ha built-up |
| lake_mead | clear-cut water detected 90.4% (134.81 ha) | 93.1% (140.14 ha) | water 2x2 speckle filter keeps the thin shoreline ring |
| rondonia | IoU 0.534 | IoU 0.534 | nothing kept |
| rondonia_seasonal | 1754.35 ha (should be near 0) | 1754.35 ha | nothing kept (step 4 cut it to 351 ha but was reverted) |
| farm_greening | 1393.19 ha | 1393.19 ha | nothing |
| palisades_fire | IoU 0.855 | IoU 0.855 | nothing kept |

## Kept

- **Step 0** (no detection change): `snapshot` / `compare`, proxy metrics (ha per WorldCover
  class, clear-cut water change detected), and per-patch `seed_share`, `mean_score`, `weak` in the
  patch GeoJSON. The map styling for weak patches was removed again (no frontend work in Phase 2).
- **Step 3** water-only 2x2 opening (`water.opening_px: 2`). The 3x3 opening erased 85% of Lake
  Mead's seeded shoreline ring. A cross-shaped opening would not help: it removes straight strips
  under 3 px exactly like the square.

## Reverted attempts

| step | change | compare result | why it failed / what next |
| --- | --- | --- | --- |
| 2 | `water.nir_max: 0.65` on the wet date (flood-patch NIR p95 = 0.52, p99 = 0.64) | FAIL: dubai_storm open-ground ha 169.56 → 154.15 (-9%, guard 2%); ha 192.19 → 160.14; built-up 22.43 → 5.80 ha | Roofs fell as intended, but the guard uses WorldCover "bare", which also holds new roofs not mapped in 2021, and shallow turquoise flood over bright sand reaches high NIR. Re-test against the hand-drawn storm reference. NDWI separates them better than NIR (roofs NDWI p50 0.03, flood 0.50). |
| 4 | Remove the scene-wide NDVI shift measured on WorldCover tree cover (`veg_shift`) | FAIL: rondonia IoU 0.534 → 0.500 (precision 0.663 → 0.592, recall 0.732 → 0.763) | Large win on the target: rondonia_seasonal 1754 → 351 ha. Rondônia's tree cover greened slightly (+0.038), so the correction pushed borderline pixels into loss. Suggested follow-up: correct only shifts beyond noise (e.g. abs shift > 0.05). |
| 6 | Burn growth also needs the NDVI drop (`grow_ndvi_drop_min: 0.05`) | FAIL: palisades IoU 0.855 → 0.834 (precision 0.986 → 0.994, recall 0.866 → 0.839) | It trimmed burned neighbourhoods inside the official perimeter. Side finding: 17 ha of "burn" lies on WorldCover permanent water (ocean edge); a water mask for burn would remove it without touching land. |

## Skipped by decision

Step 1 (urban, already tuned; rules give 0 ha on the forest and farm cases), step 5 (veg gain:
nothing regressed), the tidal flag (76% of Lake Mead's real retreat is WorldCover permanent water,
so the proposed rule would grey out a true answer), visible change (FresUNet not installed), and
Phase 3.

Overlays of the reverted attempts are kept locally in `var/eval/attempts/`.
