# Ask the Satellite

Ask a question about a place and two dates. An emulated AI satellite compares real Sentinel-2 images, measures what changed, and sends back a short answer with its evidence and a confidence score.

The satellite is a Jetson Orin Nano running the analysis onboard. Only a small answer packet comes down the simulated radio link, not the images.

Built for Challenge 1, "Ask the Satellite". Every data source is free.

## What runs where

| Part | Folder | Runs on | Port |
| --- | --- | --- | --- |
| Change engine | `satcore/` | the satellite node | |
| Flight node | `node/` | Jetson Orin Nano (or inside the ground station for a laptop-only run) | 8100 |
| Ground station API | `ground/` | laptop | 8000 |
| Web interface | `web/` | served by the ground station | 8000 |

The ground station plans the question, sends a short command up, shows progress, and explains the answer. The node finds the images, masks cloud, detects change, scores confidence and writes a one-sentence verdict with a local language model.

## Quick start (laptop only)

Needs Python 3.11+ and Node 20+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
npm --prefix web ci && npm --prefix web run build
cp .env.example .env
.venv/bin/python -m uvicorn ground.main:app --port 8000
```

Open http://127.0.0.1:8000 and click a case under **Try a real case**. With `NODE_MODE=local` (the default) the node runs inside the ground station, so no Jetson is needed.

On Windows without WSL, use `.\.venv\Scripts\python.exe` in place of `.venv/bin/python`.

## Jetson flight node

On the Jetson (JetPack 6 or newer), with this folder copied over (`web/` is not needed there):

```bash
deploy/jetson/setup.sh     # once: Python environment, Ollama and the llama3.2:3b model
deploy/jetson/run.sh       # start the node on 0.0.0.0:8100
deploy/jetson/check.sh     # smoke test: health, telemetry, offline and live analysis
```

Wait until `curl -s localhost:8100/health` shows `"verdict_model_ready": true`. After a reboot the model takes about 3 minutes to load, then stays in memory.

On the laptop, set these in `.env` and start the ground station. Over the Jetson's USB-C cable its address is `192.168.55.1`:

```
NODE_MODE=remote
NODE_URL=http://192.168.55.1:8100
```

The header then shows "Jetson Orin Nano" with its live power draw and temperature. The Jetson needs its own internet the first time it sees a place, because it downloads the image crops itself. Places it has analysed before are stored on its disk.

## Phone link (QR code)

```bash
deploy/phone_link.sh
```

Needs [`cloudflared`](https://github.com/cloudflare/cloudflared/releases) on the laptop. The script opens a public address through a Cloudflare quick tunnel, writes it to `.env` as `PUBLIC_URL`, restarts the ground station on port 8000 and prints the phone address. Press **Phone QR** on the big screen to show the code.

- The address is public while the script runs, and it changes on every run.
- The tunnel and the ground station run on the laptop. If the laptop sleeps, both stop.
- Questions are answered one at a time. Each phone shows how many are ahead of it.

## Without the Jetson

Nothing needs the board. Two ways to run without it:

- **In-process (the default).** With `NODE_MODE=local` the ground station runs the engine itself.
- **Simulated flight node.** `deploy/sim/run.sh` (or `make sim`) starts the same node code on `127.0.0.1:8100`. Set `NODE_MODE=remote` and `NODE_URL=http://127.0.0.1:8100` and the ground station talks to it exactly as it talks to the Jetson.

In the simulated node the analyses are real: same engine, same imagery. Only power and temperature are simulated, modelled on the Jetson's measurements (3.5 W idle, 9.7 W mean and 18.9 W peak while analysing, 42 °C rising towards 50 °C). They are labelled "simulated" in `/health`, `/telemetry` and on screen, so they are never shown as measurements. The verdict is written by a local Ollama model if one is running on the machine, otherwise by the template sentence.

`deploy/sim/demo.sh` starts the model server, the simulated node and the ground station together: the whole demo on one machine.

