"""Compare two published Sentinel-2 change checkpoints on a saved analysis."""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import UUID

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_erosion, binary_opening, label

from ground.cache import find
from satcore import urban_model
from satcore.change import polygons
from satcore.config import ROOT, data_path
from satcore.models import AnswerPacket, Grid

BTC_DIR = ROOT / "var/models/btc"


def btc_model(directory: Path):
    from safetensors.torch import load_file
    from transformers import SwinBackbone, SwinConfig, UperNetConfig
    from transformers.models.upernet.modeling_upernet import UperNetHead

    backbone = json.loads((directory / "backbone.json").read_text(encoding="utf-8"))
    config = SwinConfig.from_dict(backbone["backbone_config"])
    encoder = SwinBackbone(config)
    decoder = UperNetHead(
        UperNetConfig(hidden_size=512, num_labels=1), encoder.channels
    )
    weights = load_file(directory / "model.safetensors")
    for module, prefix in ((encoder, "enc.backbone."), (decoder, "dec.")):
        state = {
            key[len(prefix) :]: value
            for key, value in weights.items()
            if key.startswith(prefix)
        }
        module.load_state_dict(state, strict=True)
        module.eval()
    return encoder, decoder


def predict_btc(before: np.ndarray, after: np.ndarray, directory: Path) -> np.ndarray:
    """Infer aligned H×W×3 uint8 images, tiling at the OSCD96 training scale."""
    import torch
    from torch.nn.functional import interpolate

    torch.set_num_threads(min(4, torch.get_num_threads()))
    encoder, decoder = btc_model(directory)
    tile, halo = 96, 12
    stride = tile - 2 * halo
    height, width = before.shape[:2]
    if before.shape != after.shape or before.ndim != 3 or before.shape[2] != 3:
        raise ValueError("Use two aligned RGB images with the same dimensions.")
    pad = ((halo, halo + stride), (halo, halo + stride), (0, 0))
    images = [np.pad(image, pad, mode="edge") for image in (before, after)]
    mean = torch.tensor([0.485, 0.456, 0.406])[None, :, None, None]
    std = torch.tensor([0.229, 0.224, 0.225])[None, :, None, None]
    result = np.empty((height, width), dtype=np.float32)
    with torch.inference_mode():
        for y in range(0, height, stride):
            for x in range(0, width, stride):
                pair = np.stack([image[y : y + tile, x : x + tile] for image in images])
                batch = torch.from_numpy(pair).permute(0, 3, 1, 2).float() / 255
                batch = interpolate(
                    batch, (256, 256), mode="bilinear", align_corners=False
                )
                features = encoder((batch - mean) / std).feature_maps
                logits = decoder([level[:1] - level[1:] for level in features])
                logits = interpolate(
                    logits, (256, 256), mode="bilinear", align_corners=False
                )
                score = interpolate(
                    logits.sigmoid(), (tile, tile), mode="bilinear", align_corners=False
                )[0, 0]
                h, w = min(stride, height - y), min(stride, width - x)
                result[y : y + h, x : x + w] = score[
                    halo : halo + h, halo : halo + w
                ].numpy()
    return result


def saved_inputs(identifier: UUID):
    row = find(identifier)
    if row is None:
        raise ValueError("Saved analysis was not found.")
    packet = AnswerPacket.model_validate_json(row["packet_json"])
    directory = data_path("ANALYSES_DIR", "var/analyses") / row["artifact_id"]
    with np.load(directory / "indices.npz", allow_pickle=False) as stored:
        grid_json = str(stored["grid"])
        valid = stored["valid"]
    rgb, bands = [], []
    for role, scene in zip(("a", "b"), packet.scenes, strict=True):
        image = Image.open(directory / f"{role}.png").convert("RGB")
        rgb.append(np.asarray(image))
        key = hashlib.sha256(
            "|".join([*scene.item_ids, grid_json]).encode()
        ).hexdigest()
        file = data_path("SCENE_STORE", "var/scenes") / f"{key}.npz"
        with np.load(file, allow_pickle=False) as stored:
            bands.append({k: stored[k] for k in ("red", "green", "blue")})
    return packet, Grid.model_validate_json(grid_json), valid, rgb, bands


