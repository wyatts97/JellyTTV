"""Getting a live/offline change into Jellyfin's Live TV guide.

Two things stood between a channel going live and the guide saying so, and
neither was visible from this side: arq's dedupe suppressed the reactive job for
an hour, and Jellyfin caches the downloaded XMLTV on disk for an hour regardless
of cache headers. These pin both fixes.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.services.jellyfin import JellyfinClient
from app.worker.queue import coalesced_job_id

BASE = "http://jellyfin:8096"
GUIDE = "http://jellyttv:8730/tuner/guide.xml"


def livetv_config(providers: list[dict]) -> dict:
    return {"ListingProviders": providers, "TunerHosts": []}


# ------------------------------------------------------------------ job ids
def test_a_coalesced_job_id_dedupes_a_burst_but_not_the_hour():
    """arq refuses an id whose *result* is still stored - `keep_result` is 1h.

    A fixed id therefore meant "run at most once an hour", which is how every
    reactive guide refresh after the first was silently dropped.
    """
    a = coalesced_job_id("jellyfin_refresh_guide", window=10_000_000_000)
    b = coalesced_job_id("jellyfin_refresh_guide", window=10_000_000_000)
    assert a == b, "triggers inside one window must still coalesce"

    near = coalesced_job_id("jellyfin_refresh_guide", window=1)
    later = coalesced_job_id("jellyfin_refresh_guide", window=1)
    # Same second or the next; either way the id is not pinned for an hour.
    assert near.rsplit(":", 1)[0] == "jellyfin_refresh_guide"
    assert later.rsplit(":", 1)[0] == "jellyfin_refresh_guide"
    assert a != near


# -------------------------------------------------------- cache-busting refresh
@respx.mock
async def test_forcing_a_refresh_recreates_the_provider_to_change_its_cache_key():
    """Jellyfin keys its one-hour guide cache on the provider id.

    So the only way to make it genuinely re-download from outside the Jellyfin
    host is to give it a provider with a different id. Posting a blank Id makes
    Jellyfin mint a fresh GUID and queue the refresh itself.
    """
    provider = {
        "Id": "old-provider-id",
        "Type": "xmltv",
        "Path": f"{GUIDE}?key=stale-token",
        "EnableAllTuners": True,
        "ChannelMappings": [{"Name": "twitch.adapt"}],
    }
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    posted = respx.post(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(200, json={"Id": "new-provider-id"})
    )
    deleted = respx.delete(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(204)
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.force_guide_refresh(GUIDE) is True

    body = json.loads(posted.calls[0].request.read())
    assert body["Id"] == "", "a blank id is what makes Jellyfin mint a new one"
    # Everything else must survive, or the recreate costs the user their setup.
    assert body["ChannelMappings"] == [{"Name": "twitch.adapt"}]
    assert body["EnableAllTuners"] is True
    assert body["Path"] == provider["Path"]

    assert deleted.called
    assert deleted.calls[0].request.url.params["id"] == "old-provider-id"


@respx.mock
async def test_the_new_provider_is_added_before_the_old_one_is_removed():
    """A failure halfway must leave Live TV with a provider, not none."""
    order: list[str] = []
    provider = {"Id": "old", "Type": "xmltv", "Path": GUIDE}

    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )

    def on_post(request):
        order.append("post")
        return httpx.Response(200, json={})

    def on_delete(request):
        order.append("delete")
        return httpx.Response(204)

    respx.post(f"{BASE}/LiveTv/ListingProviders").mock(side_effect=on_post)
    respx.delete(f"{BASE}/LiveTv/ListingProviders").mock(side_effect=on_delete)

    async with JellyfinClient(BASE, "key") as client:
        await client.force_guide_refresh(GUIDE)

    assert order == ["post", "delete"]


@respx.mock
async def test_a_stale_token_in_the_stored_path_still_matches():
    """The stored Path carries the tuner token, which may have been rotated."""
    provider = {"Id": "old", "Type": "xmltv", "Path": f"{GUIDE}?key=rotated-away"}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    respx.post(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(200, json={}))
    respx.delete(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(204))

    async with JellyfinClient(BASE, "key") as client:
        assert await client.force_guide_refresh(f"{GUIDE}?key=current-token") is True


@respx.mock
async def test_no_matching_provider_reports_failure_so_the_caller_falls_back():
    other = {"Id": "x", "Type": "xmltv", "Path": "http://elsewhere/guide.xml"}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([other]))
    )
    creating = respx.post(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(200, json={})
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.force_guide_refresh(GUIDE) is False
    assert not creating.called, "must not touch a provider that is not ours"


@respx.mock
async def test_a_failed_delete_still_counts_as_refreshed():
    """The refresh is already queued against the new provider by then."""
    provider = {"Id": "old", "Type": "xmltv", "Path": GUIDE}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    respx.post(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(200, json={}))
    respx.delete(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(500))

    async with JellyfinClient(BASE, "key") as client:
        assert await client.force_guide_refresh(GUIDE) is True