## What it can do

- **Six kinds of change:** new water, lost water, lost vegetation, new vegetation, new buildings, burn scars.
- **Plain questions:** "telangana india, any flood effects from jan 2024 to jan 2025" works with no paid service. A rule-based planner reads the change type, dates and place.
- **Real overpasses:** the "When" step lists the actual Sentinel-2 passes near a date with the cloud cover over the chosen area.
- **Honest refusals:** if too little of the area is clear in both images, it refuses and offers the nearest clear passes.
- **Confidence:** a 0–100 score built from documented checks, each one shown as a plain caveat.
- **Standing watch:** the same question on every overpass. The node sends about 2 KB when nothing changed and the full answer only on real change.
- **Phone access:** a QR code opens a phone page. Questions from the room appear on the big screen. They are answered one at a time, in order.
- **Three languages:** English, Arabic, Hindi.

## Measured numbers

All measured on this build, on the Jetson, unless stated.

| What | Value |
| --- | --- |
| Dubai flood case (12 → 22 Apr 2024) | 198.72 ha of new water, 8.5% of the area, confidence 100 |
| Answer packet for that case | 87,413 bytes |
| Raw images for both dates | 3,448,970,118 bytes (about 39,000 times larger) |
| Over UHF at 9.6 kbit/s | 73 s for the answer, 33 days for the raw images |
| Standing watch, April 2024, six passes | 239 KB sent, 9.8 GB if sent raw |
| Power | about 3.5 W idle with the model loaded, 18.9 W peak |
| Onboard verdict (model warm) | 1.4–2.1 s |
| Repeat question (scenes stored) | 3–4 s |
| New place (scenes downloaded) | about 20–40 s |
| Rondônia against INPE PRODES 2020–2024 | recall 0.84, precision 0.38 (one 25 km² site) |

## Free mode and OpenAI

Nothing paid is required. Without `OPENAI_API_KEY`:

- questions are read by `ground/llm/local_planner.py`;
- the explanation is built from the packet's numbers;
- the follow-up chat is hidden.

With a key, OpenAI plans the question, writes the explanation and answers follow-ups. Every number it writes is checked against the packet, and a failed check falls back to the built-in text. The OpenAI path has not been tested in the current interface.

## Checks

```bash
.venv/bin/python -m pytest -q            # 27 offline tests
.venv/bin/python -m pytest -q -m live    # one live test, needs internet
.venv/bin/python -m ruff check .
npm --prefix web run typecheck
.venv/bin/python -m scripts.validate_prodes   # accuracy check against INPE PRODES
```

## Optional neural detectors

None of this is needed to run the product. The default detectors are index rules.

### FresUNet for urban growth

