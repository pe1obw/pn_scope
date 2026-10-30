"""Driver registry. Decorate a SpectrumAnalyzer subclass with @register and import
its module in analyzers/__init__.py; it then shows up in the GUI."""
DRIVERS: dict[str, type] = {}


def register(cls):
    DRIVERS[cls.model_name] = cls
    return cls


def create(model_name: str, host: str = ""):
    cls = DRIVERS[model_name]
    return cls(host) if getattr(cls, "needs_host", False) else cls()
