"""
services/upscale_service.py — Real-ESRGAN upscaling service
============================================================
Handles detection of the Real-ESRGAN binary and model, and runs
4x upscaling on every raw design in designs/ → Final Designs/.
"""

import sys
import shutil
import subprocess
from pathlib import Path

# ─── Real-ESRGAN Configuration ─────────────────────────────────────────────────
MODEL_PREFERENCE = [
    "realesr-general-x4v3",
    "realesrgan-x4plus-anime",
    "realesrgan-x4plus",
]
SCALE = 4
BIN_NAME = "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" \
                                        else "realesrgan-ncnn-vulkan"


def find_binary() -> Path | None:
    """
    Locate the realesrgan-ncnn-vulkan executable.
    Search order: ./bin/realesrgan/, ./bin/, project root, then $PATH.
    """
    candidates = [
        Path("bin") / "realesrgan" / BIN_NAME,
        Path("bin") / BIN_NAME,
        Path(BIN_NAME),
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()

    on_path = shutil.which(BIN_NAME)
    return Path(on_path) if on_path else None


def detect_best_model(binary_path: Path) -> tuple[str | None, list[str]]:
    """
    Inspect the models/ folder next to the binary and return:
      (chosen_model_name, list_of_all_available_models)
    Chooses by MODEL_PREFERENCE order.
    Returns (None, []) if the models folder is missing.
    """
    models_dir = binary_path.parent / "models"
    if not models_dir.is_dir():
        return None, []

    bins   = {p.stem for p in models_dir.glob("*.bin")}
    params = {p.stem for p in models_dir.glob("*.param")}
    available = sorted(bins & params)

    for preferred in MODEL_PREFERENCE:
        if preferred in available:
            return preferred, available
    return None, available


def upscale(
    designs_dir: Path,
    output_dir: Path,
    img_exts: tuple[str, ...],
    force: bool = False,
) -> int:
    """
    Run every design in `designs_dir` through Real-ESRGAN 4x and write to
    `output_dir`. Files already present in `output_dir` are skipped unless
    `force=True`. Returns the number of files newly upscaled.

    Args:
        designs_dir: Directory containing raw (not-yet-upscaled) design files.
        output_dir:  Destination directory for 4x upscaled output (Final Designs/).
        img_exts:    Tuple of glob patterns, e.g. ("*.png", "*.jpg").
        force:       When True, re-upscales files even if the output already exists.

    Returns:
        Number of files successfully upscaled.
    """
    raw_designs = _gather_files(designs_dir, img_exts)

    print("\n" + "═" * 56)
    print(f"  ⬆️   Upscale Step — {SCALE}x")
    print("═" * 56)

    if not raw_designs:
        print(f"\n  ℹ️  No raw designs in '{designs_dir}/' — nothing to upscale.")
        print(f"     (Drop .png/.jpg/.jpeg/.webp files there to enable upscaling.)")
        return 0

    todo = []
    for src in raw_designs:
        dst = output_dir / f"{src.stem}.png"
        if dst.exists() and not force:
            print(f"  ✓ Already upscaled — skipping {src.name}")
        else:
            todo.append((src, dst))

    if not todo:
        print(f"\n  ✅ Everything in '{designs_dir}/' is already upscaled.")
        return 0

    binary = find_binary()
    if binary is None:
        print(f"\n❌  Real-ESRGAN binary not found.")
        print(f"    Expected at: ./bin/realesrgan/{BIN_NAME}")
        print(f"    Download:    https://github.com/xinntao/Real-ESRGAN/releases")
        print(f"    Extract the whole release into ./bin/realesrgan/ "
              f"(binary + models folder).\n")
        print(f"    Alternatively, run with --skip-upscale to bypass this step.\n")
        sys.exit(1)

    model, available = detect_best_model(binary)
    if model is None:
        print(f"\n❌  No usable Real-ESRGAN model found.")
        print(f"    Looked in: {binary.parent / 'models'}/")
        if available:
            print(f"    Installed but unrecognized: {', '.join(available)}")
        else:
            print(f"    The 'models/' folder is missing or empty.")
        print(f"\n    Expected one of (in priority order):")
        for m in MODEL_PREFERENCE:
            print(f"      • {m}")
        print(f"\n    Standard Real-ESRGAN release ships with 'realesrgan-x4plus-anime'")
        print(f"    and 'realesrgan-x4plus' — re-extract the release zip if missing.\n")
        sys.exit(1)

    print(f"\n  🔧 Binary: {binary}")
    print(f"  🧠 Model:  {model}  (auto-selected; available: {len(available)})")
    print(f"  📂 Files to upscale: {len(todo)}\n")

    upscaled = 0
    for idx, (src, dst) in enumerate(todo, start=1):
        print(f"  [{idx}/{len(todo)}]  ⬆️  {src.name}  →  {dst.name}")
        cmd = [
            str(binary),
            "-i", str(src),
            "-o", str(dst),
            "-n", model,
            "-s", str(SCALE),
            "-f", "png",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                combined = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
                tail = combined.splitlines()[-5:] if combined else ["<no output>"]
                print(f"     ❌ Upscale failed (exit {result.returncode})")
                for line in tail:
                    print(f"        {line}")
                continue
            if not dst.exists():
                print(f"     ❌ Upscale ran but no output file was produced.")
                continue
            print(f"     ✅ Saved")
            upscaled += 1
        except FileNotFoundError:
            print(f"     ❌ Could not execute binary: {binary}")
            sys.exit(1)

    print(f"\n  🎉 Upscaling done — {upscaled} new file(s) in '{output_dir}/'")
    return upscaled


def _gather_files(directory: Path, img_exts: tuple[str, ...]) -> list[Path]:
    files = []
    for ext in img_exts:
        files.extend(directory.glob(ext))
        files.extend(directory.glob(ext.upper()))
    return sorted(set(files))
