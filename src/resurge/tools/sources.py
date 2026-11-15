"""URL hygiene and source typing.

``normalize_url`` produces a *fetchable* canonical URL (tracking params and
fragments stripped, host lower-cased, default ports and trailing-slash variance
removed); ``canonical_key`` additionally folds ``www.`` and the scheme for
de-duplication. ``classify_source`` maps a URL to a :data:`SourceType` using the
competitor's official domains plus curated domain lists, and ``PRIOR_RELIABILITY``
gives the prior trust in each type.
"""

from __future__ import annotations

import ipaddress
import warnings
from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..schemas import SourceType
from .http import host_of

try:  # Public Suffix List lookups (bundled data, no network)
    from tld import get_fld as _get_fld
except ImportError:  # pragma: no cover - tld is a declared transitive dependency
    _get_fld = None

PRIOR_RELIABILITY: dict[SourceType, float] = {
    "official": 0.9,
    "reference": 0.8,
    "news": 0.8,
    "code": 0.75,
    "review": 0.7,
    "community": 0.55,
    "social": 0.5,
    "other": 0.5,
}

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "_hs", "__hs", "vero_", "oly_", "ga_", "trk_")
_TRACKING_PARAMS = frozenset({
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid", "gbraid", "wbraid", "twclid", "ttclid",
    "li_fat_id", "igshid", "igsh", "mkt_tok", "ref", "ref_src", "ref_url", "referrer", "_ga", "_gl",
    "si", "spm", "scid", "cmpid", "s_cid", "trk", "trkcampaign", "sc_campaign", "ncid", "ocid", "cvid",
    "oc", "rb_clickid", "wickedid", "hsctatracking", "sr_share", "fb_action_ids", "fb_action_types",
    "fb_source", "action_object_map", "action_type_map", "action_ref_map", "__s", "_bta_tid", "_bta_c",
})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or k.startswith(_TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Canonical, still-fetchable form of ``url`` (non-HTTP inputs are returned stripped)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        head = raw.split("/", 1)[0]
        if ":" in head and not head.split(":", 1)[1].isdigit():  # mailto:, javascript:, ...
            return raw
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return raw
    host = parts.hostname.rstrip(".").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    query = urlencode(sorted(pairs)) if pairs else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_key(url: str) -> str:
    """De-duplication key: ``normalize_url`` minus scheme and a leading ``www.``."""
    norm = normalize_url(url)
    if not is_http_url(norm):
        return norm
    rest = norm.split("://", 1)[1]
    return rest[4:] if rest.startswith("www.") else rest


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or", "go", "ltd", "plc"})


def _psl_lookup(host: str) -> str | None:
    # tld reads its bundled list with codecs.open (deprecated on 3.14) on first use
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return _get_fld(host, fix_protocol=True, fail_silently=True)


@lru_cache(maxsize=16384)
def _registrable(host: str) -> str:
    if not host or _is_ip(host) or "." not in host:
        return host
    if _get_fld is not None:
        try:
            fld = _psl_lookup(host)
        except Exception:  # noqa: BLE001 - malformed host; use the heuristic below
            fld = None
        if fld:
            return str(fld).lower()
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_of(url: str) -> str:
    """Registrable domain of a URL or bare host (``docs.foo.co.uk`` -> ``foo.co.uk``)."""
    return _registrable(host_of(url or ""))


# --------------------------------------------------------------------------- #
# Curated domain lists (matched against the host and all its parent domains)
# --------------------------------------------------------------------------- #
_CODE = frozenset({
    "github.com", "githubusercontent.com", "github.io", "gitlab.com", "gitlab.io", "bitbucket.org",
    "codeberg.org", "sourceforge.net", "npmjs.com", "npmjs.org", "pypi.org", "crates.io", "rubygems.org",
    "packagist.org", "nuget.org", "pkg.go.dev", "hub.docker.com", "huggingface.co", "mvnrepository.com",
    "central.sonatype.com", "libraries.io", "stackshare.io", "jsr.io", "deno.land", "pub.dev",
    "readthedocs.io", "readthedocs.org", "replit.com", "codepen.io", "gitee.com",
})
_COMMUNITY = frozenset({
    "reddit.com", "redd.it", "news.ycombinator.com", "stackoverflow.com", "stackexchange.com",
    "superuser.com", "serverfault.com", "askubuntu.com", "quora.com", "discord.com", "discord.gg",
    "lobste.rs", "slashdot.org", "indiehackers.com", "medium.com", "dev.to", "hashnode.com", "hashnode.dev",
    "substack.com", "lemmy.world", "tildes.net", "spiceworks.com", "community.spiceworks.com",
    "forum.xda-developers.com", "discourse.org", "hackernoon.com",
})
_REVIEW = frozenset({
    "g2.com", "capterra.com", "trustradius.com", "trustpilot.com", "getapp.com", "producthunt.com",
    "gartner.com", "softwareadvice.com", "glassdoor.com", "clutch.co", "goodfirms.co", "peerspot.com",
    "saasworthy.com", "crozdesk.com", "financesonline.com", "selecthub.com", "featuredcustomers.com",
    "alternativeto.net", "saashub.com", "softwaresuggest.com", "apps.apple.com", "play.google.com", "chromewebstore.google.com", "appsumo.com", "comparably.com",
    "sitejabber.com", "consumeraffairs.com", "techjockey.com", "capterra.co.uk",
})
_REVIEW_STEMS = frozenset({"capterra", "trustpilot", "getapp", "softwareadvice", "trustradius", "glassdoor"})
_REFERENCE = frozenset({
    "wikipedia.org", "wikidata.org", "wikimedia.org", "crunchbase.com", "britannica.com", "pitchbook.com",
    "cbinsights.com", "owler.com", "zoominfo.com", "dnb.com", "sec.gov", "opencorporates.com",
    "statista.com", "tracxn.com", "craft.co", "macrotrends.net", "wellfound.com", "angel.co",
    "builtwith.com", "similarweb.com", "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk",
    "investopedia.com", "dealroom.co", "growjo.com", "getlatka.com", "leadiq.com", "rocketreach.co",
})
_SOCIAL = frozenset({
    "x.com", "twitter.com", "t.co", "linkedin.com", "lnkd.in", "youtube.com", "youtu.be", "facebook.com",
    "fb.com", "instagram.com", "tiktok.com", "threads.net", "threads.com", "bsky.app", "mastodon.social",
    "pinterest.com", "t.me", "telegram.org", "snapchat.com", "tumblr.com", "vimeo.com", "twitch.tv",
    "weibo.com",
})
_NEWS = frozenset({
    "techcrunch.com", "theverge.com", "wired.com", "venturebeat.com", "reuters.com", "bloomberg.com",
    "wsj.com", "nytimes.com", "ft.com", "cnbc.com", "forbes.com", "businessinsider.com", "insider.com",
    "theinformation.com", "axios.com", "zdnet.com", "cnet.com", "engadget.com", "arstechnica.com",
    "techradar.com", "techrepublic.com", "infoworld.com", "computerworld.com", "pcmag.com", "pcworld.com",
    "fastcompany.com", "inc.com", "entrepreneur.com", "fortune.com", "bbc.co.uk", "bbc.com",
    "theguardian.com", "washingtonpost.com", "apnews.com", "cnn.com", "nbcnews.com", "cbsnews.com",
    "abcnews.go.com", "usatoday.com", "economist.com", "marketwatch.com", "finance.yahoo.com",
    "news.yahoo.com", "news.google.com", "msn.com", "siliconangle.com", "geekwire.com", "thenextweb.com",
    "sifted.eu", "eu-startups.com", "businesswire.com", "prnewswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com", "prweb.com", "seekingalpha.com", "fool.com", "thestreet.com",
    "barrons.com", "digitaltrends.com", "mashable.com", "gizmodo.com", "9to5mac.com", "9to5google.com",
    "macrumors.com", "androidpolice.com", "theregister.com", "techmeme.com", "semafor.com", "politico.com",
    "npr.org", "latimes.com", "time.com", "newsweek.com", "hbr.org", "technologyreview.com",
    "informationweek.com", "crn.com", "sdtimes.com", "thenewstack.io", "infoq.com", "protocol.com",
    "theatlantic.com", "vox.com", "qz.com", "cnbctv18.com", "economictimes.indiatimes.com", "livemint.com",
    "yourstory.com", "inc42.com", "techinasia.com", "scmp.com", "nikkei.com", "asia.nikkei.com",
    "dw.com", "france24.com", "lemonde.fr", "handelsblatt.com", "techeu.com", "tech.eu", "pymnts.com",
    "fiercevideo.com", "adweek.com", "adage.com", "martech.org", "searchengineland.com", "nasdaq.com",
})


def _match(host: str, domains: frozenset[str]) -> bool:
    labels = host.split(".")
    return any(".".join(labels[i:]) in domains for i in range(len(labels) - 1))


def classify_source(url: str, official_domains: Iterable[str] = ()) -> SourceType:
    """Source type of ``url``; ``official`` when it sits on a competitor's own domain."""
    host = host_of(url or "")
    if not host:
        return "other"
    dom = domain_of(host)
    for official in official_domains or ():
        off_host = host_of(official or "")
        if off_host and (dom == domain_of(off_host) or host == off_host or host.endswith("." + off_host)):
            return "official"
    if _match(host, _CODE):
        return "code"
    if _match(host, _COMMUNITY):
        return "community"
    if _match(host, _REVIEW) or dom.split(".")[0] in _REVIEW_STEMS:
        return "review"
    if _match(host, _REFERENCE):
        return "reference"
    if _match(host, _SOCIAL):
        return "social"
    if _match(host, _NEWS):
        return "news"
    labels = host.split(".")
    if labels[-1] == "gov" or (len(labels) >= 3 and labels[-2] == "gov"):
        return "reference"
    first = labels[0] if len(labels) > 2 else ""
    if first in {"forum", "forums", "community", "discuss", "discussions", "answers"}:
        return "community"
    if first in {"news", "press", "newsroom"} and dom not in {"ycombinator.com"}:
        return "news"
    return "other"
