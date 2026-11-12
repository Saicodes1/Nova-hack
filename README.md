# Ask the Satellite

Ask the Satellite compares two Sentinel-2 acquisitions over a selected area. The satellite node reads imagery, masks clouds, detects change on a 10 m grid, and downlinks a compact packet. The ground station displays scene provenance, confidence, imagery, polygons, progress and link estimates.

## Run on Windows

From this folder, with Python 3.11+ and Node installed:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe -m uvicorn ground.main:app --host 127.0.0.1 --port 8000
```

Open [the local ground station](http://127.0.0.1:8000). If the project already has the installed environment and built frontend, run the last command only. For live frontend development, keep the API running and use `npm --prefix web run dev`, then open the Vite URL.

Choose **Run demo** to generate an offline example. The four demo cases use explicitly labeled synthetic imagery. They exercise the same indices, cloud masks, change rules, packet serialization, and display as live mode. The basemap, place search and orbit predictions need internet access.

For real imagery select **Live Sentinel-2**. Draw a polygon or rectangle, or choose a place to create a 5 km box. Pick before and after dates, choose an analysis type, and submit. The maximum area is 50 km². Acquisition dates can differ from requested dates when the catalog supplies better coverage nearby.

## OpenAI

Copy `.env.example` to `.env`. Add your `OPENAI_API_KEY` locally and restart the API. The key is read by the Python server and `.env` is ignored by Git. Both model settings default to [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) and can be overridden.

- `OPENAI_MODEL_FAST` uses Responses structured outputs to turn a question into a plan.
- `OPENAI_MODEL_MAIN` uses Responses structured outputs for explanations and function calling for follow-ups.

With explicit AOI, dates and analysis type, the server can run without a key. The node always computes the measurements. The server checks model-generated numbers against the evidence and can use a packet-based explanation if the model fails or introduces unsupported numbers.

## Node and data

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

The FastAPI ground station talks to an in-process node by default. To exercise the HTTP node locally, start `python -m uvicorn node.flight_app:app --port 8100` in another terminal and set `NODE_MODE=remote` and `NODE_URL=http://127.0.0.1:8100` in the ground server environment. Run a live analysis to use the remote node. Both apps use the same engine. Run a single ground-server worker because in-progress events are held in memory. Completed results and replay stages persist in SQLite. Keep both services on a trusted local connection.

Public sources include Element 84 Earth Search, Microsoft Planetary Computer, ESA WorldCover, OpenFreeMap and OpenStreetMap, Open-Meteo, and CelesTrak. Link speeds and orbit passes are simulated or predicted. Scene IDs, acquisition dates and image attribution appear with the result.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix web run typecheck
.\.venv\Scripts\python.exe -m pytest -q -m live
```

The Markdown plan in `docs/` describes the broader build. This implementation uses compressed AOI-specific scene caches, synthetic offline demo cases, and the same frontend for both local and remote mode. The plan's pinned real-scene demo preparation and Jetson deployment require additional setup.
