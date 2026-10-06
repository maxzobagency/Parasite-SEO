"""Decides whether a SERP result is a "parasite" page, and finds new parasite hosts."""
from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Common two-part public suffixes so root_domain() works without extra dependencies.
_SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "edu.au",
    "co.nz", "co.in", "com.br", "com.mx", "co.za", "com.sg", "com.tr", "co.jp",
    "com.ar", "com.co", "com.ng", "com.pk", "com.ph", "com.my", "com.hk", "co.id",
}
DEFAULT_ENABLED = ["ugc_blog", "forum_qa", "docs_hosting", "code_static_hosting",
                   "professional_social", "news_contributor", "suspected"]

# ---- smart detection: signals that a page is user-generated even if the host is unknown ----------
_STRONG_PATH = ("/@", "/u/", "/user/", "/users/", "/profile", "/members/", "/member/", "/pulse/",
                "/thread", "/forum", "/questions/", "/question/", "/answers/", "/discussion",
                "/community/", "/groups/", "/people/", "/ask/")
_WEAK_PATH = ("/author/", "/post/", "/posts/", "/topic", "/blog/", "/notes/", "/ideas/",
              "/portfolio/", "/wiki/", "/p/", "/articles/", "/article/")
_STRONG_HOST = ("forum.", "forums.", "community.", "answers.", "discuss.", "ask.", "members.", "discussion.")
_WEAK_HOST = ("blog.", "sites.", "pages.", "wiki.", "profiles.", "user.", "my.")
_TITLE_CUES = ("forum", "community", "discussion", "thread", "q&a", "questions and answers",
               "user reviews", "member", "profile")
SUSPECT_THRESHOLD = 3   # URL+title signals needed to flag an unknown site inline
CANDIDATE_THRESHOLD = 2  # signals needed to list it as a suggestion


def suspect_score(url: str, title: str = "") -> tuple[int, list[str]]:
    """How much does this *single result* look like user-generated / hosted content?
    Pure URL + title heuristics, so it works on the very first search with no history."""
    host = host_of(url)
    path = (urlparse(url if "//" in url else "//" + url).path or "/").lower()
    score, why = 0, []
    for pat in _STRONG_PATH:
        if pat in path:
            score += 3 if pat in ("/@", "/u/") else 2
            why.append(f"user-content URL path '{pat}'")
            break
    else:
        for pat in _WEAK_PATH:
            if pat in path:
                score += 1; why.append(f"blog/post-style URL path '{pat}'"); break
    if host.startswith(_STRONG_HOST):
        score += 2; why.append(f"host starts with '{host.split('.')[0]}.'")
    elif host.startswith(_WEAK_HOST):
        score += 1; why.append(f"host starts with '{host.split('.')[0]}.'")
    t = (title or "").lower()
    if any(c in t for c in _TITLE_CUES):
        score += 1; why.append("title mentions forum/community/profile")
    # username-style sub-domain on a non-obvious host, e.g. john-reviews.somesite.com/best-x
    labels = host.split(".")
    if len(labels) >= 3 and root_domain(host) != host and labels[0] not in ("www", "m", "shop", "store", "blog", "support", "help", "docs", "news", "app"):
        score += 1; why.append(f"personal-style sub-domain '{labels[0]}'")
    return score, why


def host_of(url: str) -> str:
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def root_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class Verdict:
    is_parasite: bool
    platform: str = ""      # the matching rule, e.g. "medium.com" or "forbes.com/sites"
    category: str = ""
    reason: str = ""        # human-readable why
    confidence: str = ""    # high | medium | low