Live **Urban growth** can use the published [FresUNet RGB checkpoint](https://www.ipol.im/pub/art/2022/439/) to help find visibly changed areas. The detector uses the Sentinel-2 bands to screen possible development and retains the cloud and patch-size checks. The **Evidence** tab names the detector used. A model score represents visible change and can also respond to grading or exposed soil.

To install the model in a fresh Windows environment, run the following once from the project folder. The setup script verifies the published archive and converts its checkpoint to ONNX. The model runs locally on CPU. PyTorch and ONNX are used for the setup step. The running app uses ONNX Runtime.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[urban-model]" onnx
.\.venv\Scripts\python.exe -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m scripts.setup_urban_model
```

The generated model is stored under `var/models/`. If the model is unavailable, Urban growth uses the spectral and texture rules. For remote mode, install the model on the flight node.

Choose **Visible change · FresUNet** to map candidate changes directly from the two images. This mode uses the model score, cloud mask, and minimum patch size. Use **Download comparison PNG** from the answer or Evidence to save both dates and the matching outlines and shaded patches.

### Compare two-image patch models

The standalone comparison runs FresUNet and [BTC-B OSCD96](https://github.com/blaz-r/BTC-change-detection) on a saved live analysis. It exports the raw scores, cleaned binary masks, WGS84 GeoJSON patches, and a before/after preview. The comparison uses clear pixels from both dates and a 0.5 ha minimum patch.

With FresUNet set up and CPU PyTorch installed, add the comparison dependencies and download the two BTC files:

```powershell
.\.venv\Scripts\python.exe -m pip install transformers==4.57.3 safetensors
.\.venv\Scripts\python.exe -m pip install torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
New-Item -ItemType Directory -Force var/models/btc
Invoke-WebRequest 'https://huggingface.co/blaz-r/BTC-B_oscd96/resolve/main/model.safetensors' -OutFile var/models/btc/model.safetensors
Invoke-WebRequest 'https://huggingface.co/facebook/mask2former-swin-base-IN21k-cityscapes-semantic/resolve/main/config.json' -OutFile var/models/btc/backbone.json
.\.venv\Scripts\python.exe -m scripts.compare_change_models <analysis-id> --output var/patch-benchmark/example
```

Use an analysis ID with both acquisitions, locally cached bands, and a completed change run. The scores are model outputs. Treat the overlay as a map of candidate changes and validate the cutoff on representative before-and-after pairs before using it for an area estimate.

### Satlas change-head experiment

The experiment freezes Ai2's [Satlas Swin-Tiny Sentinel-2 RGB encoder](https://github.com/allenai/satlaspretrain_models) and trains a small decoder on differences between the two dates' feature maps. It uses [OSCD RGB crops](https://huggingface.co/datasets/blaz-r/OSCD_RGB_Cropped_96), whose dataset card lists CC BY-NC-SA. The script selects the cutoff from a subset of the training patches and measures pixel-level precision, recall, F1 and IoU on the official test split.

```powershell
.\.venv\Scripts\python.exe -m pip install pyarrow
New-Item -ItemType Directory -Force var/models/satlas/data
Invoke-WebRequest 'https://huggingface.co/allenai/satlas-pretrain/resolve/main/sentinel2_swint_si_rgb.pth?download=true' -OutFile var/models/satlas/encoder.pth
foreach ($split in @('train','test')) {
  $file = "$split-00000-of-00001.parquet"
  Invoke-WebRequest "https://huggingface.co/datasets/blaz-r/OSCD_RGB_Cropped_96/resolve/becf869ed351bf851fcf2d70dfc9e844d4a636f6/data/$file" -OutFile "var/models/satlas/data/$file"
}
.\.venv\Scripts\python.exe -m scripts.try_satlas_change <analysis-id> --output var/patch-benchmark/satlas
```

Use the CPU PyTorch and torchvision versions from the earlier setup commands. The experiment saves the trained decoder, benchmark metrics, pixel scores, and GeoJSON patches beside its comparison PNG.

## Limits

- This is an emulation. The Jetson replays real, ground-processed Sentinel-2 data; it has no camera and no radio.
- Sentinel-2 pixels are 10 m. Features smaller than about 0.5 ha are not reported.
- One analysis covers at most 50 km². A state or country is sampled with a 5 km box at its centre, and the answer says so.
- Water found on cropland is often irrigation or a flooded field, not a flood. The confidence score does not yet flag this.
- Built-up growth is the weakest detector: bare soil can look like new construction.
- The season baseline and the "when did it happen" chart from the original plan are not built.
- The Jetson node does not start by itself after a reboot.

## Data sources

Contains modified Copernicus Sentinel data. Imagery through Element 84 Earth Search and Microsoft Planetary Computer. Land cover © ESA WorldCover project 2021. Globe imagery: Sentinel-2 cloudless 2020 by EOX IT Services GmbH. Place names and borders © OpenStreetMap contributors, OpenFreeMap. Place search by Open-Meteo and OpenStreetMap Nominatim. Weather by Open-Meteo. Orbits from CelesTrak. Deforestation reference: INPE PRODES via TerraBrasilis.
