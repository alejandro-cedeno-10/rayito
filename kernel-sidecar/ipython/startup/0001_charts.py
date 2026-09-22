"""``e2b/chart`` mime type for matplotlib figures through the vendored
``e2b_charts`` extractor. A figure the extractor cannot parse still yields
its ``image/png``: the formatter returns ``None`` on any failure.

The figure type is matched by module and class name at lookup time, so this
script imports no matplotlib module (a kernel whose warm-up is disabled,
``warmup_variant`` = ``slim``, starts with no scientific package loaded) and
the registration survives IPython's inline backend setup, which pops
``Figure`` from every formatter's registry on the first ``pyplot`` import
(``select_figure_formats``) and so wipes a ``for_type_by_name`` entry."""


def _rayito_install_chart_formatter():
    from IPython.core.formatters import BaseFormatter
    from IPython.core.getipython import get_ipython
    from traitlets import ObjectName, Unicode

    figure_type = ("matplotlib.figure", "Figure")

    def _repr_e2b_chart_(figure):
        try:
            from rayito_kernel_sidecar._vendor.e2b_charts import chart_figure_to_chart

            chart = chart_figure_to_chart(figure)
            return chart.model_dump(mode="json") if chart is not None else None
        except Exception:  # noqa: BLE001 - the PNG still travels
            return None

    def _is_figure(typ):
        if not isinstance(typ, type):
            return False
        return any((cls.__module__, cls.__name__) == figure_type for cls in typ.__mro__)

    class _ChartFormatter(BaseFormatter):
        format_type = Unicode("e2b/chart")
        print_method = ObjectName("_repr_e2b_chart_")
        _return_type = (dict, str)

        def lookup_by_type(self, typ):
            if _is_figure(typ):
                return _repr_e2b_chart_
            return super().lookup_by_type(typ)

    shell = get_ipython()
    if shell is None:
        return
    shell.display_formatter.formatters["e2b/chart"] = _ChartFormatter(parent=shell.display_formatter)


_rayito_install_chart_formatter()
del _rayito_install_chart_formatter
