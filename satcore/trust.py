from datetime import date

from .config import THRESHOLDS as T
from .models import ChangeStats, SceneRef, TrustReport


def assess(
    scenes: list[SceneRef],
    overlap: float,
    stats: ChangeStats | None,
    has_landcover: bool,
    season_baseline: bool = False,
    haze: float = 0,
):
    thresholds = T["trust"]
    score, flags, caveats = 100, [], []
    status = "ok"

    def flag(code: str, message: str, penalty: int = 0) -> None:
        nonlocal score
        flags.append(code)
        caveats.append(message)
        score -= penalty

    if len(scenes) < 2:
        status = "no_scenes"
        flag(
            "no_scenes",
            "A usable scene pair could not be found for the requested dates.",
            100,
        )
    else:
        if overlap < thresholds["overlap_low_pct"]:
            status = "refused_cloudy"
            flag(
                "very_low_overlap",
                "Too much cloud or missing coverage to estimate change.",
                60,
            )
        if overlap < thresholds["overlap_warn_pct"]:
            flag(
                "low_overlap",
                f"Only {overlap}% of the area was clear in both images.",
                25,
            )
        drift = max(scenes, key=lambda scene: scene.days_from_requested)
        if drift.days_from_requested > thresholds["date_drift_warn_days"]:
            flag(
                "date_drift",
                f"Used {drift.datetime.date()} for the requested {drift.requested_date}.",
                20
                if drift.days_from_requested > thresholds["date_drift_bad_days"]
                else 10,
            )
        days = [
            date(2000, scene.datetime.month, scene.datetime.day).timetuple().tm_yday
            for scene in scenes
        ]
        gap = abs(days[0] - days[1])
        if min(gap, 366 - gap) > thresholds["season_gap_days"] and not season_baseline:
            flag(
                "season_mismatch",
                "Seasonal differences may influence this comparison.",
                20,
            )
        if not has_landcover and stats is not None:
            # Land cover is only looked up for completed analyses; a refusal
            # has no land-cover context to report.
            flag("no_landcover", "WorldCover land-cover context was unavailable.")
        if haze > thresholds["haze_shift"]:
            flag(
                "haze_shift",
                "Haze or illumination may influence the scene-wide index shift.",
                15,
            )
        if stats is not None and status == "ok":
            if (
                stats.changed_ha
                and stats.artefact_ha / stats.changed_ha
                > thresholds["cloud_edge_warn_frac"]
            ):
                flag(
                    "cloud_edge",
                    f"{stats.artefact_ha} ha of candidate change lies along cloud edges.",
                    10,
                )
            if stats.changed_ha < thresholds["min_change_ha"]:
                status = "no_change"
                flag(
                    "tiny_change",
                    f"No change above the {thresholds['min_change_ha']} ha detection floor.",
                )
    flag(
        "resolution_note",
        "Sentinel-2 uses a 10 m analysis grid. Small features may be missed.",
    )
    score = min(100, max(0, score))
    level = (
        "High"
        if score >= T["levels"]["high"]
        else "Medium"
        if score >= T["levels"]["medium"]
        else "Low"
    )
    return status, TrustReport(
        score=score, level=level, flags=flags, caveats=caveats, thresholds=T
    )
