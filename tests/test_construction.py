import numpy as np

from satcore.change import detect


def test_visible_change_uses_model_without_urban_band_filters():
    valid = np.ones((60, 60), dtype=bool)
    valid[35:, :] = False
    score = np.zeros_like(valid, dtype=np.float32)
    score[10:25, 10:25] = 0.75
    score[40:55, 10:25] = 0.9
    result = detect({}, {}, valid, "visible_change", 0.01, model_score=score)
    assert np.count_nonzero(result) == 225


def test_pretrained_signal_adds_dry_surface_patches_and_excludes_water():
    shape = (70, 70)
    indices = {
        "ndbi": np.full(shape, 0.12),
        "ndvi": np.full(shape, 0.08),
        "mndwi": np.full(shape, -0.18),
    }
    after = {name: values.copy() for name, values in indices.items()}
    after["mndwi"][40:55, 40:55] = 0.2
    score = np.zeros(shape)
    score[15:30, 15:30] = 0.99
    score[40:55, 40:55] = 0.99
    result = detect(
        indices, after, np.ones(shape, bool), "urban_growth", 0.01, model_score=score
    )
    assert np.count_nonzero(result) == 225
    assert result[20, 20]
    assert not result[45, 45]


def test_urban_growth_detects_new_texture_without_an_ndbi_jump():
    shape = (80, 80)
    before = {
        key: np.full(shape, value, dtype=np.float32)
        for key, value in {"red": 0.30, "green": 0.24, "blue": 0.18}.items()
    }
    after = {key: band.copy() for key, band in before.items()}
    rows, columns = np.indices((30, 30))
    for band in after.values():
        band[20:50, 20:50] = np.where((rows + columns) % 2, 0.32, 0.16)
    baseline = {
        "ndbi": np.full(shape, 0.12),
        "ndvi": np.full(shape, 0.08),
        "mndwi": np.full(shape, -0.18),
    }
    valid = np.ones(shape, dtype=bool)
    result = detect(
        baseline, baseline, valid, "urban_growth", 0.01, reflectance=(before, after)
    )
    assert np.mean(result[23:47, 23:47] > 0) > 0.9
    assert not result[:12].any()
    wet = dict(baseline, mndwi=np.full(shape, 0.18))
    assert not detect(
        baseline, wet, valid, "urban_growth", 0.01, reflectance=(before, after)
    ).any()
