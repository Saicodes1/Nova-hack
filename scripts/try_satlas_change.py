"""Train a frozen Satlas Swin-Tiny encoder with a compact change-mask decoder."""

import argparse
import json
from io import BytesIO
from pathlib import Path
from time import perf_counter
from uuid import UUID

import numpy as np
import pyarrow.parquet as pq
import torch
from PIL import Image
from torch import nn
from torch.nn import functional as F
from torchvision.models import swin_v2_t

from satcore.change import polygons
from satcore.urban_model import predict as predict_fresunet
from scripts.compare_change_models import patch_labels, preview, saved_inputs

TILE = 96
HALO = 12
BANDS = (96, 192, 384, 768)
SCORES = torch.arange(0.025, 0.901, 0.025)


def resize(x, size):
    return F.interpolate(x, size=size, mode="bilinear", align_corners=False)


def encoder_from(path):
    weights = torch.load(path, map_location="cpu", weights_only=True)
    encoder = swin_v2_t(weights=None).features
    prefix = "backbone.backbone.features."
    state = {
        key.removeprefix(prefix): value
        for key, value in weights.items()
        if key.startswith(prefix)
    }
    encoder.load_state_dict(state, strict=True)
    return encoder.eval().requires_grad_(False)


class Decoder(nn.Module):
    def __init__(self):
        super().__init__()
        width = 24
        self.lateral = nn.ModuleList(
            nn.Sequential(
                nn.Conv2d(channels, width, 1), nn.GroupNorm(4, width), nn.GELU()
            )
            for channels in BANDS
        )
        self.blocks = nn.ModuleList(
            nn.Sequential(
                nn.Conv2d(width, width, 3, padding=1), nn.GroupNorm(4, width), nn.GELU()
            )
            for _ in BANDS[:-1]
        )
        self.mask = nn.Sequential(
            nn.Conv2d(width + 3, width, 3, padding=1), nn.GELU(), nn.Conv2d(width, 1, 1)
        )

    def forward(self, levels, rgb):
        state = self.lateral[-1](levels[-1])
        for i in range(len(levels) - 2, -1, -1):
            lateral = self.lateral[i](levels[i])
            state = self.blocks[i](lateral + resize(state, lateral.shape[-2:]))
        state = torch.cat((state, F.avg_pool2d(rgb, 4)), dim=1)
        return resize(self.mask(state), rgb.shape[-2:])[:, 0]


@torch.no_grad()
def features(encoder, before, after):
    joined = torch.cat((before, after)).permute(0, 3, 1, 2).float() / 255
    rgb = (joined[: len(before)] - joined[len(before) :]).abs()
    levels = []
    x = joined
    for i, layer in enumerate(encoder):
        x = layer(x)
        if i % 2:
            first, second = x.chunk(2)
            levels.append((first - second).abs().permute(0, 3, 1, 2).contiguous())
    return levels, rgb


def read_rows(file):
    table = pq.read_table(file, columns=["imageA", "imageB", "label"])
    decoded = []
    for key, mode in (("imageA", "RGB"), ("imageB", "RGB"), ("label", "L")):
        with_images = [
            np.asarray(Image.open(BytesIO(entry["bytes"])).convert(mode))
            for entry in table[key].to_pylist()
        ]
        decoded.append(torch.from_numpy(np.stack(with_images)))
    return decoded[0], decoded[1], decoded[2] >= 128


def precompute(encoder, data, name, file):
    before, after, truth = data
    if file.is_file():
        levels, rgb, *_ = torch.load(file, map_location="cpu", weights_only=True)
        if len(rgb) == len(truth):
            return levels, rgb, truth
    chunks, rgb = [], []
    for start in range(0, len(before), 16):
        levels, visible = features(
            encoder, before[start : start + 16], after[start : start + 16]
        )
        chunks.append([value.half() for value in levels])
        rgb.append(visible.half())
        if start % 160 == 0:
            print(
                f"{name} features: {min(start + 16, len(before))}/{len(before)}",
                flush=True,
            )
    result = (
        [torch.cat(stage) for stage in zip(*chunks, strict=True)],
        torch.cat(rgb),
        truth,
    )
    torch.save(result[:2], file)
    return result


def batch_outputs(model, data, indexes):
    levels, rgb, truth = data
    for ids in indexes.split(24):
        values = [level[ids].float() for level in levels]
        yield model(values, rgb[ids].float()), truth[ids]


@torch.inference_mode()
def predict(model, data, indexes):
    model.eval()
    pairs = list(batch_outputs(model, data, indexes))
    return torch.cat([x.sigmoid() for x, _ in pairs]), torch.cat([y for _, y in pairs])


def metrics(scores, truth, cutoff):
    pred = scores >= cutoff
    tp = torch.count_nonzero(pred & truth).item()
    fp = torch.count_nonzero(pred & ~truth).item()
    fn = torch.count_nonzero(~pred & truth).item()
    return {
        "precision": tp / max(1, tp + fp),
        "recall": tp / max(1, tp + fn),
        "f1": 2 * tp / max(1, 2 * tp + fp + fn),
        "iou": tp / max(1, tp + fp + fn),
    }


