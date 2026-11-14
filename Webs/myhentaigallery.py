import re
from urllib.parse import quote_plus

from loguru import logger

from .gallerykit import GalleryScraper, abs_url, cards, dedupe, first_src, labelled
from .utitls import DEAULT_MSG_FORMAT, extract_og_image

# --------------------------------------------------------------------------- #
# MyHentaiGallery (https://myhentaigallery.com)
#
# A gallery is /g/<id>; its thumbnails are listed in "comic-thumb" blocks and every full-size picture is the
# same file with /thumbnail/ replaced by /original/ in the thumbnail's address. One gallery = one manga with
# one chapter. Search tries the site's search page (?query= then ?q=); a pasted gallery link or number opens
# that gallery directly.
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose myhentaigallery` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://myhentaigallery.com/"
GID_RE = re.compile(r"/g(?:allery(?:/(?:thumbnails|slideshow))?)?/(\d+)")


def gallery_id(text):
  text = str(text or "").strip()
  if text.isdigit():
    return text
  m = GID_RE.search(text)
  return m.group(1) if m else None


def gallery_url(gid) -> str:
  return f"{BASE}g/{gid}"


def link_to_url(href):
  if href.startswith(("http", "//")) and "myhentaigallery.com" not in href:
    return None
  m = re.search(r"/g/(\d+)/?(?:[?#].*)?$", href)
  return gallery_url(m.group(1)) if m else None


def full_size(src: str) -> str:
  return src.replace("/thumbnail/", "/original/")


def gallery_images(bs):
  thumbs = []
  for block in bs.find_all(class_=re.compile(r"comic-thumb", re.I)):
    img = block if block.name == "img" else block.find("img")
    src = first_src(img)
    if src:
      thumbs.append(src)
  if not thumbs:                                   # layout changed: any thumbnail-looking picture
    thumbs = [first_src(i) for i in bs.find_all("img") if "/thumbnail/" in first_src(i)]
  return dedupe([abs_url(full_size(t), BASE) for t in thumbs])


class MyHentaiGalleryWebs(GalleryScraper):
  label = "MyHentaiGallery"

  def __init__(self):
    super().__init__(BASE, "mhg")

  async def search(self, query: str = ""):
    query = (query or "").strip()
    gid = gallery_id(query) if (query.isdigit() or "myhentaigallery" in query.lower()) else None
    if gid:
      html = await self._page(gallery_url(gid))
      if html:
        bs = self.soup(html)
        h1 = bs.find("h1")
        return [{"title": h1.get_text(" ", strip=True) if h1 else f"Gallery {gid}",
                 "poster": (gallery_images(bs) or [extract_og_image(bs) or ""])[0], "url": gallery_url(gid)}]
    urls = ([f"{BASE}search?query={quote_plus(query)}", f"{BASE}search?q={quote_plus(query)}",
             f"{BASE}?s={quote_plus(query)}"] if query else [BASE])
    for url in urls:
      html = await self._page(url)
      results = cards(self.soup(html), BASE, link_to_url) if html else []
      if results:
        return results
    logger.warning(f"MyHentaiGallery: no results for {query!r} (blocked, or the layout changed)")
    return []

  async def get_chapters(self, data, page: int = 1):
    results = data
    gid = gallery_id(results.get("url"))
    html = await self._page(gallery_url(gid)) if gid else None
    if not html:
      logger.error(f"MyHentaiGallery: could not load {results.get('url')}")
      results["chapters"] = []
      return results
    bs = self.soup(html)
    h1 = bs.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or results.get("title") or f"Gallery {gid}"
    images = gallery_images(bs)
    results.update(title=title, url=gallery_url(gid))
    results["poster"] = images[0] if images else (extract_og_image(bs) or results.get("poster"))
    tags = labelled(bs, "Categories", "Tags")
    extra = [f"{lab}: {', '.join(v[:5])}" for lab, v in
             (("Artists", labelled(bs, "Artists", "Artist")), ("Groups", labelled(bs, "Groups", "Group")),
              ("Parodies", labelled(bs, "Parodies", "Parody"))) if v]
    if images:
      extra.append(f"Pages: {len(images)}")
    results["msg"] = DEAULT_MSG_FORMAT.format(
      title=title, status="Completed", genres=", ".join(tags[:10]) or "N/A",
      summary=(" | ".join(extra) or "N/A")[:300], url=results["url"])
    results["chapters"] = [{"title": "Chapter 1", "url": results["url"], "manga_title": title[:120],
                            "poster": results["poster"]}]
    return results

  async def get_pictures(self, url, data=None):
    gid = gallery_id(url)
    html = await self._page(gallery_url(gid)) if gid else None
    images = gallery_images(self.soup(html)) if html else []
    if not images:
      logger.error(f"MyHentaiGallery: no pictures found for {url}")
    return self.encode(images)
