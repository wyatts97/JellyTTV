"""Jellyfin server client.

Auth uses an API key created in Jellyfin's Dashboard -> API Keys, passed as
`Authorization: MediaBrowser Token=<key>`.

Endpoints used:
  GET  /System/Info            - connectivity + version check
  GET  /Library/VirtualFolders - enumerate libraries so the user can pick one
  POST /Library/Refresh        - full library scan (requires elevation)
  POST /Items/{id}/Refresh     - targeted refresh, preferred when we know the id
  GET  /Items?...              - locate our series items by name
  GET  /ScheduledTasks         - resolve the "Refresh Guide" task's id
  POST /ScheduledTasks/Running/{taskId} - trigger the Live TV guide refresh
  GET  /System/Configuration/livetv     - read the XMLTV listings providers
  POST /LiveTv/ListingProviders         - (re)create a listings provider
  DELETE /LiveTv/ListingProviders       - remove one
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_conf import get_logger

log = get_logger(__name__)


class JellyfinError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class JellyfinLibrary:
    id: str
    name: str
    collection_type: str | None
    locations: list[str]


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise JellyfinError("Jellyfin url and api key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self._external = client
        self._client = client
        self._refresh_guide_task_id: str | None = None

    async def __aenter__(self) -> JellyfinClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None and self._external is None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f'MediaBrowser Token="{self.api_key}", Client="JellyTTV", Version="0.1.0"',
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = await self._http().request(
                method, url, params=params, json=json_body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise JellyfinError(f"could not reach Jellyfin at {self.base_url}: {exc}") from exc

        if response.status_code in (401, 403):
            raise JellyfinError(
                "Jellyfin rejected the API key (needs an admin key)", status=response.status_code
            )
        if response.status_code >= 400:
            raise JellyfinError(
                f"Jellyfin {method} {path} failed with {response.status_code}",
                status=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    async def system_info(self) -> dict[str, Any]:
        return await self._request("GET", "/System/Info") or {}

    async def libraries(self) -> list[JellyfinLibrary]:
        payload = await self._request("GET", "/Library/VirtualFolders") or []
        libraries: list[JellyfinLibrary] = []
        for entry in payload:
            libraries.append(
                JellyfinLibrary(
                    id=entry.get("ItemId") or entry.get("Id") or "",
                    name=entry.get("Name", "?"),
                    collection_type=entry.get("CollectionType"),
                    locations=list(entry.get("Locations") or []),
                )
            )
        return libraries

    async def refresh_all(self) -> None:
        await self._request("POST", "/Library/Refresh")
        log.info("triggered full jellyfin library scan")

    async def refresh_item(self, item_id: str, *, replace_metadata: bool = False) -> None:
        await self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "Recursive": "true",
                "ImageRefreshMode": "Default",
                "MetadataRefreshMode": "Default",
                "ReplaceAllImages": "false",
                "ReplaceAllMetadata": "true" if replace_metadata else "false",
            },
        )
        log.info("triggered targeted jellyfin refresh", item_id=item_id)

    async def find_series(self, name: str, *, parent_id: str | None = None) -> str | None:
        params: dict[str, Any] = {
            "searchTerm": name,
            "includeItemTypes": "Series",
            "recursive": "true",
            "limit": 5,
        }
        if parent_id:
            params["parentId"] = parent_id
        payload = await self._request("GET", "/Items", params=params) or {}
        for item in payload.get("Items", []):
            if item.get("Name", "").casefold() == name.casefold():
                return item.get("Id")
        items = payload.get("Items") or []
        return items[0].get("Id") if items else None

    async def _resolve_refresh_guide_task_id(self) -> str:
        """Look up the 'Refresh Guide' scheduled task's id.

        Jellyfin's `/ScheduledTasks/Running/{taskId}` requires the task's actual
        GUID, not its stable `Key` ("RefreshGuide") - there is no name-based
        route. The id is stable per Jellyfin install, so callers should cache it.
        """
        tasks = await self._request("GET", "/ScheduledTasks") or []
        for task in tasks:
            if task.get("Key") == "RefreshGuide":
                task_id = task.get("Id")
                if task_id:
                    return task_id
        raise JellyfinError("could not find the 'Refresh Guide' scheduled task")

    async def refresh_guide(self) -> None:
        """Trigger Jellyfin's 'Refresh Guide' scheduled task.

        This forces Jellyfin to re-fetch the XMLTV file and update the Live TV
        guide data, so channel live/offline state and programme metadata is
        current without waiting for the default 24h interval.
        """
        if self._refresh_guide_task_id is None:
            self._refresh_guide_task_id = await self._resolve_refresh_guide_task_id()

        try:
            await self._request(
                "POST", f"/ScheduledTasks/Running/{self._refresh_guide_task_id}"
            )
        except JellyfinError as exc:
            if exc.status != 404:
                raise
            # The id may have changed (e.g. across a Jellyfin upgrade); re-resolve once.
            self._refresh_guide_task_id = await self._resolve_refresh_guide_task_id()
            await self._request(
                "POST", f"/ScheduledTasks/Running/{self._refresh_guide_task_id}"
            )
        log.info("triggered jellyfin guide refresh")

    async def _listing_providers(self) -> list[dict[str, Any]]:
        config = await self._request("GET", "/System/Configuration/livetv") or {}
        return list(config.get("ListingProviders") or [])

    def _matches_guide(self, provider: dict[str, Any], guide_url: str) -> bool:
        """Is this the XMLTV provider pointed at our guide endpoint?

        Compared on the path with its query stripped, because the stored value
        carries the tuner token and ours may have been rotated since.
        """
        path = str(provider.get("Path") or provider.get("Url") or "")
        return path.split("?", 1)[0] == guide_url.split("?", 1)[0]

    async def force_guide_refresh(self, guide_url: str) -> bool:
        """Make Jellyfin genuinely re-download the guide, not re-read its cache.

        `XmlTvListingsProvider` caches the downloaded XMLTV at
        `<cache>/xmltv/<ListingsProviderInfo.Id>.xml` and reuses it for
        `_maxCacheAge` - one hour - regardless of what cache headers we send.
        Running the "Refresh Guide" task inside that hour therefore re-parses the
        *stale* file, which is why a channel going live or ending could take up
        to an hour to show up, at an arbitrary phase.

        `SaveListingProvider` mints a fresh `Guid.NewGuid()` whenever the posted
        Id is blank, and then queues the guide refresh itself. A new id means a
        new cache filename, so the download cannot be served from cache.

        The new provider is added *before* the old one is deleted: a failure
        halfway then leaves Live TV with a working provider rather than none.
        Re-posting the whole object preserves `EnabledTuners` and
        `ChannelMappings`.

        Returns False when no matching provider was found, so the caller can
        fall back to the ordinary refresh.
        """
        providers = await self._listing_providers()
        existing = next(
            (p for p in providers if self._matches_guide(p, guide_url)), None
        )
        if existing is None:
            log.warning(
                "no xmltv listings provider matches our guide url; "
                "cannot bypass Jellyfin's guide cache",
                guide_url=guide_url.split("?", 1)[0],
            )
            return False

        old_id = str(existing.get("Id") or "")
        replacement = {**existing, "Id": ""}
        await self._request(
            "POST",
            "/LiveTv/ListingProviders",
            params={"validateListings": "false", "validateLogin": "false"},
            json_body=replacement,
        )
        if old_id:
            try:
                await self._request(
                    "DELETE", "/LiveTv/ListingProviders", params={"id": old_id}
                )
            except JellyfinError as exc:
                # The refresh is already queued against the new provider, so the
                # guide is correct; we have merely left a duplicate behind.
                log.warning(
                    "could not remove the previous listings provider",
                    provider_id=old_id,
                    error=str(exc),
                )
        log.info("recreated the xmltv listings provider to bypass Jellyfin's guide cache")
        return True

    async def send_notification(
        self, *, title: str, body: str, subtitle: str | None = None
    ) -> None:
        """Push a notification to Streamyfin clients.

        Delivered through the Streamyfin companion plugin's custom-webhook
        endpoint, which authenticates with the same admin API key we already
        hold. Jellyfin's own web PWA has no Web Push support at all - its service
        worker only does offline caching - so this plugin is the one route to a
        real push notification on a phone or tablet.

        Raises `JellyfinError` with `status=404` when the plugin is not
        installed, which callers should treat as "not configured", not "broken".
        """
        payload: dict[str, Any] = {"title": title, "body": body}
        if subtitle:
            payload["subtitle"] = subtitle
        # The endpoint takes an array of notifications.
        await self._request("POST", "/Streamyfin/notification", json_body=[payload])
        log.info("sent streamyfin notification", title=title)

    async def refresh(self, *, library_id: str | None = None) -> str:
        """Refresh as narrowly as possible; returns what was actually done."""
        if library_id:
            try:
                await self.refresh_item(library_id)
                return "library"
            except JellyfinError as exc:
                log.warning("targeted refresh failed, falling back to full scan", error=str(exc))
        await self.refresh_all()
        return "full"
