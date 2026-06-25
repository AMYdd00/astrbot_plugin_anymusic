"""Generate an Apple-style music info card – AstrBot AnyMusic plugin.
Single unified frosted-glass background for consistent look across all platforms."""

import io
import logging
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)


# ── Layout ───────────────────────────────────────────────────────────
COVER_SIZE = 440
CORNER_R = 48
BADGE_R = 28

PAD_OUTER = 28
PAD_INNER = 26
GAP = 32

PANEL_W = 500
CARD_W = PAD_OUTER + COVER_SIZE + GAP + PANEL_W + PAD_OUTER
CARD_H = PAD_OUTER + COVER_SIZE + PAD_OUTER

COVER_X = PAD_OUTER
COVER_Y = PAD_OUTER
TEXT_X = COVER_X + COVER_SIZE + GAP
TEXT_MAX_W = PANEL_W - PAD_INNER * 2

# ── Colours ──────────────────────────────────────────────────────────
GLASS_BG     = (30, 32, 38, 215)
GLASS_BORDER = (255, 255, 255, 15)
SHADOW_COLOR = (0, 0, 0, 60)

ACCENT_COLOR = (255, 130, 145)
TITLE_COLOR  = (248, 248, 255)
TEXT_COLOR   = (210, 214, 228)
LABEL_COLOR  = (150, 155, 170)
CREDIT_COLOR = (100, 105, 120)
BADGE_TEXT   = "white"


# ── Font loader ──────────────────────────────────────────────────────

_FONT_CACHE = {}
_PLUGIN_DIR = Path(__file__).parent

# Noto Sans SC static-weight fonts – free, high-quality CJK + Latin with excellent kerning
# Source: Google Fonts / OFL licensed
# Using static builds to avoid Pillow rendering at hairline weight (variable [wght] default)
_NOTOSANS_FONTS: list[tuple[str, str]] = [
    ("NotoSansSC-Regular.otf",
     "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"),
    ("NotoSansSC-Bold.otf",
     "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf"),
]


async def _ensure_bundled_font(proxy: str = "") -> bool:
    """Ensure all bundled fonts exist; download Noto Sans SC static weights if missing.

    Returns True if all required font files are available.
    """
    fonts_dir = _PLUGIN_DIR / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    all_ok = True
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    async with aiohttp.ClientSession() as session:
        for filename, url in _NOTOSANS_FONTS:
            dest = fonts_dir / filename
            if dest.exists():
                continue
            logger.info(f"[AnyMusic] Downloading font {filename} ...")
            try:
                kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=30)}
                if proxy:
                    kwargs["proxy"] = proxy
                async with session.get(url, **kwargs) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        dest.write_bytes(data)
                        logger.info(f"[AnyMusic] Font downloaded: {dest} ({len(data)} bytes)")
                    else:
                        logger.warning(f"[AnyMusic] Font {filename} download failed (HTTP {resp.status})")
                        all_ok = False
            except Exception as e:
                logger.warning(f"[AnyMusic] Font {filename} download error: {e}")
                all_ok = False

    return all_ok


