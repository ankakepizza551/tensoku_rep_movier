"""icon.ico を生成するスクリプト。py make_icon.py で実行。"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


# ── フォント候補 ────────────────────────────────────────────
_FONT_JP = [
    "C:/Windows/Fonts/yugothb.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
]
_FONT_EN = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (size - 1, y)], fill=(r, g, b, 255))
    return img


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def make_icon(size: int) -> Image.Image:
    s = size / 256

    # 背景グラデーション（濃紺→ダーク紫）
    img = _gradient(size, (18, 18, 48), (38, 12, 72))
    img.putalpha(_rounded_mask(size, int(size // 7)))

    draw = ImageDraw.Draw(img)

    # 透かし「非」
    wm_font = _font(_FONT_JP, int(168 * s))
    draw.text((size // 2, int(118 * s)), "非", font=wm_font,
              fill=(255, 255, 255, 22), anchor="mm")

    # 左上の録画ドット（赤）
    dr = int(16 * s)
    dx, dy = int(36 * s), int(36 * s)
    draw.ellipse([dx - dr, dy - dr, dx + dr, dy + dr], fill=(220, 50, 50, 255))

    # メインテキスト .rep → .mp4
    f_main = _font(_FONT_EN, int(38 * s))
    f_arrow = _font(_FONT_EN, int(34 * s))
    cy = int(118 * s)

    draw.text((int(44 * s),  cy), ".rep", font=f_main,  fill=(140, 180, 255, 255), anchor="lm")
    draw.text((int(130 * s), cy), "→",   font=f_arrow, fill=(255, 200,  80, 255), anchor="mm")
    draw.text((int(214 * s), cy), ".mp4", font=f_main,  fill=(80,  210, 130, 255), anchor="mm")

    # 下部ラベル「リプレイ録画」
    f_label = _font(_FONT_JP, int(22 * s))
    draw.text((size // 2, int(186 * s)), "リプレイ録画", font=f_label,
              fill=(200, 200, 255, 180), anchor="mm")

    return img


def main() -> None:
    sizes = [256, 128, 64, 48, 32, 16]
    images = [make_icon(s) for s in sizes]

    out = Path(__file__).parent / "icon.ico"
    images[0].save(
        out,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"保存しました: {out}")

    # PNG プレビューも書き出す（確認用）
    preview = Path(__file__).parent / "icon_preview.png"
    images[0].save(preview)
    print(f"プレビュー:   {preview}")


if __name__ == "__main__":
    main()
