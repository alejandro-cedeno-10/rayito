"""Genera las capas WebP de la ilustración animada de la portada de la documentación.

Se ejecuta una sola vez y se versionan sus salidas (`docs/site/docs/assets/rayito/art/`
y `og-card.jpg`); no forma parte del build del sitio. El kit con las fuentes (la
placa limpia y las capas recortadas) no se versiona: se pasa como argumento.

    uv run --with pillow==12.3.0 --with numpy==2.5.4 python scripts/build_landing_art.py \
        --kit <carpeta-del-kit> --out docs/site/docs/assets/rayito

Cada número sale de `source/animate-v2.cjs` del kit, el guion que dibujó el
vídeo aprobado: las cajas de las capas sobre la placa de 1672 x 941, las
fórmulas de peso de las colas y las alas, y el polígono `rail` de la barandilla
que va delante de la gata. El CSS no puede deformar una malla, así que cada
sprite deformado se parte en dos imágenes del mismo tamaño (cuerpo y cola, o
cuerpo y ala) con una banda de solape: en reposo la pila reproduce el sprite
original y en movimiento el cuerpo tapa la unión.

Además de las capas escribe, fuera de `--out`, la imagen fija de reposo
`art-still-1672.png` si se pasa `--still`: es la referencia de la comprobación
de alineación del CSS y no se versiona.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PLATE_W, PLATE_H = 1672, 941
PLATE_SMALL_W = 960
PLATE_QUALITY = 80
LAYER_QUALITY = 90
SMOOTH_MOVING = (0.0, 0.1)
SMOOTH_STATIC = (0.25, 0.6)

OG_W, OG_H = 1200, 630
OG_QUALITY = 80

GEAR_BOXES = {"gear-1": (549, 301), "gear-2": (792, 316), "gear-3": (1044, 332)}
GEAR_SIZE = 58
MALE_BOX = (82, 585, 254, 229)
FEMALE_BOX = (1333, 345, 173, 198)
CUBE_BOX = (1458, 277, 188, 113)
RAIL_BOX = (1310, 497, 165, 96)
RAIL_POLYGONS = [
    [(1324, 497), (1454, 503), (1458, 518), (1445, 520), (1444, 514), (1324, 507)],
    [(1330, 501), (1341, 503), (1340, 554), (1330, 555)],
    [(1424, 507), (1438, 508), (1437, 558), (1425, 559)],
    [(1445, 512), (1458, 512), (1458, 551), (1446, 552)],
    [(1310, 548), (1475, 548), (1475, 593), (1310, 593)],
]
PACKET_SCALE = 0.72
BELT_START = (1348, 678)
BELT_TRAVEL = (247, 68)
BELT_PACKET_DRAW = (90 * PACKET_SCALE, 86 * PACKET_SCALE)
BELT_PACKET_ANCHOR = (45 * PACKET_SCALE, 73 * PACKET_SCALE)
ARROW_PATH = ((1334, 608), (1495, 601), (1556, 429))
ARROW_SIZE = (48, 46)
GLOW_CENTRES = [(542, 590), (819, 623), (1108, 656)]
GLOW_RADIUS = 51
GLOW_REST_ALPHA = 0.36
DOT_ROWS = [(518, 523), (790, 550), (1069, 580)]
DOT_STEP = 9
DOT_RADIUS = 2.2
DOT_REST_ALPHA = 0.6
DOT_COLOR = (83, 194, 255)
SIGNAL_START = (111, 431)
SIGNAL_RADIUS = 5


def clamp(values: np.ndarray) -> np.ndarray:
    return np.clip(values, 0.0, 1.0)


def smoothstep(low: float, high: float, values: np.ndarray) -> np.ndarray:
    x = clamp((values - low) / (high - low))
    return x * x * (3 - 2 * x)


def grid(image: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """Coordenadas de píxel de origen (sx, sy) de cada punto del sprite."""
    sy, sx = np.mgrid[0 : image.height, 0 : image.width].astype(np.float64)
    return sx, sy


def male_weight(image: Image.Image) -> np.ndarray:
    sx, sy = grid(image)
    return clamp((100 - sx) / 90) * clamp((sy - 115) / 35) * clamp((245 - sy) / 30)


def female_weight(image: Image.Image) -> np.ndarray:
    sx, sy = grid(image)
    return clamp((75 - sx) / 65) * clamp((sy - 75) / 30) * clamp((200 - sy) / 40)


def wing_weights(image: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    sx, _ = grid(image)
    return clamp((83 - sx) / 18), clamp((sx - 158) / 18)


def with_alpha(image: Image.Image, factor: np.ndarray) -> Image.Image:
    """Copia de la imagen con el alfa multiplicado por `factor`, sin tocar el color."""
    data = np.array(image.convert("RGBA"))
    data[..., 3] = np.round(data[..., 3] * factor).astype(np.uint8)
    return Image.fromarray(data, "RGBA")


def split_by_weight(
    image: Image.Image, weight: np.ndarray
) -> tuple[Image.Image, Image.Image]:
    """(cuerpo, parte móvil): el cuerpo pierde sólo la zona lejana de la unión."""
    moving = with_alpha(image, smoothstep(*SMOOTH_MOVING, weight))
    body = with_alpha(image, 1 - smoothstep(*SMOOTH_STATIC, weight))
    return body, moving


def rail_front(plate: Image.Image) -> Image.Image:
    """Recorte de la placa con la máscara antialiasada del polígono `rail`."""
    left, top, width, height = RAIL_BOX
    scale = 8
    mask = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(mask)
    for polygon in RAIL_POLYGONS:
        draw.polygon(
            [((x - left) * scale, (y - top) * scale) for x, y in polygon], fill=255
        )
    mask = mask.resize((width, height), Image.Resampling.BOX)
    crop = plate.crop((left, top, left + width, top + height)).convert("RGBA")
    crop.putalpha(mask)
    return crop


def encode_webp(image: Image.Image, path: Path, *, lossy_rgb: bool) -> None:
    """Codifica con `cwebp` si existe (medido en la especificación) o con Pillow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cwebp = shutil.which("cwebp")
    if cwebp is None:
        image.save(
            path,
            "WEBP",
            quality=PLATE_QUALITY if lossy_rgb else LAYER_QUALITY,
            method=6,
            exact=not lossy_rgb,
            alpha_quality=100,
        )
        return
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.png"
        image.save(source)
        if lossy_rgb:
            args = ["-q", str(PLATE_QUALITY), "-m", "6", "-sharp_yuv"]
        else:
            args = ["-q", str(LAYER_QUALITY), "-alpha_q", "100", "-exact", "-m", "6"]
        subprocess.run(
            [cwebp, "-quiet", *args, str(source), "-o", str(path)], check=True
        )


