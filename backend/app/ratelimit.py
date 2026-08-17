"""Shared rate limiter.

Lives in its own module so both `app.main` (which registers it on the app and
installs the 429 handler) and the routers that decorate endpoints can import it
without a circular import.

Only the publicly reachable endpoints are limited. `/eventsub/callback` has to be
open for Twitch to reach it, so a ceiling keeps a flood of forged requests from
burning CPU on HMAC verification and database lookups. The limit is far above
real Twitch traffic: notifications arrive in ones and twos, not hundreds.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Generous enough that legitimate bursts (e.g. reconciling many channels at once,
# each triggering a verification callback) never trip it.
EVENTSUB_LIMIT = "240/minute"
