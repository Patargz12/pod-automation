"""
services/mockup_service.py — T-Shirt Mockup Generation Service
===============================================================
Handles:
  • Interactive corner picking (print-area selection on a shirt template)
  • Perspective-warping a design onto the shirt
  • Auto blend-mode selection (screen for dark shirts, multiply for light)
  • Aspect-ratio-preserving transparent padding
  • Batch processing of all thumbnail × design combinations
"""

import cv2
import numpy as np
from PIL import Image, ImageFilter
from pathlib import Path

# Lift Pillow's decompression bomb limit (safe for local files)
Image.MAX_IMAGE_PIXELS = None


# ──────────────────────────────────────────────────────────────────────────────
# Corner helpers
# ──────────────────────────────────────────────────────────────────────────────

def sort_corners(corners: list) -> list:
    """
    Reorders any 4 clicked points into strict TL → TR → BR → BL order.
    Corrects mirrored / rotated output regardless of click order.
    """
    pts = np.array(corners, dtype=np.float32)

    s  = pts.sum(axis=1)
    tl = pts[np.argmin(s)].tolist()
    br = pts[np.argmax(s)].tolist()

    d  = pts[:, 1] - pts[:, 0]
    tr = pts[np.argmin(d)].tolist()
    bl = pts[np.argmax(d)].tolist()

    return [
        [int(tl[0]), int(tl[1])],
        [int(tr[0]), int(tr[1])],
        [int(br[0]), int(br[1])],
        [int(bl[0]), int(bl[1])],
    ]