def radial_glow(radius: int) -> np.ndarray:
    """Brillo RGBA como el degradado del guion, a alfa completo.

    Los tres topes (centro, 55 %, borde) se interpolan premultiplicados, como
    hace el degradado del canvas y `radial-gradient` de CSS.
    """
    size = radius * 2 + 2
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    dist = np.hypot(xx - size / 2 + 0.5, yy - size / 2 + 0.5) / radius
    positions = [0.039, 0.55, 1.0]
    alphas = [1.0, 0.25, 0.0]
    colors = [(255, 247, 185), (251, 140, 0), (251, 140, 0)]
    alpha = np.interp(dist, positions, alphas)
    channels = []
    for index in range(3):
        premult = np.interp(
            dist, positions, [a * c[index] for a, c in zip(alphas, colors)]
        )
        channels.append(np.where(alpha > 0, premult / np.maximum(alpha, 1e-6), 0))
    rgba = np.stack([*channels, alpha * 255], axis=-1)
    rgba[..., 3] = np.where(dist <= 1, rgba[..., 3], 0)
    return np.clip(np.round(rgba), 0, 255).astype(np.uint8)


def paste(
    base: Image.Image,
    layer: Image.Image,
    x: float,
    y: float,
    size: tuple[float, float],
    opacity: float = 1.0,
) -> None:
    """Pega `layer` escalada a `size` con su esquina en (x, y); admite medios píxeles."""
    width, height = round(size[0]), round(size[1])
    scaled = layer.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
    if opacity < 1:
        scaled = with_alpha(scaled, np.full((height, width), opacity))
    base.alpha_composite(scaled, (round(x), round(y)))


def quad_point(t: float) -> tuple[float, float]:
    s = 1 - t
    a, b, c = ARROW_PATH
    return (
        s * s * a[0] + 2 * s * t * b[0] + t * t * c[0],
        s * s * a[1] + 2 * s * t * b[1] + t * t * c[1],
    )


def compose_still(plate: Image.Image, layers: dict[str, Image.Image]) -> Image.Image:
    """Escena en reposo: lo que se ve con movimiento reducido (y el CSS sin animar)."""
    canvas = plate.convert("RGBA")
    for name, (x, y) in GEAR_BOXES.items():
        paste(canvas, layers[name], x, y, (GEAR_SIZE, GEAR_SIZE))
    paste(canvas, layers["male"], *MALE_BOX[:2], MALE_BOX[2:])
    paste(canvas, layers["female"], *FEMALE_BOX[:2], FEMALE_BOX[2:])
    canvas.alpha_composite(rail_front(plate), RAIL_BOX[:2])
    paste(canvas, layers["winged-cube"], *CUBE_BOX[:2], CUBE_BOX[2:])
    for index in range(3):
        u = (1 + 2 * index) / 6
        paste(
            canvas,
            layers["packet"],
            BELT_START[0] + u * BELT_TRAVEL[0] - BELT_PACKET_ANCHOR[0],
            BELT_START[1] + u * BELT_TRAVEL[1] - BELT_PACKET_ANCHOR[1],
            BELT_PACKET_DRAW,
        )
    x, y = quad_point(0.5)
    paste(
        canvas,
        layers["packet"],
        x - ARROW_SIZE[0] / 2,
        y - ARROW_SIZE[1] / 2,
        ARROW_SIZE,
    )
    glow = Image.fromarray(radial_glow(GLOW_RADIUS), "RGBA")
    for cx, cy in GLOW_CENTRES:
        paste(
            canvas, glow, cx - GLOW_RADIUS, cy - GLOW_RADIUS, glow.size, GLOW_REST_ALPHA
        )
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for cx, cy in DOT_ROWS:
        for k in range(3):
            px = cx + k * DOT_STEP
            draw.ellipse(
                [px - DOT_RADIUS, cy - DOT_RADIUS, px + DOT_RADIUS, cy + DOT_RADIUS],
                fill=(*DOT_COLOR, round(255 * DOT_REST_ALPHA)),
            )
    sx, sy = SIGNAL_START
    draw.ellipse(
        [
            sx - SIGNAL_RADIUS,
            sy - SIGNAL_RADIUS,
            sx + SIGNAL_RADIUS,
            sy + SIGNAL_RADIUS,
        ],
        fill=(255, 255, 255, 255),
    )
    canvas.alpha_composite(overlay)
    return canvas


