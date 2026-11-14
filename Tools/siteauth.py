"""Cookies / User-Agent for sites that sit behind Cloudflare's challenge.

Two sources, used for every request to the site (pages, APIs and image downloads):

  1. configured by the admin - /settings -> 🛰 Source Access -> Site cookies / Browser User-Agent.
     One site per line:   nhentai.net: cf_clearance=...; csrftoken=...
     Cloudflare ties `cf_clearance` to the IP address AND the User-Agent it was issued to, so this only works
     when the cookie was obtained from the same IP the bot runs on (e.g. a browser on the same server/VPN),
     together with that browser's exact User-Agent.
  2. learned automatically: when FlareSolverr solves a challenge for the bot, the cookies and User-Agent it
     got are remembered here for ~25 minutes, so the next requests (including the many image downloads of
     a chapter) go straight through instead of solving again.
"""
import time
from urllib.parse import urlparse

_solved = {}           # host -> (cookie header, user agent, expires)
SOLVED_TTL = 1500      # seconds


def _host(url: str) -> str:
  return urlparse(url).netloc.split(":")[0].lower().removeprefix("www.")


def _configured() -> dict:
  try:
    from bot import Vars
    raw = getattr(Vars, "SITE_COOKIES", "") or ""
  except Exception:
    return {}
  out = {}
  for line in raw.replace("\r", "").split("\n"):
    for sep in (": ", "="):
      host, found, cookie = line.partition(sep)
      if found and "." in host and cookie.strip():
        out[host.strip().lower().removeprefix("www.")] = cookie.strip()
        break
  return out


def _matches(host: str, key: str) -> bool:
  return host == key or host.endswith("." + key)


def lookup(url: str):
  """(cookie header, user agent or None) for this URL's site, or None."""
  host = _host(url)
  if not host:
    return None
  try:
    from bot import Vars
    ua = (getattr(Vars, "SITE_USER_AGENT", "") or "").strip() or None
  except Exception:
    ua = None
  for key, cookie in _configured().items():
    if _matches(host, key):
      return cookie, ua
  now = time.time()
  for key, (cookie, solved_ua, exp) in list(_solved.items()):
    if exp < now:
      _solved.pop(key, None)
    elif _matches(host, key):
      return cookie, solved_ua
  return None


def remember(url: str, cookies, user_agent: str = None):
  """Keep the cookies a solver obtained. cookies: FlareSolverr's list of {name, value, ...}."""
  host = _host(url)
  header = "; ".join(f"{c['name']}={c['value']}" for c in (cookies or []) if c.get("name"))
  if host and header:
    _solved[host] = (header, user_agent, time.time() + SOLVED_TTL)


def forget(url: str):
  """A solved session stopped working (blocked again): drop it so the next request solves afresh."""
  _solved.pop(_host(url), None)


def apply(headers: dict, url: str) -> dict:
  """headers + this site's Cookie / User-Agent (when known)."""
  auth = lookup(url)
  if not auth:
    return headers
  out = dict(headers or {})
  out["Cookie"] = auth[0]
  if auth[1]:
    out["User-Agent"] = auth[1]
  return out
