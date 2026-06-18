"""
Moore Polska Articles Scraper
Scrapes all articles from https://moorepolska.pl/artykuly/
and appends only new articles to a CSV file.

Data is extracted primarily from JSON-LD structured data (rich & reliable),
with HTML fallbacks for every field.
"""

import csv
import json
import os
import time
import logging
from datetime import datetime

import html
import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_URL   = "https://moorepolska.pl"
LISTING_URL = f"{BASE_URL}/artykuly/"
CSV_FILE   = "moore_polska_articles.csv"
DELAY_SECONDS   = 1.5
REQUEST_TIMEOUT = 30

CSV_COLUMNS = [
    "url",
    "title",
    "date_published",
    "date_modified",
    "author",
    "category",
    "excerpt",
    "thumbnail_url",
    "full_text",
    "scraped_at",
]

# URL fragments that identify non-article pages to skip
SKIP_FRAGMENTS = [
    "/artykuly/", "/uslugi/", "/en/", "mailto:", "#",
    "/o-nas", "/kontakt", "/kariera", "/blog/", "/ogloszenia",
    "/szkolenia", "/case-study", "/webinar", "/newsletter",
    "/rekrutacje", "/historia", "/siec", "/benefity",
    "/sprawozdania", "/dane-rej", "/role/", "/moorepolskateam",
    "/esg-moore", "goaml",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; MoorePolskaScraper/1.0; "
        "+https://github.com/your-org/your-repo)"
    ),
    "Accept-Language": "pl,en;q=0.9",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_soup(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


def load_existing_urls(csv_path: str) -> set[str]:
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return {row["url"] for row in reader if row.get("url")}


def append_rows(csv_path: str, rows: list[dict]) -> None:
    file_exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


# ── Listing / pagination ──────────────────────────────────────────────────────

def get_all_article_urls() -> list[str]:
    """
    Walk every paginated page of /artykuly/ and collect article URLs.
    Articles are identified by empty-text <a> tags (thumbnail wrapper links)
    that point to non-category, non-service slugs.
    """
    all_urls: list[str] = []
    page = 1

    while True:
        listing_url = LISTING_URL if page == 1 else f"{BASE_URL}/artykuly/page/{page}/"
        log.info("Scraping listing page %d → %s", page, listing_url)

        soup = get_soup(listing_url)
        if soup is None:
            log.warning("Could not load page %d, stopping.", page)
            break

        found: list[str] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            # Article card thumbnail links have no visible text
            if (
                not text
                and href.startswith(BASE_URL)
                and href not in (BASE_URL, BASE_URL + "/")
                and not any(s in href for s in SKIP_FRAGMENTS)
                and href not in seen
            ):
                seen.add(href)
                found.append(href)

        if not found:
            log.info("No articles on page %d — pagination complete.", page)
            break

        log.info("  Found %d articles on page %d", len(found), page)
        all_urls.extend(found)
        page += 1
        time.sleep(DELAY_SECONDS)

    # Deduplicate preserving order
    seen_all: set[str] = set()
    unique: list[str] = []
    for u in all_urls:
        if u not in seen_all:
            seen_all.add(u)
            unique.append(u)

    log.info("Total unique article URLs: %d", len(unique))
    return unique


# ── Article scraping ──────────────────────────────────────────────────────────

def extract_jsonld(soup: BeautifulSoup) -> dict:
    """
    Parse the first JSON-LD <script> block and return a flat dict of useful fields.
    Moore Polska embeds a rich @graph with BlogPosting, WebPage, and Person nodes.
    """
    result: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        graph = data.get("@graph", [data])  # handle both graph and flat objects
        for node in graph:
            t = node.get("@type", "")
            if t == "BlogPosting":
                result["title"]          = node.get("headline") or node.get("name", "")
                result["date_published"] = node.get("datePublished", "")
                result["date_modified"]  = node.get("dateModified", "")
                result["excerpt"]        = node.get("description", "")
                result["category"]       = node.get("articleSection", "")
                # author may be nested
                author = node.get("author", {})
                if isinstance(author, dict):
                    result["author"] = author.get("name", "")
                elif isinstance(author, str):
                    result["author"] = author
            elif t == "Person" and "author" not in result:
                result["author"] = node.get("name", "")
            elif t in ("ImageObject", "WebPage") and not result.get("thumbnail_url"):
                url = node.get("url") or node.get("@id", "")
                if url and url.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    result["thumbnail_url"] = url
    return result


def scrape_article(url: str) -> dict | None:
    soup = get_soup(url)
    if soup is None:
        return None

    # ── Primary source: JSON-LD ───────────────────────────────────────────────
    ld = extract_jsonld(soup)

    def meta(name: str) -> str:
        tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else ""

    # ── Title ─────────────────────────────────────────────────────────────────
    title = html.unescape(ld.get("title") or "")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else html.unescape(meta("og:title"))

    # ── Dates ─────────────────────────────────────────────────────────────────
    date_published = ld.get("date_published") or meta("article:published_time")
    date_modified  = ld.get("date_modified")  or meta("article:modified_time")
    if not date_published:
        time_tag = soup.find("time")
        if time_tag:
            date_published = time_tag.get("datetime") or time_tag.get_text(strip=True)

    # ── Author ────────────────────────────────────────────────────────────────
    author = ld.get("author") or meta("author")

    # ── Category ─────────────────────────────────────────────────────────────
    category = ld.get("category") or meta("article:section")

    # ── Tags / keywords ───────────────────────────────────────────────────────

    # ── Excerpt ───────────────────────────────────────────────────────────────
    excerpt = ld.get("excerpt") or meta("og:description") or meta("description")

    # ── Thumbnail ─────────────────────────────────────────────────────────────
    thumbnail_url = ld.get("thumbnail_url") or meta("og:image")

    # ── Full text ─────────────────────────────────────────────────────────────
    body = (
        soup.find("article")
        or soup.find("div", class_=lambda c: c and "entry-content" in c)
        or soup.find("div", class_=lambda c: c and "elementor-location-single" in c)
        or soup.find("main")
    )
    if body:
        for tag in body.find_all(["nav", "footer", "aside", "script", "style"]):
            tag.decompose()
        full_text = body.get_text(separator="\n", strip=True)[:8000]
    else:
        full_text = ""

    return {
        "url":            url,
        "title":          title,
        "date_published": date_published,
        "date_modified":  date_modified,
        "author":         author,
        "category":       category,
        "excerpt":        excerpt,
        "thumbnail_url":  thumbnail_url,
        "full_text":      full_text,
        "scraped_at":     datetime.utcnow().isoformat(),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== Moore Polska articles scraper starting ===")
    log.info("CSV file: %s", os.path.abspath(CSV_FILE))

    existing_urls = load_existing_urls(CSV_FILE)
    log.info("Articles already in CSV: %d", len(existing_urls))

    all_urls = get_all_article_urls()
    new_urls = [u for u in all_urls if u not in existing_urls]
    log.info("New articles to scrape: %d", len(new_urls))

    if not new_urls:
        log.info("Nothing new. Exiting.")
        return

    new_rows: list[dict] = []
    for i, url in enumerate(new_urls, 1):
        log.info("[%d/%d] %s", i, len(new_urls), url)
        row = scrape_article(url)
        if row:
            new_rows.append(row)
        else:
            log.warning("  Skipped (failed).")
        time.sleep(DELAY_SECONDS)

    if new_rows:
        append_rows(CSV_FILE, new_rows)
        log.info("Appended %d new articles to %s", len(new_rows), CSV_FILE)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
