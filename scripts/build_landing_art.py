"""Genera las capas WebP de la ilustración animada de la portada de la documentación.

Se ejecuta una sola vez y se versionan sus salidas (`docs/site/docs/assets/rayito/art/`
y `og-card.jpg`); no forma parte del build del sitio. El kit con las fuentes (la
placa limpia y las capas recortadas) no se versiona: se pasa como argumento.

    uv run --with pillow==12.3.0 --with numpy==2.5.4 python scripts/build_landing_art.py \
        --kit <carpeta-del-kit> --out docs/site/docs/assets/rayito

Las cajas de las capas sobre la placa de 1672 x 941 y el polígono `rail` de la
barandilla que va delante de la gata salen de `source/animate-v2.cjs` del kit.
El CSS no puede deformar una malla, así que cada sprite articulado se parte en
dos imágenes del mismo tamaño (cuerpo y cola, o cuerpo y ala) con polígonos
dibujados sobre el propio sprite: el cuerpo conserva su silueta entera y la
parte móvil va detrás, con un relleno oculto bajo el cuerpo que tapa el hueco
al girar. En reposo la pila reproduce el sprite original y en movimiento no
queda ningún trazo suelto del contorno.

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
MASK_SUPERSAMPLE = 8
UNDER_FILL_DEPTH = 5
WING_FILL = (248, 249, 253)

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
MALE_TAIL_CUT = [(0, 118), (66, 118), (66, 188), (68.5, 188), (68.5, 214), (0, 214)]
MALE_TAIL_UNDER = [(0, 118), (66, 118), (66, 188), (78, 188), (78, 214), (0, 214)]
FEMALE_TAIL_CUT = [(0, 95), (50.5, 95), (50.5, 170), (0, 170)]
FEMALE_TAIL_UNDER = [(0, 95), (51, 95), (51, 150), (60, 150), (60, 176), (0, 176)]
CUBE_SILHOUETTE = [
    (69, 43),
    (75, 38),
    (90, 30),
    (105, 24),
    (125, 22),
    (140, 26),
    (146, 32),
    (156, 32.5),
    (158, 36),
    (160.5, 45),
    (162, 60),
    (164, 70),
    (166, 85),
    (168, 98),
    (166, 104),
    (125, 124),
    (100, 118),
    (79, 112),
    (73, 100),
    (73, 88),
    (77, 83),
    (81, 79),
    (80.5, 71),
    (76.5, 70),
    (72, 64),
    (70, 58),
    (70, 50),
]
CUBE_WING_SPLIT_X = 115
WING_BANDS = {"l": (0, 84, 44, 92), "r": (140, 203, 24, 66)}
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


def polygon_mask(
    size: tuple[int, int], polygon: list[tuple[float, float]]
) -> np.ndarray:
    """Máscara antialiasada (0..1) de un polígono en coordenadas de píxel del sprite."""
    scale = MASK_SUPERSAMPLE
    mask = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(mask).polygon([(x * scale, y * scale) for x, y in polygon], fill=255)
    small = mask.resize(size, Image.Resampling.BOX)
    return np.asarray(small, dtype=np.float64) / 255


def with_alpha(image: Image.Image, factor: np.ndarray) -> Image.Image:
    """Copia de la imagen con el alfa multiplicado por `factor`, sin tocar el color."""
    data = np.array(image.convert("RGBA"))
    data[..., 3] = np.round(data[..., 3] * factor).astype(np.uint8)
    return Image.fromarray(data, "RGBA")


def split_by_polygons(
    image: Image.Image,
    cut: list[tuple[float, float]],
    under: list[tuple[float, float]],
) -> tuple[Image.Image, Image.Image]:
    """(cuerpo, parte móvil): el cuerpo pierde sólo lo que queda fuera de su contorno.

    `cut` es la parte que se mueve y desaparece del cuerpo; `under` la rodea con
    unos píxeles que siguen dentro del cuerpo y que, al ir detrás, tapan la unión.
    """
    size = image.size
    body = with_alpha(image, 1 - polygon_mask(size, cut))
    moving = with_alpha(
        image, np.maximum(polygon_mask(size, under), polygon_mask(size, cut))
    )
    return body, moving


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Dilatación cuadrada de una máscara 0..1 (el máximo en una ventana)."""
    padded = np.pad(mask, radius)
    out = np.zeros_like(mask)
    height, width = mask.shape
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out = np.maximum(out, padded[dy : dy + height, dx : dx + width])
    return out


def cube_layers(cube: Image.Image) -> tuple[Image.Image, Image.Image, Image.Image]:
    """(cuerpo, ala izquierda, ala derecha) del cubo con alas.

    El cuerpo es la silueta del cubo; cada ala es todo lo que queda fuera más una
    franja de relleno liso bajo el borde del cubo, para que al girar no asome ni
    un trozo del contorno del cubo ni un hueco.
    """
    size = cube.size
    inside = polygon_mask(size, CUBE_SILHOUETTE)
    outside = 1 - inside
    near_edge = dilate(outside, UNDER_FILL_DEPTH) * (inside > 0)
    data = np.array(cube.convert("RGBA"))
    sy, sx = np.mgrid[0 : size[1], 0 : size[0]]
    wings = []
    for side, on_side in (
        ("l", sx < CUBE_WING_SPLIT_X),
        ("r", sx >= CUBE_WING_SPLIT_X),
    ):
        left, right, top, bottom = WING_BANDS[side]
        in_window = (sx >= left) & (sx <= right) & (sy >= top) & (sy <= bottom)
        band = (near_edge > 0) & in_window
        layer = data.copy()
        keep = on_side & (inside == 0)
        layer[..., 3] = np.where(keep, data[..., 3], 0)
        layer[band] = (*WING_FILL, 255)
        layer[(inside > 0) & ~band, 3] = 0
        edge = (inside > 0) & (inside < 1) & on_side & ~band
        layer[edge, 3] = np.round(data[edge, 3] * (1 - inside[edge])).astype(np.uint8)
        wings.append(Image.fromarray(layer, "RGBA"))
    return with_alpha(cube, inside), wings[0], wings[1]


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

    for name, cut, under in (
        ("male", MALE_TAIL_CUT, MALE_TAIL_UNDER),
        ("female", FEMALE_TAIL_CUT, FEMALE_TAIL_UNDER),
    ):
        body, tail = split_by_polygons(layers[name], cut, under)
        encode_webp(body, art / f"{name}-body.webp", lossy_rgb=False)
        encode_webp(tail, art / f"{name}-tail.webp", lossy_rgb=False)

    body, wing_left, wing_right = cube_layers(layers["winged-cube"])
    encode_webp(body, art / "cube-body.webp", lossy_rgb=False)
    encode_webp(wing_left, art / "cube-wing-l.webp", lossy_rgb=False)
    encode_webp(wing_right, art / "cube-wing-r.webp", lossy_rgb=False)
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
