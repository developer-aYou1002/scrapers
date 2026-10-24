from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def trace_stage(config: Any, name: str) -> Iterator[None]:
    reporter = config.stage_events
    started = time.monotonic()
    if reporter is not None:
        reporter(("stage", name, "开始", None))
    try:
        yield
    except BaseException as error:
        if reporter is not None:
            reporter(("stage", name, type(error).__name__, time.monotonic() - started))
        raise
    else:
        if reporter is not None:
            reporter(("stage", name, "完成", time.monotonic() - started))


def report_stage_result(config: Any, name: str, status: str) -> None:
    if config.stage_events is not None:
        config.stage_events(("result", name, status, None))
