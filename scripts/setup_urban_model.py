"""Download the IPOL checkpoint and export it for local ONNX Runtime inference."""

import hashlib
import importlib.util
import sys
import urllib.request
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

SOURCE = "https://www.ipol.im/pub/art/2022/439/439-master.zip"
SOURCE_SHA256 = "fe14811e754d7531e8a3e4f492d4933fdaae2946d804471933d9c8160f5cb17b"
TILE = 256


def main():
    import torch

    from satcore.config import data_path

    with urllib.request.urlopen(SOURCE, timeout=30) as response:
        payload = response.read(8_000_000)
    if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("The IPOL source archive did not match its pinned checksum.")
    output = data_path("URBAN_MODEL_PATH", "var/models/fresunet.onnx")
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        TemporaryDirectory(dir=output.parent) as scratch,
        ZipFile(BytesIO(payload)) as archive,
    ):
        files = Path(scratch)
        source = files / "fresunet.py"
        source.write_bytes(archive.read("439-master/fresunet.py"))
        spec = importlib.util.spec_from_file_location("ipol_fresunet", source)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the verified IPOL architecture.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        model = module.FresUNet(6, 2).eval()
        weights = BytesIO(archive.read("439-master/fresunet3_final.pth.tar"))
        model.load_state_dict(
            torch.load(weights, map_location="cpu", weights_only=True)
        )
        sample = torch.zeros((1, 3, TILE, TILE))
        temporary = files / output.name
        torch.onnx.export(
            model,
            (sample, sample),
            temporary,
            input_names=["before", "after"],
            output_names=["log_probability"],
            opset_version=17,
            dynamo=False,
        )
        temporary.replace(output)
    print(f"Installed {output}")


if __name__ == "__main__":
    main()
