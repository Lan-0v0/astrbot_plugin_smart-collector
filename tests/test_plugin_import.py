import importlib.util
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[1]


class Decorators:
    class EventMessageType:
        ALL = "all"

    @staticmethod
    def _decorator(*args, **kwargs):
        def apply(function):
            return function

        return apply

    command = _decorator
    event_message_type = _decorator
    llm_tool = _decorator


def test_plugin_module_loads_with_official_api_surface(monkeypatch) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    class Component:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path)

    for name in ("Plain", "File", "Image", "Video", "Record", "Node"):
        setattr(components, name, type(name, (Component,), {}))
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.MessageChain = type("MessageChain", (), {})
    event.MessageEventResult = type("MessageEventResult", (), {})
    event.filter = Decorators
    star.Context = type("Context", (), {})
    star.Star = Star
    star.StarTools = type("StarTools", (), {})
    star.register = Decorators._decorator

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.message_components": components,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    package = types.ModuleType("plugin_under_test")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "plugin_under_test", package)
    spec = importlib.util.spec_from_file_location("plugin_under_test.main", ROOT / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.SmartCollectorPlugin.__name__ == "SmartCollectorPlugin"
