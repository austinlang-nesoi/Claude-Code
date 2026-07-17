#!/usr/bin/env python3
"""
build_resort_lookup.py

Builds (and incrementally refreshes) a local JSON lookup table of
resort name -> resort page URL, sourced from partner site XML sitemaps.

WHY THIS EXISTS
----------------
Fetching and parsing a site's full sitemap on every workflow run to find
one resort's URL is expensive (some of these sitemaps run 1,000-1,500+
URLs per file, across 2-5 files per site). This script does that work
ONCE (or on a schedule), and writes a small local JSON file. The actual
alt-text workflow then does a cheap dictionary/fuzzy lookup against that
file instead of touching the network at all.

USAGE
-----
    pip install requests
    python build_resort_lookup.py                 # full build
    python build_resort_lookup.py --refresh        # only re-fetch sub-sitemaps
                                                     # whose <lastmod> changed
    python build_resort_lookup.py --out lookup.json

OUTPUT
------
Writes a JSON file (default: resort_lookup.json) shaped like:

{
  "generated_at": "2026-07-17T00:00:00Z",
  "sitemap_lastmods": {
    "https://www.buyatimeshare.com/cpt_resort-sitemap.xml": "2026-07-16T18:57:43+00:00",
    ...
  },
  "resorts": [
    {
      "site": "timesharebrokerassociates.com",
      "brand_slug": "wyndham-destinations",   // null if no brand segment
      "slug": "wyndham-harbour-lights",
      "name": "Wyndham Harbour Lights",        // slug de-hyphenated, title-cased
      "url": "https://www.timesharebrokerassociates.com/resort/wyndham-destinations/wyndham-harbour-lights/"
    },
    ...
  ]
}

The alt-text workflow should load this file, build a
{normalized_name: [entries]} dict in memory, and match against it
instead of re-fetching sitemaps.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package: pip install requests")

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
USER_AGENT = "resort-lookup-builder/1.0 (+internal alt-text workflow tool)"
REQUEST_DELAY_SECONDS = 0.5  # be polite to partner sites

# Each site config says WHICH sub-sitemaps in its index count as "resort
# pages" for our purposes. Pattern is matched against the <loc> of each
# entry in the sitemap index.
SITE_CONFIGS = [
    {
        "site": "timesharebrokerassociates.com",
        "sitemap_index": "https://www.timesharebrokerassociates.com/sitemap_index.xml",
        "resort_sitemap_pattern": re.compile(r"/cpt_resort-sitemap\d*\.xml$"),
        "url_path_prefix": "/resort/",
    },
    {
        "site": "fidelityrealestate.com",
        "sitemap_index": "https://www.fidelityrealestate.com/sitemap_index.xml",
        "resort_sitemap_pattern": re.compile(r"/cpt_resort-sitemap\d*\.xml$"),
        "url_path_prefix": "/resort/",
    },
    {
        "site": "buyatimeshare.com",
        "sitemap_index": "https://www.buyatimeshare.com/sitemap_index.xml",
        "resort_sitemap_pattern": re.compile(r"/cpt_resort-sitemap\d*\.xml$"),
        "url_path_prefix": "/resort/",
    },
    {
        "site": "timesharesonly.com",
        "sitemap_index": "https://www.timesharesonly.com/sitemap_index.xml",
        "resort_sitemap_pattern": re.compile(r"/cpt_resort-sitemap\d*\.xml$"),
        "url_path_prefix": "/resort/",
    },
    {
        # NOTE: nookoutdoors.com does NOT have a cpt_resort sitemap. It's
        # built on a hotel-booking plugin (MotoPress Hotel Booking) and
        # organizes content by room/unit type, not resort. Included here
        # so it's visible in output, but treat matches as "unit" pages,
        # not resort pages, when wiring this into the alt-text workflow.
        "site": "nookoutdoors.com",
        "sitemap_index": "https://www.nookoutdoors.com/sitemap_index.xml",
        "resort_sitemap_pattern": re.compile(r"/mphb_room_type-sitemap\d*\.xml$"),
        "url_path_prefix": None,  # no consistent /resort/ path here
        "entry_kind": "unit",
    },
]


def fetch_xml(url: str) -> ET.Element:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return ET.fromstring(resp.content)


def get_index_entries(index_url: str):
    """Returns list of (loc, lastmod) from a sitemap index file."""
    root = fetch_xml(index_url)
    entries = []
    for sitemap_el in root.findall(f"{SITEMAP_NS}sitemap"):
        loc = sitemap_el.findtext(f"{SITEMAP_NS}loc")
        lastmod = sitemap_el.findtext(f"{SITEMAP_NS}lastmod")
        if loc:
            entries.append((loc.strip(), lastmod))
    return entries


def get_url_entries(sitemap_url: str):
    """Returns list of (loc, lastmod) from a leaf urlset sitemap file."""
    root = fetch_xml(sitemap_url)
    entries = []
    for url_el in root.findall(f"{SITEMAP_NS}url"):
        loc = url_el.findtext(f"{SITEMAP_NS}loc")
        lastmod = url_el.findtext(f"{SITEMAP_NS}lastmod")
        if loc:
            entries.append((loc.strip(), lastmod))
    return entries


def slug_to_name(slug: str) -> str:
    """'wyndham-harbour-lights' -> 'Wyndham Harbour Lights'"""
    words = slug.replace("_", "-").split("-")
    # Keep short connector words lowercase unless they start the name
    minor = {"a", "at", "the", "of", "and", "in", "on"}
    out = []
    for i, w in enumerate(words):
        if not w:
            continue
        if i > 0 and w.lower() in minor:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def parse_resort_url(url: str, url_path_prefix: str):
    """
    For URLs like /resort/{brand-slug}/{resort-slug}/ or /resort/{resort-slug}/
    returns (brand_slug_or_None, resort_slug).
    """
    if url_path_prefix and url_path_prefix in url:
        tail = url.split(url_path_prefix, 1)[1].strip("/")
        parts = [p for p in tail.split("/") if p]
        if len(parts) == 2:
            return parts[0], parts[1]
        elif len(parts) == 1:
            return None, parts[0]
    # Fallback: use last non-empty path segment as slug
    parts = [p for p in url.rstrip("/").split("/") if p]
    return None, parts[-1] if parts else url


def load_existing(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def build(out_path: str, refresh_only: bool):
    existing = load_existing(out_path) if refresh_only else None
    existing_lastmods = (existing or {}).get("sitemap_lastmods", {})

    all_lastmods = {}
    all_resorts = []

    for cfg in SITE_CONFIGS:
        print(f"[{cfg['site']}] reading sitemap index...", file=sys.stderr)
        try:
            index_entries = get_index_entries(cfg["sitemap_index"])
        except Exception as e:
            print(f"[{cfg['site']}] FAILED to read index: {e}", file=sys.stderr)
            continue

        resort_sitemaps = [
            (loc, lastmod)
            for loc, lastmod in index_entries
            if cfg["resort_sitemap_pattern"].search(loc)
        ]
        print(
            f"[{cfg['site']}] {len(resort_sitemaps)} resort sub-sitemap(s) found",
            file=sys.stderr,
        )

        for loc, lastmod in resort_sitemaps:
            all_lastmods[loc] = lastmod

            if refresh_only and existing_lastmods.get(loc) == lastmod:
                # Unchanged since last run -- reuse cached entries instead
                # of re-fetching.
                if existing:
                    cached = [
                        r for r in existing.get("resorts", [])
                        if r.get("_source_sitemap") == loc
                    ]
                    all_resorts.extend(cached)
                    print(f"  - {loc} unchanged, reused {len(cached)} cached entries", file=sys.stderr)
                    continue

            try:
                url_entries = get_url_entries(loc)
            except Exception as e:
                print(f"  - FAILED {loc}: {e}", file=sys.stderr)
                continue

            print(f"  - {loc}: {len(url_entries)} entries", file=sys.stderr)

            for url, _ in url_entries:
                # Skip placeholder/template URLs like %resort_brand%
                if "%" in url:
                    continue
                brand_slug, resort_slug = parse_resort_url(url, cfg["url_path_prefix"])
                all_resorts.append({
                    "site": cfg["site"],
                    "kind": cfg.get("entry_kind", "resort"),
                    "brand_slug": brand_slug,
                    "slug": resort_slug,
                    "name": slug_to_name(resort_slug),
                    "url": url,
                    "_source_sitemap": loc,
                })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sitemap_lastmods": all_lastmods,
        "resorts": all_resorts,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(
        f"\nWrote {len(all_resorts)} resort entries across "
        f"{len(SITE_CONFIGS)} sites to {out_path}",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="resort_lookup.json", help="Output JSON path")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Only re-fetch sub-sitemaps whose lastmod changed since last run",
    )
    args = parser.parse_args()
    build(args.out, args.refresh)


if __name__ == "__main__":
    main()
