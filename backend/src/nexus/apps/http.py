"""HTTP for the live apps.

Every request opens a fresh connection and closes it, retries once after a short pause on a rate limit or server
error (honouring Retry-After), and turns any failure into an AppError the agent can read as data.
"""

import time
from collections.abc import Callable, Collection
from typing import Any

import httpx

from nexus.apps.base import AppError

ClientFactory = Callable[[], httpx.Client]
RETRY_STATUSES = frozenset({429, 500, 502, 503})
DEFAULT_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 5.0
MAX_PAGES = 20


def retry_wait(response: httpx.Response | None) -> float:
    """Seconds to pause before retrying: the app's Retry-After if it sent one (capped), otherwise one second."""
    header = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        return min(max(float(header), 0.0), MAX_WAIT_SECONDS)
    except ValueError:
        return DEFAULT_WAIT_SECONDS


def error_message(response: httpx.Response) -> str:
    """The app's own error message when it sent JSON (Stripe style), otherwise the start of the body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return response.text[:200]


def request(new_client: ClientFactory, app: str, method: str, url: str, *, ok_statuses: Collection[int] = (),
            sleep: Callable[[float], None] = time.sleep, **kwargs: Any) -> httpx.Response:
    for attempt in (1, 2):
        try:
            with new_client() as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise AppError(f"{app} unreachable: {exc}") from exc
            sleep(DEFAULT_WAIT_SECONDS)
            continue
        if response.status_code in RETRY_STATUSES and attempt == 1:
            sleep(retry_wait(response))
            continue
        if response.status_code >= 400 and response.status_code not in ok_statuses:
            raise AppError(f"{app} {response.status_code}: {error_message(response)}")
        return response
    raise AppError(f"{app} did not respond")


def _json(app: str, response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise AppError(f"{app} returned a response that isn't JSON") from exc


def request_json(new_client: ClientFactory, app: str, method: str, url: str, **kwargs: Any) -> Any:
    return _json(app, request(new_client, app, method, url, **kwargs))


def get_json_or_none(new_client: ClientFactory, app: str, url: str, **params: Any) -> Any | None:
    """GET that treats 404 as "doesn't exist yet" rather than an error."""
    response = request(new_client, app, "GET", url, ok_statuses={404}, params=params)
    return None if response.status_code == 404 else _json(app, response)


def collect_pages(fetch_page: Callable[[int], list[Any]], page_size: int, max_pages: int = MAX_PAGES) -> list[Any]:
    """Fetch offset-based pages until one comes back short, up to max_pages."""
    items: list[Any] = []
    for page in range(max_pages):
        batch = fetch_page(page * page_size)
        items += batch
        if len(batch) < page_size:
            break
    return items