def _find_system_fonts() -> list[str]:
    """Return a list of candidate font file paths on the current platform."""
    import platform

    candidates = []

    system = platform.system()
    if system == "Windows":
        windir = Path("C:/Windows/Fonts")
        if windir.exists():
            candidates.extend([
                str(windir / "msyh.ttc"),           # Microsoft YaHei
                str(windir / "msyhbd.ttc"),         # Microsoft YaHei Bold
                str(windir / "simsun.ttc"),         # SimSun
                str(windir / "arial.ttf"),
                str(windir / "segoeui.ttf"),
                str(windir / "seguiemj.ttf"),
            ])
    elif system == "Darwin":  # macOS
        candidates.extend([
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            str(Path.home() / "Library/Fonts/NotoSansCJKsc-Regular.otf"),
        ])
    else:  # Linux / other
        candidates.extend([
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        ])

    return candidates


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a CJK + Latin capable font, searching system paths and bundled fonts.

    Caches loaded fonts by (size, bold) to avoid repeated filesystem I/O.
    """
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    # 1. Try bundled fonts inside the plugin directory
    bundled_dir = _PLUGIN_DIR / "fonts"
    if bundled_dir.is_dir():
        # Prefer the correctly matched weight file (Bold / Regular)
        preferred_name = "NotoSansSC-Bold.otf" if bold else "NotoSansSC-Regular.otf"
        preferred = bundled_dir / preferred_name
        if preferred.exists():
            try:
                font = ImageFont.truetype(str(preferred), size)
                _FONT_CACHE[cache_key] = font
                return font
            except Exception:
                pass
        # Fallback: any font file in the directory
        for fp in bundled_dir.iterdir():
            if fp.suffix.lower() in (".ttf", ".otf", ".ttc"):
                try:
                    font = ImageFont.truetype(str(fp), size)
                    _FONT_CACHE[cache_key] = font
                    return font
                except Exception:
                    pass

    # 2. Try system fonts
    system_paths = _find_system_fonts()
    for fp in system_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, size)
                _FONT_CACHE[cache_key] = font
                return font
            except Exception:
                pass

    # 3. Final fallback: PIL default (low quality, Latin-only)
    font = ImageFont.load_default()
    _FONT_CACHE[cache_key] = font
    return font


def _auto_crop_cover(im: Image.Image) -> Image.Image:
    bg = Image.new("RGBA", im.size, (255, 255, 255, 0))
    bg.paste(im, (0, 0), im)
    gray = bg.convert("L")
    bbox = gray.point(lambda p: 0 if p > 250 else 255).getbbox()
    if bbox:
        return im.crop(bbox)
    return im


async def download_cover(cover_url: str, proxy: str = "") -> Image.Image:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=15)}
    if proxy:
        kwargs["proxy"] = proxy
    async with aiohttp.ClientSession() as session:
        async with session.get(cover_url, **kwargs) as resp:
            data = await resp.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _round_corners(im: Image.Image, r: int) -> Image.Image:
    """Round corners with 2x supersampling for smooth anti-aliased edges."""
    scale = 2
    w, h = im.size
    big = (w * scale, h * scale)
    mask = Image.new("L", big, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, *big), r * scale, fill=255)
    mask = mask.resize(im.size, Image.LANCZOS)
    result = Image.new("RGBA", im.size)
    result.paste(im, mask=mask)
    return result


# ── Card builder ─────────────────────────────────────────────────────

async def make_info_card(
    cover_url: str,
    title: str,
    artist: str,
    album: str = "",
    source: str = "",
    proxy: str = "",
    duration: str = "",
    release_date: str = "",
) -> Image.Image:
    """Create a music card — unified frosted-glass background."""

    # Ensure bundled font is available before rendering text
    await _ensure_bundled_font(proxy)

    card = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # ── Card shadow (full rectangle, not rounded — avoids white corners on phone) ──
    so = 10
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle(
        (so, so, CARD_W - so, CARD_H - so), fill=SHADOW_COLOR,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    card.paste(shadow, mask=shadow)

    # ── Unified frosted-glass background (full rectangle) ──
    draw.rectangle((0, 0, CARD_W, CARD_H), fill=GLASS_BG)

    # ── Cover art ──
    cover = await download_cover(cover_url, proxy)
    cover = _auto_crop_cover(cover)
    cover = cover.resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    cover = _round_corners(cover, CORNER_R)
    card.paste(cover, (COVER_X, COVER_Y), cover)

    # ── Fonts & row heights ──
    font_title  = _load_font(44, bold=True)
    font_sub    = _load_font(30, bold=True)
    font_meta   = _load_font(26)
    font_label  = _load_font(22)
    font_credit = _load_font(20)
    font_badge  = _load_font(22, bold=True)

    title_line_h = 54
    sub_line_h   = 48
    meta_line_h  = 44
    gap_big = 18
    gap_sm  = 12
    badge_h = 38

    # ── Draw text top-down ──
    y = COVER_Y + PAD_INNER

    # Source badge — centered above title
    source_display = source.replace("_", " ").title()
    badge_text = f"  {source_display}  "
    badge_text_w = font_badge.getlength(badge_text)
    badge_bbox = font_badge.getbbox(badge_text)
    badge_text_h = badge_bbox[3] - badge_bbox[1]
    badge_x = TEXT_X + (PANEL_W - badge_text_w) // 2
    badge_pad = 6
    draw.rounded_rectangle(
        (badge_x - badge_pad, y,
         badge_x + badge_text_w + badge_pad, y + badge_h),
        BADGE_R, fill=ACCENT_COLOR,
    )
    # Vertical center: shift up a bit more — wqy-zenhei Latin baseline sits low
    text_y = y + (badge_h - badge_text_h) // 2 - 3
    draw.text((badge_x, text_y), badge_text, font=font_badge, fill=BADGE_TEXT)
    y += badge_h + gap_big

    # Title — auto-scale font if too long, max 2 lines
    title_lines = _fit_text(title, font_title, TEXT_MAX_W, min_size=28, max_lines=2)
    for line, line_font in title_lines:
        bb = line_font.getbbox(line)
        lw = bb[2] - bb[0]
        cx = TEXT_X + (PANEL_W - lw) // 2
        draw.text((cx, y), line, font=line_font, fill=TITLE_COLOR)
        y += title_line_h

    y += gap_big

    # Artist — center, auto-scale font, truncate with ellipsis
    artist_prefix = "艺术家  "
    artist_full = artist_prefix + artist
    artist_lines = _fit_text(
        artist_full, font_sub, TEXT_MAX_W, min_size=18, max_lines=2
    )
    for line, line_font in artist_lines:
        bb = line_font.getbbox(line)
        lw = bb[2] - bb[0]
        cx = TEXT_X + (PANEL_W - lw) // 2
        draw.text((cx, y), line, font=line_font, fill=TEXT_COLOR)
        y += sub_line_h
    y += gap_big

    # Metadata
    if album:
        _draw_label_value(draw, TEXT_X + PAD_INNER, y, "专辑", album,
                          font_label, font_meta, TEXT_MAX_W, LABEL_COLOR, TEXT_COLOR)
        y += meta_line_h
    if duration:
        _draw_label_value(draw, TEXT_X + PAD_INNER, y, "时长", duration,
                          font_label, font_meta, TEXT_MAX_W, LABEL_COLOR, TEXT_COLOR)
        y += meta_line_h
    if release_date:
        _draw_label_value(draw, TEXT_X + PAD_INNER, y, "发行", release_date,
                          font_label, font_meta, TEXT_MAX_W, LABEL_COLOR, TEXT_COLOR)
        y += meta_line_h

    # ── Credit line pinned to cover bottom ──
    credit_text = "由AstrBot AnyMusic插件生成"
    credit_h = 28
    credit_y = COVER_Y + COVER_SIZE - credit_h
    cb = font_credit.getbbox(credit_text)
    cw = cb[2] - cb[0]
    cx_credit = TEXT_X + (PANEL_W - cw) // 2
    draw.text((cx_credit, credit_y), credit_text, font=font_credit, fill=CREDIT_COLOR)

    return card


def _draw_label_value(draw, x, y, label, value, font_label, font_value, max_w,
                      label_color, value_color):
    prefix = f"{label}  "
    pw = font_label.getbbox(prefix)[2] - font_label.getbbox(prefix)[0]
    draw.text((x, y), prefix, font=font_label, fill=label_color)
    vw = max_w - pw
    vt = _truncate(value, font_value, vw)
    draw.text((x + pw, y), vt, font=font_value, fill=value_color)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    if not text:
        return []
    lines = []
    current = ""
    for ch in text:
        test = current + ch
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] > max_w and current:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    if not text:
        return ""
    bbox = font.getbbox(text)
    if bbox[2] - bbox[0] <= max_w:
        return text
    while text and font.getbbox(text + "...")[2] - font.getbbox(text + "...")[0] > max_w:
        text = text[:-1]
    return text + "..."


def _fit_text(
    text: str,
    base_font: ImageFont.FreeTypeFont,
    max_w: int,
    min_size: int = 28,
    max_lines: int = 2,
) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """Fit text into max_lines by auto-scaling font size.

    Returns list of (line_text, font) tuples.
    If text still overflows at min_size, last visible line is truncated with "...".
    """
    current_size = base_font.size
    bold = True  # title font is always bold

    while current_size >= min_size:
        font = _load_font(current_size, bold=bold)
        lines = _wrap_text(text, font, max_w)
        if len(lines) <= max_lines:
            return [(line, font) for line in lines]
        current_size -= 2

    # At min_size, still too many lines → truncate to max_lines
    font = _load_font(min_size, bold=bold)
    lines = _wrap_text(text, font, max_w)
    if len(lines) > max_lines:
        # Keep first max_lines-1 full lines, truncate the last
        visible = lines[: max_lines - 1]
        last_line = _truncate(lines[max_lines - 1], font, max_w)
        visible.append(last_line)
        return [(line, font) for line in visible]
    return [(line, font) for line in lines]


def _fit_single_line(
    text: str,
    base_font: ImageFont.FreeTypeFont,
    max_w: int,
    min_size: int = 20,
) -> str:
    """Fit text to a single line by auto-scaling font, then truncating with "...".

    Note: This modifies base_font in-place by reloading at smaller sizes.
    Returns the fitted text string.
    """
    current_size = base_font.size
    bold = True  # artist font is bold

    while current_size >= min_size:
        font = _load_font(current_size, bold=bold)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_w:
            return text
        current_size -= 1

    # At min_size, still too long → truncate with "..."
    font = _load_font(min_size, bold=bold)
    return _truncate(text, font, max_w)
