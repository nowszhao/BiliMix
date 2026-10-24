"""Bounded retries for resumable translation calls."""

import time
from core import config


def _run_with_retry(
    fn,
    *args,
    max_retries=None,
    cancel_check=None,
    progress_cb=None,
    resume_batch=0,
    existing_translations=None,
    checkpoint_cb=None,
    name="task",
    **kwargs,
):
    """Generic auto-retry with cancellation support."""
    auto_retry_max = getattr(config, "AUTO_RETRY_MAX", 2)
    retries = max_retries if max_retries is not None else auto_retry_max
    kwargs.setdefault("cancel_check", cancel_check)
    kwargs.setdefault("progress_cb", progress_cb)
    kwargs.setdefault("resume_batch", resume_batch)
    kwargs.setdefault("existing_translations", existing_translations)
    kwargs.setdefault("checkpoint_cb", checkpoint_cb)

    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except InterruptedError:
            raise
        except Exception as e:
            if cancel_check and cancel_check():
                raise
            last_err = e
            if attempt >= retries:
                break
            wait = 2**attempt
            print(
                f"[Retry] {name} failed (attempt {attempt + 1}/{retries + 1}), "
                f"retry in {wait}s: {e}"
            )
            time.sleep(wait)

    raise RuntimeError(f"{name} failed after {retries + 1} attempts: {last_err}")
