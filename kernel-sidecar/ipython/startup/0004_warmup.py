"""Warm-up run in every kernel before it answers ``kernel_info``: the
scientific stack is imported, one figure is rendered and extracted, one frame
is described and one matrix inverted, so the snapshot (and ``/validate``'s
prefetch sampling) already holds those pages. The user namespace stays clean:
everything lives inside this function.

Which imports stay is a measured rule (M6, ``AWS_API_NOTES.md`` Q50), not a
size knob: ``scipy.stats`` and ``sklearn.linear_model`` leave this list only
if their combined RSS delta exceeds 100 MB. Measured on ``rayito-base`` 10.0
as uid 1000 (``ru_maxrss`` after each import): numpy +10.5 MB, pandas
+42.3 MB, matplotlib.pyplot + savefig +30.2 MB, scipy.stats +51.6 MB,
sklearn.linear_model +21.7 MB; scipy + sklearn = 73.3 MB < 100 MB, so the
full list stays. numpy, pandas and matplotlib always stay: ``/validate`` and
the acceptance tests use them.

Build flag: a sibling file ``warmup_variant`` whose stripped content is
``slim`` skips the warm-up entirely (nothing is imported). The repository
never contains that file; ``scripts/image_zip.py --variant slim`` writes it
into the artifact only, so a slim image differs from the full one by the
memory snapshot alone. Any other content, or no file, means the full warm-up.
An image environment variable cannot carry this flag: ``rayd`` builds the
sidecar's and the kernel's environments from scratch."""


def _rayito_warmup_variant():
    from pathlib import Path

    marker = Path(__file__).with_name("warmup_variant")
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return "full"


def _rayito_warmup():
    if _rayito_warmup_variant() == "slim":
        return

    from io import BytesIO

    import numpy
    import pandas
    import matplotlib
    import matplotlib.pyplot as plt

    try:
        import scipy
        import scipy.stats  # noqa: F401
    except Exception:  # noqa: BLE001 - optional in a trimmed image
        pass
    try:
        import sklearn
        import sklearn.linear_model  # noqa: F401
    except Exception:  # noqa: BLE001 - optional in a trimmed image
        pass

    figure, axis = plt.subplots()
    axis.plot([0, 1])
    figure.savefig(BytesIO(), format="png")
    try:
        from rayito_kernel_sidecar._vendor.e2b_charts import chart_figure_to_chart

        chart_figure_to_chart(figure)
    except Exception:  # noqa: BLE001 - the extractor is exercised, not asserted
        pass
    plt.close(figure)

    pandas.DataFrame({"a": [1.0]}).describe()
    numpy.linalg.inv(numpy.array([[2.0, 1.0], [1.0, 3.0]]))


_rayito_warmup()
del _rayito_warmup
del _rayito_warmup_variant