class Detector:
    def __init__(self, enabled_categories: list[str] | None = None,
                 overrides: dict[str, str] | None = None,
                 builtin_path: Path | None = None):
        path = builtin_path or Path(__file__).with_name("parasites.toml")
        data = tomllib.loads(path.read_text())
        self.enabled = set(enabled_categories or DEFAULT_ENABLED)
        self.rules: list[tuple[str, str, str, str]] = []  # (host, path, category, raw)
        for cat, body in data.get("categories", {}).items():
            if cat not in self.enabled and cat != "edu_gov":
                continue
            if cat == "suspected":
                continue
            for raw in body.get("rules", []):
                host, _, p = raw.partition("/")
                self.rules.append((host, "/" + p if p else "", cat, raw))
        # user overrides from the DB: {"domain": "parasite" | "ignore"}
        self.overrides = overrides or {}

    # ---- matching -------------------------------------------------------
    @staticmethod
    def _host_match(host: str, rule_host: str) -> bool:
        if rule_host.endswith("."):          # label-prefix rule, e.g. "forums."
            return host.startswith(rule_host)
        return host == rule_host or host.endswith("." + rule_host)

    def classify(self, url: str, title: str = "") -> Verdict:
        host = host_of(url)
        path = urlparse(url if "//" in url else "//" + url).path or "/"
        rd = root_domain(host)

        ov = self.overrides.get(host) or self.overrides.get(rd)
        if ov == "ignore":
            return Verdict(False)
        if ov == "parasite":
            return Verdict(True, rd, "custom", "marked as parasite by you", "high")

        best: Verdict | None = None
        for rule_host, rule_path, cat, raw in self.rules:
            if cat == "edu_gov" or not self._host_match(host, rule_host):
                continue
            if rule_path and not path.lower().startswith(rule_path.lower()):
                continue
            # specific path rules beat bare host rules
            v = Verdict(True, raw.rstrip("."), cat, f"matches {cat} rule '{raw}'", "high")
            if best is None or len(raw) > len(best.platform):
                best = v
        if best:
            return best

        # Heuristic: .edu / .gov / .ac.* pages. Mostly "hijacked or abandoned" pages ranking for
        # commercial queries show up here; low confidence, off unless edu_gov is enabled.
        if "edu_gov" in self.enabled and (
            host.endswith((".edu", ".gov")) or ".ac." in host or ".edu." in host or ".gov." in host
        ):
            return Verdict(True, rd, "edu_gov", "institutional domain (check relevance manually)", "low")
        if "suspected" in self.enabled:
            score, why = suspect_score(url, title)
            if score >= SUSPECT_THRESHOLD:
                return Verdict(True, rd, "suspected", "looks user-generated: " + "; ".join(why), "low")
        return Verdict(False)


# ---- discovery ------------------------------------------------------------
def discover_candidates(rows: list[dict], known_parasite_roots: set[str],
                        min_subdomains: int = 3, min_keywords: int = 3,
                        min_niches: int = 3, min_niche_keywords: int = 8) -> list[dict]:
    """Find domains that *behave* like parasite hosts but aren't on any list yet.

    Signals (all computed from your own SERP data):
      * many distinct sub-domains of one root  -> it hands out sub-domains to users
        (blogspot-style hosting)
      * one root appearing in many niches with many distinct URL paths -> generic UGC site
      * a single result whose URL/title looks user-generated (see suspect_score)
    rows: dicts with keys keyword, niche, url, host, root, title.
    """
    by_root: dict[str, dict] = defaultdict(lambda: {
        "hosts": set(), "keywords": set(), "niches": set(), "urls": set(), "susp": 0, "why": [],
        "susp_url": ""})
    for r in rows:
        d = by_root[r["root"]]
        d["hosts"].add(r["host"])
        d["keywords"].add(r["keyword"])
        d["niches"].add(r["niche"] or "")
        d["urls"].add(r["url"])
        sc, why = suspect_score(r["url"], r.get("title", ""))
        if sc > d["susp"]:
            d["susp"], d["why"], d["susp_url"] = sc, why, r["url"]

    out = []
    for root, d in by_root.items():
        if root in known_parasite_roots:
            continue
        sub_hosts = len(d["hosts"])
        score, signals = 0, []
        if sub_hosts >= min_subdomains and len(d["keywords"]) >= min_keywords:
            score += 2
            signals.append(f"{sub_hosts} different sub-domains rank")
        if len(d["niches"]) >= min_niches and len(d["keywords"]) >= min_niche_keywords \
                and len(d["urls"]) >= min_niche_keywords:
            score += 1
            signals.append(f"ranks across {len(d['niches'])} niches")
        if d["susp"] >= CANDIDATE_THRESHOLD:
            score += d["susp"] - 1
            signals.append("; ".join(d["why"]))
        if score:
            out.append({"domain": root, "score": score, "keywords": len(d["keywords"]),
                        "niches": len(d["niches"]), "subdomains": sub_hosts,
                        "signals": "; ".join(signals),
                        "example": d["susp_url"] or sorted(d["urls"])[0]})
    out.sort(key=lambda x: (-x["score"], -x["keywords"]))
    return out
