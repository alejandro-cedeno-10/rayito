"""``e2b/data`` mime type for pandas frames and series: ``to_dict(orient="list")``
with numpy scalars turned into plain Python values.

The pandas types are matched by module and class name at lookup time, so
this script imports no pandas module at kernel start (the ``slim`` variant
starts with no scientific package loaded); the full warm-up imports pandas
anyway."""


def _rayito_install_data_formatter():
    from IPython.core.formatters import BaseFormatter
    from IPython.core.getipython import get_ipython
    from traitlets import ObjectName, Unicode

    def _plain(value):
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return item()
            except (TypeError, ValueError):
                return str(value)
        return value

    def _frame_repr(frame):
        try:
            return {str(k): [_plain(v) for v in vs] for k, vs in frame.to_dict(orient="list").items()}
        except Exception:  # noqa: BLE001 - text/html still travel
            return None

    def _series_repr(series):
        try:
            return _frame_repr(series.to_frame())
        except Exception:  # noqa: BLE001
            return None

    printers = {
        ("pandas.core.frame", "DataFrame"): _frame_repr,
        ("pandas.core.series", "Series"): _series_repr,
    }

    def _printer_for(typ):
        if not isinstance(typ, type):
            return None
        for cls in typ.__mro__:
            printer = printers.get((cls.__module__, cls.__name__))
            if printer is not None:
                return printer
        return None

    class _DataFormatter(BaseFormatter):
        format_type = Unicode("e2b/data")
        print_method = ObjectName("_repr_e2b_data_")
        _return_type = (dict, str)

        def lookup_by_type(self, typ):
            printer = _printer_for(typ)
            if printer is not None:
                return printer
            return super().lookup_by_type(typ)

    shell = get_ipython()
    if shell is None:
        return
    shell.display_formatter.formatters["e2b/data"] = _DataFormatter(parent=shell.display_formatter)


_rayito_install_data_formatter()
del _rayito_install_data_formatter