def train(model, data, epochs, generator):
    all_ids = torch.randperm(len(data[-1]), generator=generator)
    count = len(all_ids) // 7
    calibration, training = all_ids[:count], all_ids[count:]
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.002, weight_decay=0.0005)
    for epoch in range(epochs):
        model.train()
        order = training[torch.randperm(len(training), generator=generator)]
        for scores, truth in batch_outputs(model, data, order):
            target = truth.float()
            p = scores.sigmoid()
            intersection = (p * target).sum()
            dice = 1 - (2 * intersection + 1) / (p.sum() + target.sum() + 1)
            loss = F.binary_cross_entropy_with_logits(scores, target) + dice
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        if (epoch + 1) % 4 == 0 or epoch + 1 == epochs:
            print(f"Training epoch {epoch + 1}/{epochs}", flush=True)
    p, y = predict(model, data, calibration)
    results = [(float(threshold), metrics(p, y, threshold)) for threshold in SCORES]
    cutoff, scores = max(results, key=lambda pair: pair[1]["f1"])
    return cutoff, scores, len(training), len(calibration)


@torch.inference_mode()
def predict_scene(encoder, model, before, after):
    model.eval()
    height, width = before.shape[:2]
    stride = TILE - 2 * HALO
    pad = ((HALO, HALO + stride), (HALO, HALO + stride), (0, 0))
    a, b = (np.pad(image, pad, mode="edge") for image in (before, after))
    points = [(y, x) for y in range(0, height, stride) for x in range(0, width, stride)]
    scores = np.empty((height, width), dtype=np.float32)
    for start in range(0, len(points), 12):
        block = points[start : start + 12]
        images = [
            torch.from_numpy(
                np.stack([image[y : y + TILE, x : x + TILE] for y, x in block])
            )
            for image in (a, b)
        ]
        output = model(*features(encoder, *images)).sigmoid().numpy()
        for (y, x), tile in zip(block, output, strict=True):
            h, w = min(stride, height - y), min(stride, width - x)
            scores[y : y + h, x : x + w] = tile[HALO : HALO + h, HALO : HALO + w]
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis_id", type=UUID)
    parser.add_argument("--model-dir", type=Path, default=Path("var/models/satlas"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=16)
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("--epochs must be positive")
    torch.set_num_threads(min(4, torch.get_num_threads()))
    torch.manual_seed(42)
    generator = torch.Generator().manual_seed(42)
    args.output.mkdir(parents=True, exist_ok=True)
    model_dir = args.model_dir
    encoder = encoder_from(model_dir / "encoder.pth")
    train_data, test_data = (
        precompute(
            encoder,
            read_rows(model_dir / "data" / f"{part}-00000-of-00001.parquet"),
            part,
            model_dir / f"{part}-features-v1.pt",
        )
        for part in ("train", "test")
    )
    model = Decoder()
    cutoff, calibration, train_count, calibration_count = train(
        model, train_data, args.epochs, generator
    )
    test_scores, test_truth = predict(
        model, test_data, torch.arange(len(test_data[-1]))
    )
    test = metrics(test_scores, test_truth, cutoff)
    torch.save(
        {"decoder": model.state_dict(), "cutoff": cutoff},
        args.output / "satlas-change-head.pt",
    )
    packet, grid, valid, rgb, bands = saved_inputs(args.analysis_id)
    start = perf_counter()
    score = predict_scene(encoder, model, *rgb)
    seconds = perf_counter() - start
    pixel_ha = grid.resolution**2 / 10000
    patches = patch_labels(score, valid, cutoff, pixel_ha)
    geo = polygons(patches, np.zeros_like(valid), grid, "visible_change")
    np.savez_compressed(args.output / "scores.npz", score=score, valid=valid)
    (args.output / "satlas-patches.geojson").write_text(
        json.dumps(geo), encoding="utf-8"
    )
    fresunet = predict_fresunet(*bands, valid)
    overlays = [("Satlas Swin-Tiny - calibrated cutoff", patches > 0)]
    if fresunet is not None:
        overlays.append(
            (
                "FresUNet - score cutoff 0.5",
                patch_labels(fresunet, valid, 0.5, pixel_ha) > 0,
            )
        )
    preview(
        args.output / "comparison.png",
        rgb,
        packet,
        overlays,
    )
    report = {
        "training_patches": train_count,
        "calibration_patches": calibration_count,
        "official_test_patches": len(test_truth),
        "epochs": args.epochs,
        "cutoff": cutoff,
        "calibration": calibration,
        "held_out_oscd": test,
        "pair_inference_seconds": seconds,
        "pair_change_ha": float((patches > 0).sum() * pixel_ha),
        "pair_patches": len(geo["features"]),
        "head_parameters": sum(p.numel() for p in model.parameters()),
    }
    (args.output / "metrics.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
