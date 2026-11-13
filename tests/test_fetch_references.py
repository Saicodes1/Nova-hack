from scripts.fetch_references import hansen_tile, years


def test_hansen_tiles_are_named_by_their_north_west_corner():
    assert hansen_tile(-64.1, -9.07) == "00N_070W"  # Rondônia
    assert hansen_tile(55.3, 25.1) == "30N_050E"  # Dubai
    assert hansen_tile(-118.5, 34.0) == "40N_120W"  # Los Angeles


def test_year_lists():
    assert years("2020-2023") == [2020, 2021, 2022, 2023]
    assert years("2019,2024") == [2019, 2024]
    assert years("") == []
