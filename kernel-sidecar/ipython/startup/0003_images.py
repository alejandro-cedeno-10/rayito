"""``_repr_png_``/``_repr_jpeg_`` for PIL images when Pillow does not provide
them, so an image as the last expression yields ``png``/``jpeg``."""


def _rayito_install_image_reprs():
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow missing
        return
    from io import BytesIO

    def _encode(image, fmt):
        buffer = BytesIO()
        try:
            image.save(buffer, format=fmt)
        except Exception:  # noqa: BLE001 - mode not encodable in that format
            return None
        return buffer.getvalue()

    if not hasattr(Image.Image, "_repr_png_"):
        Image.Image._repr_png_ = lambda self: _encode(self, "PNG")
    if not hasattr(Image.Image, "_repr_jpeg_"):
        Image.Image._repr_jpeg_ = lambda self: _encode(self.convert("RGB"), "JPEG")


_rayito_install_image_reprs()
del _rayito_install_image_reprs
