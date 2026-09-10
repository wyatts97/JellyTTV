"""The Jellyfin REST client, where Jellyfin's own API contract is version-bound.

JellyTTV targets Jellyfin 12.0. These pin the two places where 12.0 tightened
something that used to be forgiving, so a regression shows up here rather than as
a silent 404 or an empty search result against a live server.
"""

from __future__ import annotations

import httpx
import respx

from app.services.jellyfin import JellyfinClient

BASE = "http://jellyfin:8096"


@respx.mock
async def test_the_auth_header_uses_the_scheme_12_0_still_accepts():
    """12.0 disabled its legacy authorization mechanisms by default.

    `X-Emby-Authorization`, `X-Emby-Token`, `X-MediaBrowser-Token`, the `Emby`
    scheme and the `api_key` query parameter are all gated off now. The
    `MediaBrowser` scheme is the supported one and is not gated - so this header
    is the only thing standing between us and a 401 on every call.
    """
    route = respx.get(f"{BASE}/System/Info").mock(
        return_value=httpx.Response(200, json={"Version": "12.0.0"})
    )

    async with JellyfinClient(BASE, "the-key") as client:
        assert (await client.system_info())["Version"] == "12.0.0"

    auth = route.calls[0].request.headers["Authorization"]
    assert auth.startswith("MediaBrowser "), auth
    assert 'Token="the-key"' in auth
    assert "X-Emby-Authorization" not in route.calls[0].request.headers
    assert "api_key" not in route.calls[0].request.url.params


@respx.mock
async def test_find_series_asks_for_a_recursive_search():
    """12.0 made /Items honour `recursive` when `includeItemTypes` is set.

    Earlier versions forced it, so dropping the parameter used to cost nothing.
    On 12.0 it would silently narrow the search to the library's immediate
    children and never find the series, which sit under it.
    """
    route = respx.get(f"{BASE}/Items").mock(
        return_value=httpx.Response(200, json={"Items": [{"Id": "abc", "Name": "Adapt"}]})
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.find_series("Adapt", parent_id="lib-1") == "abc"

    params = route.calls[0].request.url.params
    assert params["recursive"] == "true"
    assert params["includeItemTypes"] == "Series"
    assert params["searchTerm"] == "Adapt"
    assert params["parentId"] == "lib-1"


@respx.mock
async def test_find_series_prefers_an_exact_name_over_the_first_hit():
    """`searchTerm` is a fuzzy match, so the first result need not be ours."""
    respx.get(f"{BASE}/Items").mock(
        return_value=httpx.Response(
            200,
            json={"Items": [{"Id": "near", "Name": "Adapt Highlights"},
                            {"Id": "exact", "Name": "adapt"}]},
        )
    )

    async with JellyfinClient(BASE, "key") as client:
        assert await client.find_series("Adapt") == "exact"
