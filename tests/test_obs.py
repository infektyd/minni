import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import minni.obs as obs


def test_json_formatter_emits_parseable_object():
    formatter = obs.JsonLogFormatter()
    record = logging.LogRecord(
        name="minnid",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    parsed = json.loads(formatter.format(record))
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "minnid"
    assert parsed["message"] == "hello world"
    assert parsed["ts"].endswith("Z")


def test_json_formatter_includes_exception_text():
    formatter = obs.JsonLogFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="minnid",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "ValueError: boom" in parsed["exc"]


def test_configure_logging_is_idempotent_and_honors_env(monkeypatch):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_flag = getattr(root, obs._CONFIGURED_FLAG, False)
    try:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        if hasattr(root, obs._CONFIGURED_FLAG):
            delattr(root, obs._CONFIGURED_FLAG)

        monkeypatch.setenv("MINNI_LOG_FORMAT", "json")
        monkeypatch.setenv("MINNI_LOG_LEVEL", "DEBUG")

        obs.configure_logging()
        count_after_first = len(root.handlers)
        obs.configure_logging()
        count_after_second = len(root.handlers)

        assert count_after_first == 1
        assert count_after_second == 1  # idempotent: no duplicate handlers
        assert root.level == logging.DEBUG
        assert isinstance(root.handlers[0].formatter, obs.JsonLogFormatter)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        if saved_flag:
            setattr(root, obs._CONFIGURED_FLAG, True)
        elif hasattr(root, obs._CONFIGURED_FLAG):
            delattr(root, obs._CONFIGURED_FLAG)


def test_counters_increment_and_snapshot():
    counters = obs.Counters()
    counters.incr("errors")
    counters.incr("errors")
    counters.incr("errors.search", 3)
    snap = counters.snapshot()
    assert snap["errors"] == 2
    assert snap["errors.search"] == 3
    assert counters.get("missing") == 0


def test_status_surfaces_error_counters(monkeypatch):
    import minni.minnid as minnid

    monkeypatch.setattr(minnid, "_request_count", 0)
    obs.METRICS.reset()
    obs.incr("errors")
    obs.incr("errors.search")

    daemon = minnid._handle_status({}, 1)["result"]["daemon"]
    assert daemon["errors"] == 1
    assert daemon["counters"]["errors.search"] == 1
    obs.METRICS.reset()