def pick_corners_interactive(shirt_path: Path) -> list | None:
    """
    Opens an OpenCV window. User clicks the 4 corners of the print area.
    Returns [[x,y], …] in TL → TR → BR → BL order, or None if cancelled.

    Keyboard shortcuts:
      Ctrl+Z / Backspace / U  → undo last point
      R                       → reset all points
      ENTER                   → confirm (requires 4 points)
      ESC                     → skip this thumbnail
    """
    img = cv2.imread(str(shirt_path))
    if img is None:
        print(f"   ❌ Could not load image: {shirt_path}")
        return None

    h, w = img.shape[:2]
    max_display = 900
    scale = min(max_display / w, max_display / h, 1.0)
    display_w = int(w * scale)
    display_h = int(h * scale)

    display_img      = cv2.resize(img.copy(), (display_w, display_h))
    corners_display: list = []
    corners_real: list    = []

    LABELS = ["TOP-LEFT", "TOP-RIGHT", "BOTTOM-RIGHT", "BOTTOM-LEFT"]
    COLORS = [(0, 255, 80), (0, 200, 255), (255, 120, 0), (180, 0, 255)]

    def draw_overlay():
        overlay = display_img.copy()
        for i in range(len(corners_display)):
            cv2.circle(overlay, tuple(corners_display[i]), 7, COLORS[i], -1)
            cv2.circle(overlay, tuple(corners_display[i]), 7, (255, 255, 255), 1)
            if i > 0:
                cv2.line(overlay, tuple(corners_display[i - 1]),
                         tuple(corners_display[i]), (0, 255, 0), 2)
            if i == 3:
                cv2.line(overlay, tuple(corners_display[3]),
                         tuple(corners_display[0]), (0, 255, 0), 2)
        remaining = 4 - len(corners_display)
        if remaining > 0:
            label = LABELS[len(corners_display)]
            cv2.putText(overlay, f"Click: {label}", (10, display_h - 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(overlay, f"Points left: {remaining}", (10, display_h - 42),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
            cv2.putText(overlay,
                        "Ctrl+Z / Backspace / U = undo   |   R = reset",
                        (10, display_h - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 255), 1)
        else:
            cv2.putText(overlay, "ENTER = confirm   |   ESC = cancel",
                        (10, display_h - 42),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 180), 2)
            cv2.putText(overlay,
                        "Ctrl+Z / Backspace / U = undo   |   R = reset",
                        (10, display_h - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 255), 1)
        return overlay

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(corners_display) < 4:
            corners_display.append([x, y])
            real_x = int(x / scale)
            real_y = int(y / scale)
            corners_real.append([real_x, real_y])
            print(f"   📍 Point {len(corners_display)}/4 "
                  f"({LABELS[len(corners_display) - 1]}): ({real_x}, {real_y})")
        cv2.imshow(win_name, draw_overlay())

    win_name = f"Corner Picker — {shirt_path.name}"
    try:
        cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(win_name, display_w, display_h)
        cv2.setMouseCallback(win_name, on_mouse)
        cv2.imshow(win_name, draw_overlay())

        print(f"\n   ┌─ CORNER PICKER ───────────────────────────────────────┐")
        print(f"   │  Click the 4 corners of the PRINT AREA on the shirt:  │")
        print(f"   │  1. TOP-LEFT  →  2. TOP-RIGHT                         │")
        print(f"   │  3. BOTTOM-RIGHT  →  4. BOTTOM-LEFT                   │")
        print(f"   │                                                       │")
        print(f"   │  Ctrl+Z / Backspace / U  =  undo last point           │")
        print(f"   │  R                       =  reset all points          │")
        print(f"   │  ENTER                   =  confirm (need 4 points)   │")
        print(f"   │  ESC                     =  skip this thumbnail       │")
        print(f"   └───────────────────────────────────────────────────────┘\n")

        UNDO_KEYS  = {26, 8, ord('u'), ord('U')}
        RESET_KEYS = {ord('r'), ord('R')}

        while True:
            key = cv2.waitKey(20) & 0xFF
            if key == 13 and len(corners_real) == 4:
                cv2.destroyWindow(win_name)
                sorted_corners = sort_corners(corners_real)
                print(f"   🔄 Auto-sorted corners → "
                      f"TL:{sorted_corners[0]} TR:{sorted_corners[1]} "
                      f"BR:{sorted_corners[2]} BL:{sorted_corners[3]}")
                return sorted_corners
            elif key == 27:
                cv2.destroyWindow(win_name)
                return None
            elif key in UNDO_KEYS and corners_display:
                removed_label = LABELS[len(corners_display) - 1]
                corners_display.pop()
                corners_real.pop()
                print(f"   ↩️  Undo — removed {removed_label}  "
                      f"({len(corners_display)}/4 points remaining)")
                cv2.imshow(win_name, draw_overlay())
            elif key in RESET_KEYS and corners_display:
                count = len(corners_display)
                corners_display.clear()
                corners_real.clear()
                print(f"   🔁 Reset — cleared {count} point(s). Start again from TOP-LEFT.")
                cv2.imshow(win_name, draw_overlay())
            if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1:
                return None

    except cv2.error as e:
        print(f"   ⚠️  No display available for interactive picker: {e}")
        print(f"   💡 Run with a display, or manually add corners to config.json")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Core: warp + blend a design onto a shirt
# ──────────────────────────────────────────────────────────────────────────────

def warp_design_onto_shirt(
    shirt_pil: Image.Image,
    design_pil: Image.Image,
    corners: list,
    opacity: float = 1.0,
) -> Image.Image:
    """
    Perspective-warps `design_pil` into the region defined by `corners` on
    `shirt_pil`. Aspect ratio is preserved via transparent padding.

    Blend mode is chosen automatically:
      • Dark shirt  (avg brightness < 85)  → screen blend
      • Mid/light shirt                    → adaptive multiply blend

    Args:
        shirt_pil:  PIL Image of the shirt template (RGB or RGBA).
        design_pil: PIL Image of the design to overlay (RGB or RGBA).
        corners:    4 points [[x,y], …] in TL → TR → BR → BL order.
        opacity:    Global opacity of the design layer (0.0–1.0).

    Returns:
        PIL RGB Image with the design warped onto the shirt.
    """
    shirt_rgb   = np.array(shirt_pil.convert("RGB"), dtype=np.uint8)
    sh, sw      = shirt_rgb.shape[:2]
    design_rgba = np.array(design_pil.convert("RGBA"), dtype=np.uint8)
    dh, dw      = design_rgba.shape[:2]

    # Preserve design aspect ratio by padding with transparency
    corners_np = np.array(corners, dtype=np.float32)
    top_w      = np.linalg.norm(corners_np[1] - corners_np[0])
    bottom_w   = np.linalg.norm(corners_np[2] - corners_np[3])
    left_h     = np.linalg.norm(corners_np[3] - corners_np[0])
    right_h    = np.linalg.norm(corners_np[2] - corners_np[1])
    target_w   = (top_w + bottom_w) / 2.0
    target_h   = (left_h + right_h) / 2.0
    target_ar  = target_w / target_h
    design_ar  = dw / dh

    if abs(design_ar - target_ar) > 0.01:
        if design_ar < target_ar:
            new_dw  = int(round(dh * target_ar))
            offset  = (new_dw - dw) // 2
            padded  = np.zeros((dh, new_dw, 4), dtype=np.uint8)
            padded[:, offset:offset + dw] = design_rgba
            print(f"  📐 Aspect fit: {dw}x{dh} → {new_dw}x{dh} (padded horizontally)")
            design_rgba = padded
        else:
            new_dh  = int(round(dw / target_ar))
            offset  = (new_dh - dh) // 2
            padded  = np.zeros((new_dh, dw, 4), dtype=np.uint8)
            padded[offset:offset + dh, :] = design_rgba
            print(f"  📐 Aspect fit: {dw}x{dh} → {dw}x{new_dh} (padded vertically)")
            design_rgba = padded
        dh, dw = design_rgba.shape[:2]

    src_pts = np.float32([[0, 0], [dw, 0], [dw, dh], [0, dh]])
    dst_pts = np.float32(corners)
    M = cv2.getPerspectiveTransform(src_pts, dst_pts)

    warped_rgba = cv2.warpPerspective(
        design_rgba, M, (sw, sh),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    warped_color = warped_rgba[:, :, :3].astype(np.float32)
    warped_alpha = (warped_rgba[:, :, 3:4].astype(np.float32) / 255.0) * opacity
    design_mask  = warped_alpha[:, :, 0] > 0.05
    shirt_gray   = cv2.cvtColor(shirt_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)

    avg_brightness = float(
        shirt_gray[design_mask].mean() if design_mask.any() else shirt_gray.mean()
    )
    print(f"  🔍 Shirt brightness in design area: {avg_brightness:.0f}/255", end="")

    texture_strength = np.interp(
        avg_brightness,
        [0, 85, 128, 200, 255],
        [0.08, 0.10, 0.30, 0.55, 0.55],
    )

    shirt_gray_3ch = np.stack([shirt_gray] * 3, axis=-1)

    if avg_brightness < 85:
        print("  (dark shirt → screen blend)")
        shirt_n      = shirt_gray_3ch / 255.0
        design_n     = warped_color    / 255.0
        screened     = 1.0 - (1.0 - design_n) * (1.0 - shirt_n * 0.25)
        blended_design = np.clip(screened * 255.0, 0, 255)
    else:
        print(f"  (light/mid shirt → multiply blend, strength={texture_strength:.2f})")
        texture_factor = shirt_gray_3ch / 128.0
        flat           = warped_color
        textured       = np.clip(warped_color * texture_factor, 0, 255)
        blended_design = flat * (1.0 - texture_strength) + textured * texture_strength

    shirt_f = shirt_rgb.astype(np.float32)
    result  = shirt_f * (1.0 - warped_alpha) + blended_design * warped_alpha
    result  = np.clip(result, 0, 255).astype(np.uint8)

    # Feather edges with a soft alpha mask
    alpha_mask_img = Image.fromarray(
        (warped_alpha[:, :, 0] * 255).astype(np.uint8), "L"
    ).filter(ImageFilter.GaussianBlur(radius=1))
    alpha_mask = np.array(alpha_mask_img).astype(np.float32)[:, :, np.newaxis] / 255.0

    final = shirt_f * (1.0 - alpha_mask) + result.astype(np.float32) * alpha_mask
    return Image.fromarray(np.clip(final, 0, 255).astype(np.uint8), "RGB")


# ──────────────────────────────────────────────────────────────────────────────
# Batch mockup runner
# ──────────────────────────────────────────────────────────────────────────────

def build_output_name(
    thumb_path: Path,
    design_path: Path,
    multi_thumb: bool,
) -> str:
    """
    Naming strategy:
      • Single thumbnail    → mockup_{design}.png
      • Multiple thumbnails → mockup_{thumb}_{design}.png
    """
    if multi_thumb:
        return f"mockup_{thumb_path.stem}_{design_path.stem}.png"
    return f"mockup_{design_path.stem}.png"


def generate_mockups(
    thumbnails: list[Path],
    designs: list[Path],
    corners_map: dict[str, list],
    output_dir: Path,
) -> tuple[int, int]:
    """
    Render every thumbnail × design combination and save to `output_dir`.

    Args:
        thumbnails:  List of shirt template image paths.
        designs:     List of upscaled design image paths (from Final Designs/).
        corners_map: Mapping of thumbnail filename → [[x,y], …] corner list.
        output_dir:  Destination directory for rendered mockups.

    Returns:
        (processed_count, skipped_count)
    """
    multi_thumb = len(thumbnails) > 1
    total_jobs  = len(thumbnails) * len(designs)
    processed   = 0
    skipped     = 0
    job_idx     = 0

    print("\n" + "═" * 56)
    print("  🎽  Mockup Step")
    print("═" * 56)
    print(f"\n  📂 Thumbnails       : {len(thumbnails)}")
    print(f"  🎨 Upscaled designs : {len(designs)}")
    print(f"  🧮 Mockups to generate: {total_jobs}")

    # Cache thumbnail loads to avoid re-reading per design
    thumb_cache: dict[str, tuple[Image.Image, str]] = {}
    for thumb_path in thumbnails:
        if thumb_path.name not in corners_map:
            continue
        img = Image.open(thumb_path)
        thumb_cache[thumb_path.name] = (img, img.mode)

    for thumb_path in thumbnails:
        if thumb_path.name not in corners_map:
            skipped += len(designs)
            continue

        corners          = corners_map[thumb_path.name]
        shirt_img, mode  = thumb_cache[thumb_path.name]

        for design_path in designs:
            job_idx += 1
            print(f"\n{'─' * 56}")
            print(f"  [{job_idx}/{total_jobs}]  🖼️  {thumb_path.name}  ←  🎨  {design_path.name}")

            try:
                design_img = Image.open(design_path)
                result     = warp_design_onto_shirt(shirt_img, design_img, corners)

                # Preserve original alpha channel if the shirt template has one
                if mode == "RGBA":
                    original_rgba = np.array(shirt_img.convert("RGBA"))
                    result_rgb    = np.array(result)
                    result_rgba   = np.dstack([result_rgb, original_rgba[:, :, 3]])
                    result        = Image.fromarray(result_rgba, "RGBA")

                out_name = build_output_name(thumb_path, design_path, multi_thumb)
                out_path = output_dir / out_name
                result.save(out_path, "PNG")
                print(f"  ✅ Saved → {out_path}")
                processed += 1

            except Exception as e:
                print(f"  ❌ Failed: {e}")
                skipped += 1

    return processed, skipped
