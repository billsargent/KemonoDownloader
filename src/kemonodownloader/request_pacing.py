"""Per-host request pacing and configurable retry backoff.

The pacer enforces a minimum spacing between outbound HTTP requests. Spacing is
configured globally and can be raised per host, so polite rates (for example one
request per second to Pawchive) can be applied without slowing every other site.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

# Defaults preserve the shipped behaviour: no global delay, one request per
# second for Pawchive, and a 1s → 30s exponential retry backoff.
DEFAULT_GLOBAL_DELAY = 0.0
DEFAULT_HOST_DELAYS: Dict[str, float] = {"pawchive.pw": 1.0}
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 30.0

# Granularity used while waiting for a host slot, so cancellation is honoured
# promptly without busy-looping.
_POLL_INTERVAL = 0.1

__all__ = [
    "RequestCancelled",
    "PacedSession",
    "RequestPacer",
    "pacer",
    "parse_host_delays",
    "format_host_delays",
    "compute_backoff",
    "paced_get",
]


class RequestCancelled(requests.RequestException):
    """Raised when a paced request is abandoned before it is sent."""


def parse_host_delays(text: str) -> Tuple[Dict[str, float], List[str]]:
    """Parse ``host=seconds`` lines.

    Returns ``(delays, errors)`` where *errors* holds the raw invalid lines.
    Blank lines and ``#`` comments are ignored.
    """
    delays: Dict[str, float] = {}
    errors: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        host, separator, value = line.partition("=")
        host = host.strip().lower().lstrip("*").lstrip(".")
        if not separator or not host:
            errors.append(line)
            continue
        try:
            seconds = float(value.strip())
        except ValueError:
            errors.append(line)
            continue
        if seconds < 0:
            errors.append(line)
            continue
        delays[host] = seconds
    return delays, errors


def format_host_delays(delays: Dict[str, float]) -> str:
    """Render host delays as ``host=seconds`` lines for the settings field."""
    return "\n".join(f"{host}={float(seconds)}" for host, seconds in delays.items())


class RequestPacer:
    """Thread-safe per-host request schedule."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed: Dict[str, float] = {}
        self._global_delay = DEFAULT_GLOBAL_DELAY
        self._host_delays = dict(DEFAULT_HOST_DELAYS)
        self._backoff_base = DEFAULT_BACKOFF_BASE
        self._backoff_cap = DEFAULT_BACKOFF_CAP

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(
        self,
        global_delay: Optional[float] = None,
        host_delays: Optional[Dict[str, float]] = None,
        backoff_base: Optional[float] = None,
        backoff_cap: Optional[float] = None,
    ) -> None:
        """Update pacing values and drop any pending host schedules."""
        with self._lock:
            if global_delay is not None:
                self._global_delay = max(0.0, float(global_delay))
            if host_delays is not None:
                self._host_delays = {
                    str(host).lower(): max(0.0, float(seconds))
                    for host, seconds in host_delays.items()
                }
            if backoff_base is not None:
                self._backoff_base = max(0.0, float(backoff_base))
            if backoff_cap is not None:
                self._backoff_cap = max(0.0, float(backoff_cap))
            self._next_allowed.clear()

    def reset(self) -> None:
        """Restore the shipped defaults."""
        self.configure(
            DEFAULT_GLOBAL_DELAY,
            DEFAULT_HOST_DELAYS,
            DEFAULT_BACKOFF_BASE,
            DEFAULT_BACKOFF_CAP,
        )

    @property
    def global_delay(self) -> float:
        with self._lock:
            return self._global_delay

    @property
    def backoff_base(self) -> float:
        with self._lock:
            return self._backoff_base

    @property
    def backoff_cap(self) -> float:
        with self._lock:
            return self._backoff_cap

    def host_delays(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._host_delays)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _host_override(self, hostname: str) -> float:
        """Return the most specific host delay; caller must hold the lock."""
        best_key = ""
        best_delay = 0.0
        for key, seconds in self._host_delays.items():
            if hostname == key or hostname.endswith("." + key):
                if len(key) >= len(best_key):
                    best_key = key
                    best_delay = seconds
        return best_delay

    def delay_for(self, url: str) -> float:
        """Return the effective delay for *url* (global floor plus host rules)."""
        hostname = (urlparse(url).hostname or "").lower()
        with self._lock:
            return max(self._global_delay, self._host_override(hostname))

    def wait(self, url: str, is_running: Optional[Callable[[], bool]] = None) -> bool:
        """Block until *url* may be requested.

        Returns ``False`` if *is_running* reports the worker stopped while waiting.
        """
        delay = self.delay_for(url)
        if delay <= 0:
            return True

        hostname = (urlparse(url).hostname or "").lower()
        while True:
            if is_running is not None and not is_running():
                return False
            with self._lock:
                now = time.monotonic()
                next_allowed = self._next_allowed.get(hostname, 0.0)
                if now >= next_allowed:
                    self._next_allowed[hostname] = now + delay
                    return True
                remaining = next_allowed - now
            time.sleep(min(max(remaining, _POLL_INTERVAL), 1.0))

    def compute_backoff(self, attempt: int) -> float:
        """Return the retry delay for *attempt* (0-based), capped when configured."""
        with self._lock:
            base = self._backoff_base
            cap = self._backoff_cap
        if base <= 0:
            return 0.0
        value = base * (2 ** max(0, int(attempt)))
        return min(value, cap) if cap > 0 else value


pacer = RequestPacer()


def compute_backoff(attempt: int) -> float:
    """Return the configured retry delay for *attempt* (0-based)."""
    return pacer.compute_backoff(attempt)


def paced_get(
    session,
    url: str,
    *,
    is_running: Optional[Callable[[], bool]] = None,
    **kwargs,
):
    """Perform ``session.get`` after waiting for the host's pacing slot.

    Raises :class:`RequestCancelled` when *is_running* reports the worker stopped
    while waiting, so callers already handling ``requests.RequestException`` do
    the right thing.
    """
    if not pacer.wait(url, is_running):
        raise RequestCancelled(f"Request to {url} cancelled before it was sent")
    return session.get(url, **kwargs)


class PacedSession:
    """Proxy that paces every ``get`` call made on a requests session.

    Useful where a session is threaded through several helper methods and
    pacing every call individually would be noisy.
    """

    def __init__(self, session, is_running: Optional[Callable[[], bool]] = None):
        self._session = session
        self._is_running = is_running

    def get(self, url: str, **kwargs):
        return paced_get(self._session, url, is_running=self._is_running, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)
