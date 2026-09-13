"""`/stream/{login}.ts` - a live channel as one continuous MPEG-TS stream.

The tuner url Jellyfin plays when `live_delivery` is "ts". See
services.live_stream for why this replaces the rewritten-HLS proxy.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from app.db import get_session_factory
from app.logging_conf import get_logger
from app.routers.hls import _channel_quality
from app.security import check_tuner_token
from app.services import live_stream, resolver
from app.services.settings_store import ResolvedSettings, get_settings

log = get_logger(__name__)

router = APIRouter(tags=["stream"])

TS_MEDIA_TYPE = "video/mp2t"
NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def _player_type(settings: ResolvedSettings) -> str | None:
    if settings.row.ad_free_source:
        return resolver.AD_FREE_PLAYER_TYPE
    return settings.row.twitch_player_type


def _unavailable(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


@router.api_route("/stream/{login}.ts", methods=["GET", "HEAD"], include_in_schema=False)
async def live_ts(
    login: str,
    request: Request,
    key: str | None = Query(default=None, description="Tuner access token"),
) -> Response:
    # Everything the stream needs from the database is read here, in a session
    # that is closed before the first byte is sent. A request-scoped `get_db`
    # dependency can stay open for the whole life of a StreamingResponse, and a
    # live stream runs for hours: a handful of viewers would hold the SQLite
    # pool and stall every other request in the app.
    async with get_session_factory()() as session:
        await check_tuner_token(session, request, key)
        settings = await get_settings(session)
        quality = await _channel_quality(session, settings, login)

    if request.method == "HEAD":
        # Jellyfin probes with HEAD; answering must not start a streamlink.
        return Response(status_code=status.HTTP_200_OK, media_type=TS_MEDIA_TYPE, headers=NO_STORE)

    cmd = resolver.streamlink_stdout_cmd(
        f"https://www.twitch.tv/{login}",
        quality,
        settings.twitch_user_token,
        _player_type(settings),
        settings.twitch_device_id,
    )
    try:
        handle = await live_stream.open_stream(login, cmd)
    except resolver.ChannelOffline as exc:
        raise _unavailable(f"{login} is offline", retry_after=60) from exc
    except live_stream.StreamCapacityError as exc:
        log.warning("live stream refused: no free slot", login=login, error=str(exc))
        raise _unavailable(str(exc), retry_after=30) from exc
    except live_stream.StreamStartError as exc:
        log.warning("live stream failed to start", login=login, error=str(exc)[:300])
        raise _unavailable(f"could not start {login}: {exc}", retry_after=5) from exc

    return StreamingResponse(handle.iter_bytes(), media_type=TS_MEDIA_TYPE, headers=NO_STORE)
