from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Callable, Iterable


@dataclass(frozen=True)
class PlatformResult:
    platform: str
    outcome: str
    retryable: bool = False
    returncode: int = 0
    fresh_output: bool = False
    attempts: int = 1
    duration_seconds: float = 0.0
    error_message: str = ""
    started: bool = False


def _failed_result(platform: str, exc: Exception, *, attempts: int) -> PlatformResult:
    return PlatformResult(
        platform=platform,
        outcome="failed",
        retryable=attempts < 2,
        returncode=1,
        attempts=attempts,
        error_message=str(exc) or repr(exc),
    )


def run_bounded(
    platforms: Iterable[str],
    run_one: Callable[[str], PlatformResult],
    *,
    max_workers: int = 2,
) -> dict[str, PlatformResult]:
    ordered = list(platforms)
    if not ordered:
        return {}
    worker_count = max(1, min(int(max_workers), 2, len(ordered)))
    results: dict[str, PlatformResult] = {}

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="platform") as pool:
        futures = {pool.submit(run_one, platform): platform for platform in ordered}
        for future in as_completed(futures):
            platform = futures[future]
            try:
                result = future.result()
                results[platform] = replace(result, attempts=max(int(result.attempts), 1))
            except Exception as exc:
                results[platform] = _failed_result(platform, exc, attempts=1)

    for platform in ordered:
        first = results[platform]
        if first.outcome == "success" or not first.retryable:
            continue
        try:
            retried = run_one(platform)
            results[platform] = replace(retried, attempts=2, retryable=False)
        except Exception as exc:
            results[platform] = _failed_result(platform, exc, attempts=2)

    return {platform: results[platform] for platform in ordered}
