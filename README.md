# POD Automation — T-Shirt Mockup Generator

A modular pipeline that turns raw design PNGs into polished t-shirt mockup thumbnails for print-on-demand listings. Drop your designs in, get upscaled mockups out.

## What it does

1. **Upscales** every raw design 4x using Real-ESRGAN (the same engine Upscayl uses)
2. **Warps** each upscaled design onto your t-shirt template(s) using a one-time interactive corner picker
3. **Blends** intelligently based on shirt brightness so the print looks like fabric, not a pasted sticker
4. **Outputs** ready-to-use thumbnails for Etsy, Printful, Redbubble, etc.

## Folder structure

```
pod-automation/
├── execute.py               # entry point — runs the full pipeline
├── config.json              # auto-saved corner coordinates (don't edit manually)
├── designs/                 # YOU drop raw, un-upscaled designs here
├── Final Designs/           # auto-populated with 4x upscaled versions
├── thumbnails/              # YOU drop your t-shirt template image(s) here
├── output/                  # final mockups land here
├── services/
│   ├── upscale_service.py   # Real-ESRGAN binary detection + 4x upscaling logic
│   └── mockup_service.py    # corner picker, perspective warp, batch rendering
└── bin/
    └── realesrgan/
        ├── realesrgan-ncnn-vulkan.exe
        └── models/
            ├── realesrgan-x4plus-anime.bin
            ├── realesrgan-x4plus-anime.param
            └── ... (other models)
```

## One-time setup

1. Install Python dependencies:

   ```bash
   pip install opencv-python pillow numpy
   ```

2. Download Real-ESRGAN-ncnn-vulkan from https://github.com/xinntao/Real-ESRGAN/releases (pick your OS).

3. Extract the entire release zip into `bin/realesrgan/` so you have the `.exe` (or binary) plus a `models/` folder next to it.

4. Drop at least one t-shirt template image into `thumbnails/`.

## Usage

### First run (corner setup)

```bash
python execute.py
```

An interactive window opens for each thumbnail. Click the 4 corners of the printable area in this order:

```
1. Top-Left  →  2. Top-Right  →  3. Bottom-Right  →  4. Bottom-Left
```

**Picker controls:**

| Key                          | Action                      |
| ---------------------------- | --------------------------- |
| Left click                   | Place corner                |
| `Ctrl+Z` / `Backspace` / `U` | Undo last point             |
| `R`                          | Reset all points            |
| `Enter`                      | Confirm (requires 4 points) |
| `Esc`                        | Skip this thumbnail         |

Corners are saved to `config.json`. Subsequent runs are fully automatic.

### Daily workflow

1. Drop new raw designs into `designs/`
2. Run `python execute.py`
3. Grab finished mockups from `output/`

That's it. The pipeline:

- Upscales any _new_ designs (skips ones already in `Final Designs/`)
- Generates a mockup for every `(thumbnail × design)` pair
- Caches everything, so re-runs are fast

## CLI reference

| Command                            | Purpose                                       |
| ---------------------------------- | --------------------------------------------- |
| `python execute.py`                | Full pipeline: upscale → mockup               |
| `python execute.py --upscale-only` | Just upscale, no mockups                      |
| `python execute.py --skip-upscale` | Use existing `Final Designs/` as-is           |
| `python execute.py --reupscale`    | Force re-upscale (overwrite `Final Designs/`) |
| `python execute.py --setup`        | Just run the corner picker, no processing     |
| `python execute.py --reset`        | Clear saved corners (re-pick on next run)     |
| `python execute.py --help`         | Full help text                                |

## Key features

- **Auto-model selection** — Scans installed Real-ESRGAN models and picks the best one for illustration-style designs. Preference order: `realesr-general-x4v3` (Upscayl Lite) → `realesrgan-x4plus-anime` → `realesrgan-x4plus`.
- **Aspect-ratio preservation** — Portrait designs stay portrait, landscape stays landscape. The print area is automatically padded with transparency so designs never get stretched or squashed regardless of corner placement.
- **Adaptive blending** — Detects shirt brightness inside the print region and switches blend mode:
  - Dark shirt (avg < 85) → screen blend (keeps design colors vivid, adds fabric depth)
  - Mid/light shirt → multiply blend with strength ramped to brightness (subtle fabric grain on light shirts)
- **Soft edge feathering** — A 1px Gaussian blur on the alpha mask kills hard pixel borders at the corners.
- **Batch mode** — Process N thumbnails × M designs in one command (`N × M` mockups).
- **Idempotent upscaling** — Files already in `Final Designs/` are skipped; only new designs get upscaled.
- **Pre-flight diagnostics** — Every run starts with a scan showing exactly what files and tools are detected, so issues are obvious before processing starts.

## Output naming

- **1 thumbnail + N designs** → `mockup_{design_name}.png` (clean, design-focused)
- **N thumbnails + M designs** → `mockup_{thumbnail_name}_{design_name}.png` (disambiguated)

## Supported input formats

`.png`, `.jpg`, `.jpeg`, `.webp` — for both designs and thumbnails. Outputs are always `.png` to preserve transparency.

## How the warp works (brief)

1. Your 4 clicked corners on the shirt define a quadrilateral (the print area).
2. The design is padded with transparent pixels to match that quad's aspect ratio.
3. A perspective transform maps the padded design's corners onto the quad's corners.
4. The warped result is composited onto the shirt with the chosen blend mode.

This means a slightly skewed click (perspective on a folded shirt) still produces a natural-looking print, and the corners can be on any shape of quad — square, rectangle, trapezoid, parallelogram.

## Troubleshooting

**`Real-ESRGAN bin → ❌ NOT FOUND`** in pre-flight: the binary isn't in `bin/realesrgan/`. Re-extract the release zip.

**`No usable Real-ESRGAN model found`**: the `models/` folder is missing or empty. Make sure both `.bin` and `.param` files for at least one supported model are present.

**Design comes out stretched**: shouldn't happen anymore (aspect ratio is auto-preserved), but if it does, re-pick corners with `--reset` and click more carefully along the actual edges of the printable area.

**Mockup looks washed out on a light shirt**: this is intentional — the multiply blend simulates ink on fabric. If you want a flatter look, the blending strength can be tuned in `warp_design_onto_shirt()` inside `services/mockup_service.py`.

## Dependencies

- Python 3.10+
- `opencv-python` — perspective warp + corner picker UI
- `pillow` — image I/O and Gaussian blur
- `numpy` — array math for blending
- `realesrgan-ncnn-vulkan` (external binary) — 4x upscaling