def social_card(still: Image.Image) -> Image.Image:
    """1200 x 630 sobre blanco: la imagen fija escalada a la altura de la tarjeta."""
    width = round(still.width * OG_H / still.height)
    card = Image.new("RGB", (OG_W, OG_H), (255, 255, 255))
    scaled = still.convert("RGB").resize((width, OG_H), Image.Resampling.LANCZOS)
    card.paste(scaled, ((OG_W - width) // 2, 0))
    return card


def build(kit: Path, out: Path, still_path: Path | None) -> None:
    layer_dir = kit / "source" / "layers"
    plate = Image.open(kit / "source" / "clean-plate.png").convert("RGB")
    if plate.size != (PLATE_W, PLATE_H):
        raise SystemExit(
            f"la placa mide {plate.size}, se esperaba {(PLATE_W, PLATE_H)}"
        )
    art = out / "art"
    for old in art.glob("*.webp"):
        old.unlink()

    encode_webp(plate, art / f"plate-{PLATE_W}.webp", lossy_rgb=True)
    small = plate.resize(
        (PLATE_SMALL_W, round(PLATE_H * PLATE_SMALL_W / PLATE_W)),
        Image.Resampling.LANCZOS,
    )
    encode_webp(small, art / f"plate-{PLATE_SMALL_W}.webp", lossy_rgb=True)

    layers = {p.stem: Image.open(p).convert("RGBA") for p in layer_dir.glob("*.png")}
    for name in GEAR_BOXES:
        encode_webp(layers[name], art / f"{name}.webp", lossy_rgb=False)
    encode_webp(layers["output-packet"], art / "packet.webp", lossy_rgb=False)
    layers["packet"] = layers["output-packet"]

    male = layers["male"]
    body, tail = split_by_weight(male, male_weight(male))
    encode_webp(body, art / "male-body.webp", lossy_rgb=False)
    encode_webp(tail, art / "male-tail.webp", lossy_rgb=False)
    female = layers["female"]
    body, tail = split_by_weight(female, female_weight(female))
    encode_webp(body, art / "female-body.webp", lossy_rgb=False)
    encode_webp(tail, art / "female-tail.webp", lossy_rgb=False)

    cube = layers["winged-cube"]
    left, right = wing_weights(cube)
    both = np.maximum(left, right)
    encode_webp(
        with_alpha(cube, 1 - smoothstep(*SMOOTH_STATIC, both)),
        art / "cube-body.webp",
        lossy_rgb=False,
    )
    encode_webp(
        with_alpha(cube, smoothstep(*SMOOTH_MOVING, left)),
        art / "cube-wing-l.webp",
        lossy_rgb=False,
    )
    encode_webp(
        with_alpha(cube, smoothstep(*SMOOTH_MOVING, right)),
        art / "cube-wing-r.webp",
        lossy_rgb=False,
    )
    encode_webp(rail_front(plate), art / "rail-front.webp", lossy_rgb=False)

    still = compose_still(plate, layers)
    if still_path is not None:
        still.convert("RGB").save(still_path)
    social_card(still).save(
        out / "og-card.jpg",
        "JPEG",
        quality=OG_QUALITY,
        subsampling=2,
        optimize=True,
        progressive=True,
    )
    total = sum(p.stat().st_size for p in art.glob("*.webp"))
    print(f"{len(list(art.glob('*.webp')))} capas, {total / 1024:.0f} KB en {art}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--kit", type=Path, required=True, help="carpeta del kit con source/"
    )
    parser.add_argument("--out", type=Path, required=True, help="carpeta assets/rayito")
    parser.add_argument(
        "--still",
        type=Path,
        help="dónde escribir la imagen fija de referencia (no se versiona)",
    )
    args = parser.parse_args()
    build(args.kit, args.out, args.still)


if __name__ == "__main__":
    main()
