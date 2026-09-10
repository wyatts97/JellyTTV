"""Getting a live/offline change into Jellyfin's Live TV guide.

Two things stood between a channel going live and the guide saying so, and
neither was visible from this side: arq's dedupe suppressed the reactive job for
an hour, and Jellyfin caches the downloaded XMLTV on disk for an hour regardless
of cache headers. These pin both fixes.

The second one is Jellyfin-version-sensitive. `XmlTvListingsProvider` caches at
`<cache>/xmltv/<provider id>.xml` for an hour, so triggering the "Refresh Guide"
task inside that hour just re-parses the stale copy. Jellyfin 12.0's
`SaveListingProvider` deletes that file and queues the refresh itself, so saving
the provider *as it stands* is now the whole operation - where 10.11 needed the
provider recreated under a new id to change the cache key.
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
async def test_saving_the_provider_unchanged_is_what_busts_the_cache():
    """Jellyfin 12.0 clears the guide cache when the provider is saved.

    The provider must go back with its own `Id`, so `SaveListingProvider` takes
    the replace-in-place branch. A blank id would have Jellyfin mint a new
    provider and orphan the old one - which is what 10.11 required, and is now
    both unnecessary and destructive.
    """
    provider = {
        "Id": "provider-id",
        "Type": "xmltv",
        "Path": f"{GUIDE}?key=stale-token",
        "EnableAllTuners": True,
        "ChannelMappings": [{"Name": "twitch.adapt"}],
    }
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    posted = respx.post(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(200, json={"Id": "provider-id"})
    )
    deleted = respx.delete(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(204)
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.refresh_guide_now(GUIDE) is True

    assert posted.call_count == 1
    body = json.loads(posted.calls[0].request.read())
    assert body["Id"] == "provider-id", "the id must survive, or the provider is replaced"
    # Everything else must survive too, or the save costs the user their setup.
    assert body["ChannelMappings"] == [{"Name": "twitch.adapt"}]
    assert body["EnableAllTuners"] is True
    assert body["Path"] == provider["Path"]

    params = posted.calls[0].request.url.params
    assert params["validateListings"] == "false"
    assert params["validateLogin"] == "false"

    assert not deleted.called, "nothing is destroyed any more"


@respx.mock
async def test_the_refresh_never_touches_the_scheduled_tasks_api():
    """Saving the provider queues the refresh, so there is no task to trigger.

    `/ScheduledTasks/Running/{guid}` needed the task's GUID looked up by `Key`,
    which was a whole round-trip to work around there being no name-based route.
    """
    provider = {"Id": "p", "Type": "xmltv", "Path": GUIDE}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    respx.post(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(200, json={}))
    tasks = respx.get(f"{BASE}/ScheduledTasks").mock(return_value=httpx.Response(200, json=[]))

    async with JellyfinClient(BASE, "key") as client:
        assert await client.refresh_guide_now(GUIDE) is True

    assert not tasks.called
    assert not hasattr(JellyfinClient, "refresh_guide")


@respx.mock
async def test_a_stale_token_in_the_stored_path_still_matches():
    """The stored Path carries the tuner token, which may have been rotated."""
    provider = {"Id": "p", "Type": "xmltv", "Path": f"{GUIDE}?key=rotated-away"}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([provider]))
    )
    respx.post(f"{BASE}/LiveTv/ListingProviders").mock(return_value=httpx.Response(200, json={}))

    async with JellyfinClient(BASE, "key") as client:
        assert await client.refresh_guide_now(f"{GUIDE}?key=current-token") is True


@respx.mock
async def test_no_matching_provider_reports_failure_rather_than_guessing():
    """No provider of ours means no XMLTV guide is configured at all."""
    other = {"Id": "x", "Type": "xmltv", "Path": "http://elsewhere/guide.xml"}
    respx.get(f"{BASE}/System/Configuration/livetv").mock(
        return_value=httpx.Response(200, json=livetv_config([other]))
    )
    saving = respx.post(f"{BASE}/LiveTv/ListingProviders").mock(
        return_value=httpx.Response(200, json={})
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.refresh_guide_now(GUIDE) is False
    assert not saving.called, "must not touch a provider that is not ours"