def patch_labels(score, valid, cutoff, pixel_ha):
    opened = binary_opening(valid & (score >= cutoff), structure=np.ones((3, 3), bool))
    components, _ = label(opened)
    keep = np.bincount(components.ravel()) * pixel_ha >= 0.5
    keep[0] = False
    return np.where(keep[components], components, 0).astype(np.int32)


def preview(path, rgb, packet, masks):
    width, height = rgb[0].shape[1], rgb[0].shape[0]
    title_height = 30
    canvas = Image.new("RGB", (width * 2, (height + title_height) * 2), "#10151d")
    draw = ImageDraw.Draw(canvas)
    panels = [
        (f"Before · {packet.scenes[0].datetime.date()}", rgb[0]),
        (f"After · {packet.scenes[1].datetime.date()}", rgb[1]),
    ]
    for title, mask in masks:
        pixels = rgb[1].copy()
        color = np.array([51, 234, 242])
        pixels[mask] = np.rint(0.63 * pixels[mask] + 0.37 * color).astype(np.uint8)
        pixels[mask & ~binary_erosion(mask)] = color
        panels.append((title, pixels))
    for i, (title, image) in enumerate(panels):
        x, y = (i % 2) * width, (i // 2) * (height + title_height)
        draw.text((x + 10, y + 8), title, fill="#ecf2f9")
        canvas.paste(Image.fromarray(image), (x, y + title_height))
    canvas.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis_id", type=UUID)
    parser.add_argument("--weights", type=Path, default=BTC_DIR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff", type=float, default=0.5)
    args = parser.parse_args()
    if not 0 < args.cutoff <= 1:
        parser.error("--cutoff must be greater than 0 and at most 1")
    packet, grid, valid, rgb, bands = saved_inputs(args.analysis_id)
    args.output.mkdir(parents=True, exist_ok=True)
    start = perf_counter()
    fres = urban_model.predict(*bands, valid)
    if fres is None:
        raise RuntimeError(
            "Install the FresUNet model using scripts.setup_urban_model first."
        )
    fres_seconds = perf_counter() - start
    print(f"FresUNet complete in {fres_seconds:.2f}s.", flush=True)
    start = perf_counter()
    btc = predict_btc(*rgb, args.weights)
    btc_seconds = perf_counter() - start
    pixel_ha = grid.resolution**2 / 10000
    summary = {
        "analysis_id": str(args.analysis_id),
        "scene_ids": [s.item_ids for s in packet.scenes],
        "cutoff": args.cutoff,
        "clear_area_ha": round(valid.sum() * pixel_ha, 2),
    }
    masks = []
    np.savez_compressed(args.output / "scores.npz", btc=btc, fresunet=fres, valid=valid)
    for key, score, seconds in (
        ("fresunet", fres, fres_seconds),
        ("btc", btc, btc_seconds),
    ):
        patches = patch_labels(score, valid, args.cutoff, pixel_ha)
        mask = patches > 0
        geo = polygons(patches, np.zeros_like(valid), grid, "visible_change")
        summary[key] = {
            "area_ha": round(mask.sum() * pixel_ha, 2),
            "patches": len(geo["features"]),
            "seconds": round(seconds, 2),
        }
        (args.output / f"{key}.geojson").write_text(json.dumps(geo), encoding="utf-8")
        Image.fromarray(mask.astype(np.uint8) * 255).save(
            args.output / f"{key}-mask.png"
        )
        masks.append(
            (
                f"{'FresUNet' if key == 'fresunet' else 'BTC-B OSCD96'} - score cutoff {args.cutoff:g}",
                mask,
            )
        )
    preview(args.output / "comparison.png", rgb, packet, masks)
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
