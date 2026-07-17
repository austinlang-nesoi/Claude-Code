#!/usr/bin/env python3
"""
resort_linker.py

Runtime helper for the alt-text/content workflow. Loads the JSON built by
build_resort_lookup.py and does fast, local, zero-network lookups + first-
occurrence linking. No sitemap fetching happens here -- that's the whole
point.

USAGE
-----
    from resort_linker import ResortLinker

    # `site` is whatever tag/domain identifies which site the article is
    # hosted on -- links will ONLY ever point back to that same site.
    linker = ResortLinker("resort_lookup.json", site="TBA")

    url = linker.find("Wyndham Harbour Lights")
    # -> "https://www.timesharebrokerassociates.com/resort/wyndham-destinations/wyndham-harbour-lights/"
    # (returns None if that resort doesn't have a page on TBA specifically,
    #  even if it exists on one of the other partner sites)

    linked_text = linker.link_first_occurrence(text)
    # wraps the first mention of any known resort name in an <a> tag
    # (or Markdown link, via `fmt="md"`)
"""

import json
import re
from difflib import SequenceMatcher


def _normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


SITE_TAG_ALIASES = {
    # Map the tag values used by the main workflow to the site domain keys
    # stored in resort_lookup.json (from build_resort_lookup.py's
    # SITE_CONFIGS).
    "TBA": "timesharebrokerassociates.com",
    "FREA": "fidelityrealestate.com",
    "BAT": "buyatimeshare.com",
    "TSO": "timesharesonly.com",
    "NOOK": "nookoutdoors.com",
}


def resolve_site(tag_or_domain: str) -> str:
    """Accepts either a workflow tag (e.g. 'TBA') or a raw domain
    (e.g. 'timesharebrokerassociates.com') and returns the domain."""
    if tag_or_domain in SITE_TAG_ALIASES:
        return SITE_TAG_ALIASES[tag_or_domain]
    if tag_or_domain in SITE_TAG_ALIASES.values():
        return tag_or_domain
    raise ValueError(
        f"Unrecognized site tag/domain: {tag_or_domain!r}. "
        f"Known tags: {list(SITE_TAG_ALIASES.keys())}, "
        f"known domains: {list(set(SITE_TAG_ALIASES.values()))}"
    )


class ResortLinker:
    def __init__(self, lookup_path: str, site: str):
        """
        site: the workflow tag (e.g. "TBA") or raw domain
        (e.g. "timesharebrokerassociates.com") that the current article is
        hosted on. Only resorts published on THIS site will ever be
        linked -- no cross-site links, ever.
        """
        with open(lookup_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.site = resolve_site(site)

        self._by_norm_name = {}
        for entry in data.get("resorts", []):
            if entry.get("site") != self.site:
                continue
            if entry.get("kind") == "unit":
                continue  # skip nookoutdoors-style unit pages by default
            key = _normalize(entry["name"])
            self._by_norm_name.setdefault(key, []).append(entry)

        self._all_keys = list(self._by_norm_name.keys())

    def find(self, name: str, fuzzy_threshold: float = 0.88):
        """
        Exact (normalized) match first, then fuzzy fallback. Returns URL
        or None. Only ever returns a URL on self.site.
        """
        key = _normalize(name)
        if key in self._by_norm_name:
            return self._by_norm_name[key][0]["url"]

        # Fuzzy fallback for near-miss spelling/wording
        best_key, best_score = None, 0.0
        for candidate_key in self._all_keys:
            score = SequenceMatcher(None, key, candidate_key).ratio()
            if score > best_score:
                best_key, best_score = candidate_key, score

        if best_score >= fuzzy_threshold:
            return self._by_norm_name[best_key][0]["url"]
        return None

    def link_first_occurrence(self, text: str, fmt: str = "html") -> str:
        """
        Finds the first mention of each known resort name in `text` and
        wraps it in a link. Only the FIRST occurrence of each resort is
        linked, matching the workflow requirement.
        """
        # Sort candidate names longest-first so "Wyndham Harbour Lights"
        # matches before a shorter partial like "Harbour Lights" would.
        names_by_length = sorted(
            {entries[0]["name"] for entries in self._by_norm_name.values()},
            key=len,
            reverse=True,
        )

        linked_spans = []  # (start, end) already claimed, to avoid double-linking
        result = text

        for name in names_by_length:
            pattern = re.compile(re.escape(name), re.IGNORECASE)
            match = pattern.search(result)
            if not match:
                continue
            start, end = match.span()
            if any(s < end and start < e for s, e in linked_spans):
                continue  # overlaps an already-linked span

            url = self.find(name)
            if not url:
                continue

            matched_text = result[start:end]
            if fmt == "md":
                replacement = f"[{matched_text}]({url})"
            else:
                replacement = f'<a href="{url}">{matched_text}</a>'

            result = result[:start] + replacement + result[end:]
            shift = len(replacement) - len(matched_text)
            linked_spans.append((start, end + shift))

        return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python resort_linker.py <lookup.json> <site_tag_or_domain>", file=sys.stderr)
        sys.exit(1)
    linker = ResortLinker(sys.argv[1], site=sys.argv[2])
    print(linker.find("Wyndham Harbour Lights"))
