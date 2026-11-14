from .scraper import Scraper

import json
import re
import time
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from loguru import logger

from .utitls import T_MSG_FORMAT

# --------------------------------------------------------------------------- #
# NHentai (https://nhentai.net)
#
# A gallery is a one-shot, so each gallery is exposed to the bot as a manga with exactly one
# chapter ("Chapter 1") holding every page - the same shape as Webs/hentaifox.py.
#
# nhentai.net was rewritten as a SvelteKit app backed by a JSON REST API under /api/v2/ (the old
# /api/gallery/<id> and /api/galleries/search endpoints are gone, and the pages no longer embed the data).
# Endpoints are therefore tried in this order, newest first, and the one that works is remembered:
#     gallery   /api/v2/galleries/<id>            then the old /api/gallery/<id>
#     search    /api/v2/search?query=<q>          then /api/v2/galleries/search, then the old /api/galleries/search
# The response parser is deliberately tolerant (field names/shapes are accepted in several spellings).
# Auth: an API key from the nhentai account settings (/settings -> Source Access -> NHentai API key) is sent as
# `Authorization: Key <key>` (or `User <key>` if that is refused) and lets the bot skip Cloudflare's captcha.
# Page pictures:  https://i.nhentai.net/galleries/<media_id>/<n>.<ext>   (or the exact path the API gives)
# The site is behind Cloudflare: see Tools/siteauth.py + Webs/scraper.py (browser fingerprints, cookies, FlareSolverr).
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose nhentai` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://nhentai.net/"
IMG_HOST = "https://i.nhentai.net/"
THUMB_HOST = "https://t.nhentai.net/"
EXT = {"j": "jpg", "p": "png", "g": "gif", "w": "webp", "a": "avif", "b": "bmp"}
KINDS = ("tag", "artist", "group", "parody", "character", "language", "category")
CHAPTER_TITLE = "Chapter 1"
CACHE_TTL = 600


# --------------------------------------------------------------------------- #
# helpers (module level so they can be tested without the network)
# --------------------------------------------------------------------------- #
def gallery_id(text):
  """'177013', '/g/177013/', 'https://nhentai.net/g/177013/12/' -> '177013'."""
  text = str(text or "").strip()
  if text.isdigit():
    return text
  m = re.search(r"/g/(\d+)", text)
  return m.group(1) if m else None


def gallery_url(gid) -> str:
  return urljoin(BASE, f"g/{gid}/")


def _ext(code, default="jpg"):
  code = str(code or "").strip().lower()
  if code in EXT:
    return EXT[code]
  return code if code in EXT.values() else default


def _title(t) -> str:
  if isinstance(t, str):
    return t.strip()
  t = t or {}
  return (t.get("pretty") or t.get("english") or t.get("japanese") or "").strip()


def _unwrap(j):
  """{"gallery": {...}} / {"data": {...}} / {"result": {...}} -> the gallery object itself."""
  if isinstance(j, dict) and not j.get("id"):
    for k in ("gallery", "data", "result", "doujin", "item"):
      if isinstance(j.get(k), dict):
        return _unwrap(j[k])
  return j


def _abs_img(v: str, media_id=None) -> str:
  v = (v or "").strip()
  if not v:
    return ""
  if v.startswith("//"):
    return "https:" + v
  if v.startswith(("http://", "https://")):
    return v
  if v.startswith("/"):
    return IMG_HOST.rstrip("/") + v
  return IMG_HOST + v.lstrip("/")


def _page_items(j: dict) -> list:
  imgs = j.get("images")
  if isinstance(imgs, dict) and isinstance(imgs.get("pages"), list):
    return imgs["pages"]
  for k in ("pages", "images", "page_list", "pictures"):
    if isinstance(j.get(k), list):
      return j[k]
  return []


def _page_url(item, media_id, n) -> str:
  if isinstance(item, str):
    return _abs_img(item)
  if isinstance(item, dict):
    for k in ("url", "src", "image", "full", "original", "path", "file"):
      v = item.get(k)
      if isinstance(v, str) and v:
        return _abs_img(v)
    code = item.get("t") or item.get("type") or item.get("ext") or item.get("format")
    if media_id:
      return f"{IMG_HOST}galleries/{media_id}/{n}.{_ext(code)}"
  return f"{IMG_HOST}galleries/{media_id}/{n}.jpg" if media_id else ""


def _thumb_url(v, media_id=None) -> str:
  """A thumbnail/cover given as a string path/url or {"path"/"url"/"t": ...}."""
  if isinstance(v, str):
    if v.startswith(("http", "//")):
      return _abs_img(v)
    return (THUMB_HOST + v.lstrip("/")) if v else ""
  if isinstance(v, dict):
    for k in ("url", "src", "path"):
      if isinstance(v.get(k), str) and v[k]:
        return _thumb_url(v[k])
    if media_id and v.get("t"):
      return f"{THUMB_HOST}galleries/{media_id}/cover.{_ext(v['t'])}"
  return ""


def from_api(j):
  """The site's gallery JSON (old or v2 shape) -> the normalised gallery used everywhere below."""
  j = _unwrap(j)
  if not isinstance(j, dict) or not j.get("id"):
    return None
  media_id = str(j.get("media_id") or j.get("mediaId") or j.get("media") or "")
  items = _page_items(j)
  urls = [u for u in (_page_url(it, media_id, n) for n, it in enumerate(items, 1)) if u]
  if not urls:
    return None
  pages = [(urls[i].rsplit(".", 1)[-1].lower() if "." in urls[i].rsplit("/", 1)[-1] else "jpg") for i in range(len(urls))]
  tags = {k: [] for k in KINDS}
  for t in j.get("tags") or []:
    if not isinstance(t, dict):
      continue
    kind, name = (t.get("type") or t.get("category") or "").lower(), (t.get("name") or "").strip()
    if kind in tags and name and name not in tags[kind]:
      tags[kind].append(name)
  imgs = j.get("images") if isinstance(j.get("images"), dict) else {}
  cover = _thumb_url(imgs.get("cover") or j.get("cover") or j.get("thumbnail") or imgs.get("thumbnail"), media_id)
  return {
    "id": str(j["id"]), "media_id": media_id, "title": _title(j.get("title") or j.get("name")),
    "pages": pages, "urls": urls, "num_pages": int(j.get("num_pages") or j.get("pages_count") or len(urls)),
    "tags": tags, "cover": cover,
    "cover_ext": _ext((imgs.get("cover") or {}).get("t") if isinstance(imgs.get("cover"), dict) else None, pages[0]),
  }


def _js_json(html: str):
  """The gallery JSON older/newer page layouts embed:  window._gallery = JSON.parse("…")."""
  m = re.search(r"_gallery\s*=\s*JSON\.parse\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')\s*\)", html)
  if m:
    raw = m.group(1)
    try:
      lit = raw if raw[0] == '"' else '"' + raw[1:-1].replace('"', '\\"') + '"'
      return json.loads(json.loads(lit))
    except ValueError:
      pass
  m = re.search(r"_gallery\s*=\s*(\{.*?\})\s*;", html, re.S)
  if m:
    try:
      return json.loads(m.group(1))
    except ValueError:
      pass
  return None


def from_html(html: str, gid: str):
  """A gallery page -> the normalised gallery (embedded JSON first, else its thumbnails)."""
  j = _js_json(html)
  g = from_api(j) if j else None
  if g:
    return g
  bs = BeautifulSoup(html, "html.parser")
  thumbs = {}
  media_id = None
  for m in re.finditer(r"(?:https?:)?//t\d*\.nhentai\.net/galleries/(\d+)/(\d+)t\.(\w+)", html):
    media_id = media_id or m.group(1)
    if m.group(1) == media_id:
      thumbs.setdefault(int(m.group(2)), _ext(m.group(3)))
  if not media_id or not thumbs:
    return None
  title = ""
  node = bs.select_one("#info h1") or bs.select_one("h1.title") or bs.find("h1")
  if node:
    title = node.get_text(" ", strip=True)
  tags = {k: [] for k in KINDS}
  for a in bs.find_all("a", href=True):
    m = re.match(r"^/(tag|artist|group|parody|character|language|category)/([^/]+)/?$", a["href"])
    if not m:
      continue
    name = a.find(class_="name")
    name = (name.get_text(" ", strip=True) if name else re.sub(r"\s*\d[\d,\.Kk]*$", "", a.get_text(" ", strip=True))).strip()
    if name and name not in tags[m.group(1)]:
      tags[m.group(1)].append(name)
  n = max(thumbs)
  pages = [thumbs.get(i, "jpg") for i in range(1, n + 1)]
  cover = re.search(r"galleries/%s/cover\.(\w+)" % media_id, html)
  return {"id": gid, "media_id": media_id, "title": title, "pages": pages, "num_pages": n, "tags": tags,
          "urls": [f"{IMG_HOST}galleries/{media_id}/{i}.{e}" for i, e in enumerate(pages, 1)], "cover": "",
          "cover_ext": _ext(cover.group(1)) if cover else pages[0]}


def page_urls(g: dict) -> list:
  if g.get("urls"):
    return list(g["urls"])
  return [f"{IMG_HOST}galleries/{g['media_id']}/{i}.{ext}" for i, ext in enumerate(g["pages"], 1)]


def cover_url(g: dict) -> str:
  return g.get("cover") or f"{THUMB_HOST}galleries/{g['media_id']}/cover.{g['cover_ext']}"


def _result_list(data):
  """A search response -> its list of galleries, whatever the envelope is called."""
  if isinstance(data, list):
    return data
  if isinstance(data, dict):
    for k in ("result", "results", "galleries", "data", "items"):
      if isinstance(data.get(k), list):
        return data[k]
      if isinstance(data.get(k), dict):
        inner = _result_list(data[k])
        if inner:
          return inner
  return []


def card_from_api(j):
  """One search-result item -> {title, poster, url}. Items may be the full object or a reduced one."""
  j = _unwrap(j)
  if not isinstance(j, dict) or not j.get("id"):
    return None
  media_id = str(j.get("media_id") or j.get("mediaId") or "")
  imgs = j.get("images") if isinstance(j.get("images"), dict) else {}
  poster = _thumb_url(j.get("thumbnail") or j.get("cover") or imgs.get("thumbnail") or imgs.get("cover"), media_id)
  if not poster and media_id:
    poster = f"{THUMB_HOST}galleries/{media_id}/thumb.jpg"
  return {"title": _title(j.get("title") or j.get("name")) or f"Gallery {j['id']}", "poster": poster, "url": gallery_url(j["id"])}


def parse_listing(bs):
  """Cards of an HTML search / home page -> [{title, poster, url}]."""
  out, seen = [], set()
  for a in bs.find_all("a", href=True):
    m = re.match(r"^/g/(\d+)/?$", a["href"])
    if not m or m.group(1) in seen:
      continue
    seen.add(m.group(1))
    img = a.find("img")
    src = ""
    if img is not None:
      src = (img.get("data-src") or img.get("src") or "").strip()
      if src.startswith("data:"):
        src = (img.get("data-src") or "")
      src = ("https:" + src) if src.startswith("//") else src
    cap = a.find(class_="caption")
    title = (cap.get_text(" ", strip=True) if cap else "") or (img.get("alt", "").strip() if img is not None else "")
    out.append({"title": title or f"Gallery {m.group(1)}", "poster": src, "url": gallery_url(m.group(1))})
  return out


# --------------------------------------------------------------------------- #
class NHentaiWebs(Scraper):
  def __init__(self):
    super().__init__()
    self.url = BASE
    self.bg = None
    self.sf = "nh"
    self.cs = False               # images: plain headers first; the downloader falls back to Chrome fingerprints on 403
    self.impersonate = "chrome"   # Cloudflare: real-Chrome fingerprint for the site and its API
    self.headers = {"Referer": self.url, "Accept-Language": "en-US,en;q=0.9"}
    self.api_headers = {**self.headers, "Accept": "application/json, text/plain, */*"}
    self._cache = {}              # gid -> (time, gallery)
    self._ep = {}                 # remembered working endpoints

  # ----------------------------------------------------------------------- #
  def _auth_schemes(self):
    try:
      from bot import Vars
      key = (getattr(Vars, "NHENTAI_API_KEY", "") or "").strip()
    except Exception:
      key = ""
    if not key:
      return [None]
    if key.split(" ", 1)[0] in ("Key", "User", "Bearer"):
      return [key]
    return [f"Key {key}", f"User {key}"]

  async def _api(self, *paths):
    """GET the first of `paths` that answers with JSON; the one that worked is tried first next time."""
    group = paths[0].split("?")[0].rsplit("/", 1)[0]
    good = self._ep.get(group)
    if good in paths:
      paths = (good,) + tuple(p for p in paths if p != good)
    for path in paths:
      for auth in self._auth_schemes():
        headers = dict(self.api_headers)
        if auth:
          headers["Authorization"] = auth
        try:
          data = await self.get(urljoin(self.url, path), cs=True, rjson=True, headers=headers)
        except Exception as e:
          logger.warning(f"NHentai: {path} failed: {type(e).__name__}: {e}")
          continue
        if isinstance(data, (dict, list)):
          self._ep[group] = path
          return data
    return None

  async def _html(self, path):
    for attempt in range(2):
      try:
        html = await self.get(urljoin(self.url, path), cs=True, headers=self.headers)
        if html:
          return html
      except Exception as e:
        logger.warning(f"NHentai: {path} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  async def _gallery(self, gid):
    hit = self._cache.get(gid)
    if hit and time.time() - hit[0] < CACHE_TTL:
      return hit[1]
    raw = await self._api(f"api/v2/galleries/{gid}", f"api/gallery/{gid}")
    g = from_api(raw)
    if raw and not g:
      keys = sorted(raw)[:20] if isinstance(raw, dict) else type(raw).__name__
      logger.warning(f"NHentai: gallery {gid} answered but its JSON was not recognised (keys: {keys}) - paste this in a bug report")
    if not g:
      html = await self._html(f"g/{gid}/")
      g = from_html(html, gid) if html else None
    if g:
      if len(self._cache) > 200:
        self._cache.clear()
      self._cache[gid] = (time.time(), g)
    else:
      logger.error(f"NHentai: could not load gallery {gid} (blocked, removed, or the layout changed)")
    return g

  # ----------------------------------------------------------------------- #
  async def search(self, query: str = ""):
    query = (query or "").strip()

    gid = gallery_id(query) if (query.isdigit() or "nhentai" in query.lower()) else None
    if gid:                                   # a pasted link / bare number opens that gallery directly
      g = await self._gallery(gid)
      return [{"title": g["title"] or f"Gallery {gid}", "poster": cover_url(g), "url": gallery_url(gid)}] if g else []

    q = quote_plus(query)
    data = await (self._api(f"api/v2/search?query={q}&page=1", f"api/v2/galleries/search?query={q}&page=1",
                            f"api/galleries/search?query={q}&page=1") if query
                  else self._api("api/v2/galleries?page=1", "api/galleries/all?page=1"))
    results = [c for c in (card_from_api(j) for j in _result_list(data)) if c]
    if data and not results:
      logger.warning(f"NHentai: search answered but no galleries were recognised (keys: {sorted(data)[:20] if isinstance(data, dict) else type(data).__name__})")
    if results:
      return results

    html = await self._html(f"search/?q={quote_plus(query)}" if query else "")
    results = parse_listing(BeautifulSoup(html, "html.parser")) if html else []
    if not results:
      logger.warning(f"NHentai: no results for {query!r} (blocked, or the layout changed)")
    return results

  # ----------------------------------------------------------------------- #
  async def get_chapters(self, data, page: int = 1):
    results = data
    gid = gallery_id(results.get("url"))
    g = await self._gallery(gid) if gid else None
    if not g:
      results["chapters"] = []
      return results

    title = g["title"] or results.get("title") or f"Gallery {gid}"
    results["title"] = title
    results["url"] = gallery_url(gid)
    # page 1 is the full-size cover (the "cover" file is a ~350px thumbnail); Tools.poster falls back to the cover if page 1 is refused
    results["poster"] = page_urls(g)[0] if g["pages"] else cover_url(g)
    t = g["tags"]
    bits = []
    for label, kind in (("Artists", "artist"), ("Groups", "group"), ("Parodies", "parody"), ("Characters", "character")):
      if t[kind]:
        bits.append(f"{label}: {', '.join(t[kind][:6])}")
    if g["num_pages"]:
      bits.append(f"Pages: {g['num_pages']}")
    langs = [x for x in t["language"] if x.lower() != "translated"] or t["language"]
    results["msg"] = T_MSG_FORMAT.format(
      title=title, status="Completed", genres=", ".join(t["tag"][:10]) or "N/A",
      language=", ".join(langs) or "N/A", summary=(" | ".join(bits) or "N/A")[:400], url=results["url"])
    results["chapters"] = [{"title": CHAPTER_TITLE, "url": results["url"], "manga_title": title[:120],
                            "poster": results["poster"]}]
    return results

  def iter_chapters(self, data, page: int = 1):
    chapters = data.get("chapters") or []
    return chapters[(page - 1) * 60:page * 60] if page != 1 else chapters

  # ----------------------------------------------------------------------- #
  async def get_pictures(self, url, data=None):
    gid = gallery_id(url)
    g = await self._gallery(gid) if gid else None
    if not g or not g["pages"]:
      logger.error(f"NHentai: no pages for {url}")
      return []
    return page_urls(g)
