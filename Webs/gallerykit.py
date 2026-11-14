"""Shared pieces for the "one gallery = one manga with one chapter" sources (and their search pages).

Nothing here depends on a site's CSS class names: galleries are recognised by the shape of their links,
page pictures by the folder their files live in.
"""
import re
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from .scraper import Scraper

IMG_EXT = r"\.(?:jpe?g|png|webp|gif|avif|bmp)"
IMG_RE = re.compile(r"https?:[^\s\"'<>)]+?" + IMG_EXT + r"(?:\?[^\s\"'<>)]*)?", re.I)
JUNK_RE = re.compile(r"(logo|avatar|favicon|icon|banner|placeholder|sprite|/ads?/|emoji|gravatar|loading|spinner|"
                     r"dmca|/themes/|/pictures/|button|badge|flag)", re.I)


def abs_url(url: str, base: str) -> str:
  url = (url or "").strip()
  if not url:
    return ""
  if url.startswith("//"):
    return "https:" + url
  return url if url.startswith(("http://", "https://")) else urljoin(base, url)


def first_src(img) -> str:
  if img is None:
    return ""
  for key in ("data-src", "data-lazy-src", "data-original", "data-cfsrc", "src"):
    v = (img.get(key) or "").strip()
    if v and not v.startswith("data:"):
      return v
  srcset = (img.get("data-srcset") or img.get("srcset") or "").strip()
  return srcset.split(",")[0].strip().split(" ")[0] if srcset else ""


def largest_folder(urls):
  """Page pictures live together in one folder; thumbnails of related items, logos and ads are scattered.
  Keep the biggest folder (page order kept)."""
  groups, order = {}, []
  for u in urls:
    p = urlparse(u)
    key = (p.netloc, p.path.rsplit("/", 1)[0])
    if key not in groups:
      groups[key] = []
      order.append(key)
    groups[key].append(u)
  if not groups:
    return []
  best = max(order, key=lambda k: (len(groups[k]), -order.index(k)))
  return groups[best]


def dedupe(seq):
  seen, out = set(), []
  for x in seq:
    if x and x not in seen:
      seen.add(x)
      out.append(x)
  return out


def title_from_slug(slug: str) -> str:
  return (slug or "").replace("-", " ").replace("_", " ").strip().title() or "Untitled"


def cards(bs, base: str, link_to_url):
  """Listing cards -> [{title, poster, url}]. `link_to_url(href)` returns the normalised gallery URL for a
  link that points at a gallery, else None. A card's picture/title are taken from the link or the smallest
  container around it that holds only that one gallery."""
  out, by_url = [], {}
  for a in bs.find_all("a", href=True):
    url = link_to_url(a["href"])
    if not url:
      continue
    img = a.find("img")
    card = a
    for _ in range(4):
      parent = card.parent
      if parent is None or parent.name in ("body", "html", "[document]"):
        break
      others = {link_to_url(x["href"]) for x in parent.find_all("a", href=True)} - {None}
      if len(others) > 1:
        break
      card = parent
      if card.find("img") is not None and card.find(["h2", "h3", "h4", "h5"]):
        break
    if img is None:
      img = card.find("img")
    title = (a.get("title") or "").strip()
    if not title:
      node = card.find(["h2", "h3", "h4", "h5"]) or card.find(class_=re.compile("title|name|caption", re.I))
      title = node.get_text(" ", strip=True) if node else ""
    if not title and img is not None:
      title = (img.get("alt") or "").strip()
    if not title:
      title = a.get_text(" ", strip=True)
    poster = abs_url(first_src(img), base)
    if url in by_url:
      prev = by_url[url]
      prev["title"] = prev["title"] or title
      prev["poster"] = prev["poster"] or poster
      continue
    by_url[url] = {"title": title, "poster": poster, "url": url}
    out.append(by_url[url])
  for r in out:
    r["title"] = r["title"] or title_from_slug(r["url"].rstrip("/").rsplit("/", 1)[-1])
  return out


def labelled(bs, *labels):
  """Names listed after a label like "Artists:" -> the anchors' texts in that block."""
  names = []
  for label in labels:
    node = bs.find(string=re.compile(r"^\s*" + re.escape(label) + r"\s*:?\s*$", re.I))
    holder = node.parent if node is not None else None
    for _ in range(3):
      if holder is None:
        break
      found = [a.get_text(" ", strip=True) for a in holder.find_all("a")]
      if found:
        names += [re.sub(r"\s*\(?\d[\d,\.]*\)?$", "", n).strip() for n in found]
        break
      holder = holder.parent
  return dedupe([n for n in names if n and len(n) < 50])


class GalleryScraper(Scraper):
  """Base: Chrome-fingerprint client for pages, plain headers first for images, one-chapter galleries."""
  label = "Gallery"

  def __init__(self, base: str, sf: str):
    super().__init__()
    self.url = base
    self.bg = None
    self.sf = sf
    self.cs = False
    self.impersonate = "chrome"
    self.headers = {"Referer": base, "Accept-Language": "en-US,en;q=0.9"}

  async def _page(self, url, **kw):
    for attempt in range(2):
      try:
        html = await self.get(url, cs=True, headers=self.headers, **kw)
        if html:
          return html
      except Exception as e:
        logger.warning(f"{self.label}: {url} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  def soup(self, html):
    return BeautifulSoup(html, "html.parser")

  def iter_chapters(self, data, page: int = 1):
    chapters = data.get("chapters") or []
    return chapters[(page - 1) * 60:page * 60] if page != 1 else chapters

  @staticmethod
  def encode(urls):
    return [quote(u, safe=":/%?=&+,()~@!$'*;[]#") for u in urls]
