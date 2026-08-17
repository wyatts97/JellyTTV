"""Deterministic season/episode numbering for Twitch VODs.

Requirements:
  * Stable - a number, once assigned, must never change, or Jellyfin will show
    duplicate/renumbered episodes.
  * Ordered - sorting by episode number must equal sorting by broadcast date.
  * Collision-free for multiple streams on the same day.

Scheme (`year`):      Season = broadcast year, Episode = day_of_year * 10 + idx
Scheme (`calendar_month`): Season = YYYYMM,   Episode = day_of_month * 10 + idx

`idx` is the 0-based index of the VOD within that day, so up to 10 broadcasts a
day are representable before wrapping (we clamp and log if exceeded).
"""

from __future__ import annotations

from datetime import datetime

from app.logging_conf import get_logger
from app.models import SeasonScheme

log = get_logger(__name__)

MAX_PER_DAY = 10


def compute_season(published_at: datetime, scheme: SeasonScheme) -> int:
    if scheme is SeasonScheme.calendar_month:
        return published_at.year * 100 + published_at.month
    return published_at.year


def compute_episode(published_at: datetime, scheme: SeasonScheme, index_within_day: int) -> int:
    if index_within_day >= MAX_PER_DAY:
        log.warning(
            "more than %s broadcasts on one day, episode numbers may collide",
            MAX_PER_DAY,
            date=published_at.date().isoformat(),
        )
        index_within_day = MAX_PER_DAY - 1
    base = (
        published_at.day
        if scheme is SeasonScheme.calendar_month
        else published_at.timetuple().tm_yday
    )
    return base * MAX_PER_DAY + index_within_day


def assign_numbers(
    published_at: datetime,
    scheme: SeasonScheme,
    *,
    taken: set[tuple[int, int]],
) -> tuple[int, int]:
    """Return the first free (season, episode) pair for `published_at`."""
    season = compute_season(published_at, scheme)
    for index in range(MAX_PER_DAY):
        episode = compute_episode(published_at, scheme, index)
        if (season, episode) not in taken:
            return season, episode
    # Extremely unlikely: fall back to appending after the day's block.
    episode = compute_episode(published_at, scheme, MAX_PER_DAY - 1)
    while (season, episode) in taken:
        episode += 1
    return season, episode


def format_episode_tag(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:04d}"
