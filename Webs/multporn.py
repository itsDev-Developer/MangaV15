import re
from urllib.parse import quote_plus, unquote, urlparse

from loguru import logger

from .gallerykit import (GalleryScraper, JUNK_RE, abs_url, cards, dedupe, first_src, labelled, largest_folder,
                         title_from_slug)
from .utitls import DEAULT_MSG_FORMAT, extract_og_image

# --------------------------------------------------------------------------- #
# Multporn (https://multporn.net)
#
# A Drupal site: a comic / manga is a page like /comics/<slug> or /hentai_manga/<slug>; its pages are
# images in /sites/default/files/. The page shows resized copies (/styles/<name>/public/...); the original is
# the same path without the style part, which is what the bot downloads. Pages shown by the Juicebox gallery
# (images loaded from an XML file) are read from that XML. One comic = one manga with one chapter.
#
# The section name is not assumed: any two-segment link inside a listing card counts, minus Drupal's own pages.
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose multporn` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://multporn.net/"
NOT_SECTION = {"node", "taxonomy", "user", "users", "search", "tags", "tag", "category", "categories", "sites", "admin",
               "contact", "about", "dmca", "privacy", "terms", "rss", "feed", "forum", "page", "login", "register",
               "comment", "comments", "blog", "faq", "help", "stats", "top", "new", "latest", "popular", "random"}
STYLE_RE = re.compile(r"/styles/[^/]+/public/")


def link_to_url(href):
  p = urlparse(href)
  if p.netloc and p.netloc.lower().removeprefix("www.") != "multporn.net":
    return None
  seg = [x for x in p.path.split("/") if x]
  if len(seg) != 2 or seg[0].lower() in NOT_SECTION or "." in seg[1]:
    return None
  return f"{BASE}{seg[0]}/{seg[1]}"


def original(url: str) -> str:
  """…/sites/default/files/styles/<style>/public/<path>?itok=x  ->  …/sites/default/files/<path>"""
  url = url.split("?")[0]
  return STYLE_RE.sub("/", url)


def page_images(bs, html: str):
  cand = []
  for a in bs.find_all("a", href=True):                      # lightbox links usually point at the full file
    if re.search(r"/sites/default/files/.+\.(?:jpe?g|png|webp|gif)", a["href"], re.I):
      cand.append(abs_url(a["href"], BASE))
  if not cand:
    for img in bs.find_all("img"):
      src = first_src(img)
      if "/sites/default/files/" in src:
        cand.append(abs_url(src, BASE))
  cand = [original(u) for u in cand if not JUNK_RE.search(u)]
  return largest_folder(dedupe(cand))


def juicebox_xml_urls(html: str):
  return dedupe(abs_url(m.replace("\\/", "/"), BASE)
                for m in re.findall(r"configUrl[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", html))


class MultpornWebs(GalleryScraper):
  label = "Multporn"

  def __init__(self):
    super().__init__(BASE, "mpn")

  async def search(self, query: str = ""):
    query = (query or "").strip()
    urls = ([f"{BASE}search?search_api_views_fulltext={quote_plus(query)}", f"{BASE}search/node/{quote_plus(query)}",
             f"{BASE}?s={quote_plus(query)}"] if query else [BASE])
    for url in urls:
      html = await self._page(url)
      results = cards(self.soup(html), BASE, link_to_url) if html else []
      if results:
        return results
    logger.warning(f"Multporn: no results for {query!r} (blocked, or the layout changed)")
    return []

  async def get_chapters(self, data, page: int = 1):
    results = data
    url = link_to_url(results["url"]) or results["url"]
    html = await self._page(url)
    if not html:
      logger.error(f"Multporn: could not load {url}")
      results["chapters"] = []
      return results
    bs = self.soup(html)
    h1 = bs.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or results.get("title") or title_from_slug(url.rsplit("/", 1)[-1])
    images = page_images(bs, html)
    results.update(title=title, url=url)
    results["poster"] = images[0] if images else (extract_og_image(bs) or results.get("poster"))
    tags = [t.strip() for t in labelled(bs, "Tags", "Characters", "Category")]
    extra = [f"{lab}: {', '.join(v[:5])}" for lab, v in
             (("Artists", labelled(bs, "Authors", "Author", "Artists", "Artist")),
              ("Section", [url.split('/')[-2].replace('_', ' ').title()])) if v]
    if images:
      extra.append(f"Pages: {len(images)}")
    results["msg"] = DEAULT_MSG_FORMAT.format(
      title=title, status="Completed", genres=", ".join(tags[:10]) or "N/A",
      summary=(" | ".join(extra) or "N/A")[:300], url=url)
    results["chapters"] = [{"title": "Chapter 1", "url": url, "manga_title": title[:120], "poster": results["poster"]}]
    return results

  async def get_pictures(self, url, data=None):
    html = await self._page(url)
    if not html:
      logger.error(f"Multporn: could not load {url}")
      return []
    images = page_images(self.soup(html), html)
    if not images:                                  # pages loaded by the Juicebox gallery: read its XML
      for cfg in juicebox_xml_urls(html):
        xml = await self._page(cfg)
        found = re.findall(r"imageURL=\"([^\"]+)\"", xml or "")
        images = dedupe(original(abs_url(unquote(u).replace("&amp;", "&"), BASE)) for u in found)
        if images:
          break
    if not images:
      logger.error(f"Multporn: no pictures found for {url}")
    return self.encode(images)
