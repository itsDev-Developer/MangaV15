import html as _html
import json
import re
from urllib.parse import quote_plus, unquote, urlparse

from loguru import logger

from .gallerykit import GalleryScraper, abs_url, cards, dedupe, first_src, title_from_slug
from .utitls import DEAULT_MSG_FORMAT, extract_og_image

# --------------------------------------------------------------------------- #
# 8muses (https://8muses.io)
#
# Comics are nested *albums*: /comics/album/<Artist>/<Series>/<Issue>. An album page lists its sub-albums and/or
# its pictures. The page data is embedded as JSON (a hidden "ractive-public" input holding `albums` and `pictures`;
# a picture's file is /image/fl/<publicUri>.jpg) - and, if that is missing, read from the tiles (thumbnails
# /image/th/<uri>.jpg are the same file as /image/fl/<uri>.jpg).
#
#   an album with sub-albums  -> a series: each sub-album is a chapter
#   an album with pictures    -> one chapter ("Chapter 1")
#   pictures()                -> follows sub-albums up to two levels, so picking an artist/series album still works
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose 8muses` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://8muses.io/"
HOSTS = {"8muses.io", "www.8muses.io", "comics.8muses.io"}
MAX_SUBALBUMS = 12


def link_to_album(href):
  p = urlparse(href)
  host = p.netloc.lower()
  if host and host not in HOSTS:
    return None
  if not p.path.startswith("/comics/album/"):
    return None
  return f"{p.scheme or 'https'}://{host or '8muses.io'}{p.path}".rstrip("/")


def thumb_to_full(src: str) -> str:
  return re.sub(r"/image/(?:th|fm|sm)/", "/image/fl/", src)


def embedded_json(html: str):
  m = re.search(r"id=[\"']ractive-public[\"'][^>]*\bvalue=(?:\"([^\"]*)\"|'([^']*)')", html, re.S) or \
      re.search(r"\bvalue=(?:\"([^\"]*)\"|'([^']*)')[^>]*id=[\"']ractive-public[\"']", html, re.S)
  if not m:
    return None
  try:
    return json.loads(_html.unescape(m.group(1) or m.group(2)))
  except ValueError:
    return None


def read_album(html: str, bs, base: str = BASE):
  """-> (pictures [url], albums [{title,url,poster}])"""
  pictures, albums = [], []
  data = embedded_json(html)
  if isinstance(data, dict):
    for p in data.get("pictures") or []:
      uri = (p or {}).get("publicUri") or (p or {}).get("uri")
      if uri:
        pictures.append(abs_url(f"/image/fl/{uri}.jpg", base))
    for a in data.get("albums") or []:
      path = (a or {}).get("permalink") or (a or {}).get("path") or ""
      if path:
        path = path if path.startswith("/") else "/comics/album/" + path.lstrip("/")
        albums.append({"title": (a.get("name") or a.get("title") or "").strip(), "url": abs_url(path, base).rstrip("/"),
                       "poster": ""})
  if not pictures and not albums:                         # DOM tiles
    for a in bs.find_all("a", href=True):
      img = a.find("img")
      src = first_src(img)
      if "/comics/picture/" in a["href"] and src:
        pictures.append(abs_url(thumb_to_full(src), base))
      elif link_to_album(a["href"]):
        albums.append({"title": (a.get("title") or (img.get("alt") if img is not None else "") or
                                 a.get_text(" ", strip=True)).strip(),
                       "url": link_to_album(a["href"]), "poster": abs_url(thumb_to_full(src), base) if src else ""})
  seen = set()
  albums = [x for x in albums if not (x["url"] in seen or seen.add(x["url"]))]
  return dedupe(pictures), albums


class EightMusesWebs(GalleryScraper):
  label = "8muses"

  def __init__(self):
    super().__init__(BASE, "8ms")
    self.aliases = ("https://www.8muses.io/", "https://comics.8muses.io/")

  async def search(self, query: str = ""):
    query = (query or "").strip()
    urls = ([f"{BASE}search?q={quote_plus(query)}", f"{BASE}comics/search?q={quote_plus(query)}",
             f"{BASE}search/{quote_plus(query)}"] if query else [f"{BASE}comics"])
    for url in urls:
      html = await self._page(url)
      results = cards(self.soup(html), BASE, link_to_album) if html else []
      if results:
        return results
    logger.warning(f"8muses: no results for {query!r} (blocked, or the layout changed)")
    return []

  async def get_chapters(self, data, page: int = 1):
    results = data
    url = link_to_album(results["url"]) or results["url"].rstrip("/")
    html = await self._page(url)
    if not html:
      logger.error(f"8muses: could not load {url}")
      results["chapters"] = []
      return results
    bs = self.soup(html)
    pictures, albums = read_album(html, bs, self.url)
    h1 = bs.find("h1")
    title = re.sub(r"\s*[-|–]\s*8muses.*$", "", (h1.get_text(" ", strip=True) if h1 else ""), flags=re.I) \
        or results.get("title") or title_from_slug(unquote(url.rsplit("/", 1)[-1]))
    results.update(title=title, url=url)
    results["poster"] = (pictures[0] if pictures else next((a["poster"] for a in albums if a["poster"]), "")) \
        or extract_og_image(bs) or results.get("poster")
    summary = f"{len(albums)} chapters" if albums else (f"Pages: {len(pictures)}" if pictures else "N/A")
    results["msg"] = DEAULT_MSG_FORMAT.format(title=title, status="N/A", genres="N/A", summary=summary, url=url)
    if albums:
      chapters = [{"title": a["title"] or title_from_slug(unquote(a["url"].rsplit("/", 1)[-1])), "url": a["url"],
                   "manga_title": title[:120], "poster": results["poster"]} for a in albums]
    elif pictures:
      chapters = [{"title": "Chapter 1", "url": url, "manga_title": title[:120], "poster": results["poster"]}]
    else:
      chapters = []
      logger.error(f"8muses: nothing found on {url} (blocked, or the layout changed)")
    results["chapters"] = chapters
    return results

  async def _collect(self, url, depth):
    html = await self._page(url)
    if not html:
      return []
    pictures, albums = read_album(html, self.soup(html), self.url)
    if pictures or depth <= 0:
      return pictures
    out = []
    for sub in albums[:MAX_SUBALBUMS]:                    # an artist / series album: gather its issues in order
      out += await self._collect(sub["url"], depth - 1)
    return out

  async def get_pictures(self, url, data=None):
    images = dedupe(await self._collect(url, 2))
    if not images:
      logger.error(f"8muses: no pictures found for {url}")
    return self.encode(images)
