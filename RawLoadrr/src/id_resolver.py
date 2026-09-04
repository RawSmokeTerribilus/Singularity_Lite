"""
Multi-provider ID resolver with cross-reference consensus.

Problem this solves: relying on a single database (TMDB) and blindly taking
search.results[0] mis-identifies releases when the real entry is missing,
renamed, or out-ranked by an unrelated title. See the "Disaster 2026" case:
the show was removed from TMDB, so results[0] fell through to "Pecheresses".

Approach: query several providers, normalise every hit to a shared id-set,
then vote.
  - General providers (always): TMDB, OMDb/IMDB, TVmaze.
        Voting key = the IMDB id (all three expose it).
  - Anime providers (only when the release looks like anime): MAL via Jikan,
        AniList. Voting key = the MAL id (AniList exposes idMal).
When >=2 providers agree the result is trusted ('high'); a lone hit is 'low';
nothing usable is 'none' -- the caller should then ask the user.

TVDB is intentionally NOT used: v4 moved to a paid per-user PIN model that
does not scale for a publicly distributed tool.

The module is self-contained and synchronous (plain requests) so it can be
called from the async prep pipeline via a normal function call.
"""

import re
import os
import json
import time
import unicodedata
import requests
from difflib import SequenceMatcher

# ─── tunables ──────────────────────────────────────────────────────────────
TIMEOUT = 12
MIN_CANDIDATE_SCORE = 0.50   # below this a provider hit is not a valid vote
CONSENSUS_VOTES     = 2      # providers that must agree for "high" confidence
STRONG_LEAD         = 0.70   # min score for a hit to trigger by-id verification
SCORE_MARGIN        = 0.15   # how far below the best score a candidate may sit
                             # and still win on votes alone
TMDB_LANGUAGE       = "es-ES"  # display language asked of TMDB; the catalogue
                              # is Spanish, so an English title scores badly

IMDB_PROVIDERS = ("tmdb", "omdb", "tvmaze")   # vote on the IMDB id
MAL_PROVIDERS  = ("jikan", "anilist")         # vote on the MAL id (anime)


# ─── text helpers ──────────────────────────────────────────────────────────
def _norm(s):
    """Lowercase, fold diacritics, drop punctuation, collapse whitespace.

    The diacritic fold has to happen before the ascii-only character class
    below, or every accented letter becomes a word break: "limites" would
    come out of "l\u00edmites" as "l mites", which then scores as two short
    tokens against one. That systematically penalises the correct match in a
    Spanish-language catalogue, which is most of what this resolver sees.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s):
    return set(_norm(s).split())


def _title_score(query, candidate):
    """0..1 similarity, with a containment boost for guessit-trimmed titles."""
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = SequenceMatcher(None, q, c).ratio()
    qt, ct = _tokens(query), _tokens(candidate)
    if qt and ct and (qt <= ct or ct <= qt):
        # one title is a token-subset of the other (e.g. "Disaster" in
        # "Disaster 2026") -- treat as a strong match, not a partial one.
        ratio = max(ratio, 0.88)
    return ratio


def _score(q_title, q_year, c_title, c_year):
    """Combined title + year score."""
    score = _title_score(q_title, c_title)
    if q_year and c_year:
        try:
            diff = abs(int(q_year) - int(c_year))
            if diff == 0:
                score += 0.05
            elif diff == 1:
                pass                       # release-year drift, tolerate
            else:
                score -= 0.30
        except (TypeError, ValueError):
            pass
    return max(0.0, min(1.0, score))


def _norm_imdb(v):
    """Normalise any IMDB id form to 'ttNNNNNNN', or '' if absent."""
    if not v:
        return ""
    v = str(v).strip()
    if not v or v in ("0", "tt0", "None"):
        return ""
    digits = re.sub(r"\D", "", v)
    if not digits or int(digits) == 0:
        return ""
    return "tt" + digits


def _int(v):
    try:
        n = int(str(v).strip())
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _cand(provider, title, year, category, tmdb=0, tvdb=0, imdb="", mal=0,
          alt=""):
    """One provider hit.

    `alt` is a second title for the same work -- the original-language title
    when the display title has been localised, or vice versa. Scoring takes
    the better of the two, so a Spanish query still matches a record whose
    display title came back in English.
    """
    return {
        "provider": provider, "title": title or "", "year": year,
        "alt_title": alt or "",
        "category": category, "tmdb": _int(tmdb), "tvdb": _int(tvdb),
        "imdb": _norm_imdb(imdb), "mal": _int(mal), "score": 0.0,
    }


# ─── persistence: override store + pending-IDs report ─────────────────────
# A conflictive release stays conflictive forever, so a human decision only
# needs to be made ONCE. Resolved ids are written to an override store; the
# resolver checks it first and a hit returns instantly with no API calls.
# Releases that could not be resolved unattended are appended to a pending
# report so a later triage pass can walk just those.

def _default_base():
    """RawLoadrr root dir (this file lives in <root>/src/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def overrides_path(base_dir=None):
    return os.path.join(base_dir or _default_base(), "tmp", "id_overrides.json")


def pending_path(base_dir=None):
    return os.path.join(base_dir or _default_base(), "tmp",
                        "needs_manual_id.jsonl")


def load_overrides(path=None):
    try:
        with open(path or overrides_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                           # noqa: BLE001
        return {}


def save_override(key, data, path=None):
    """Persist a (usually human-confirmed) id-set for a release key."""
    key = _norm(key)
    if not key:
        return
    path = path or overrides_path()
    store = load_overrides(path)
    store[key] = {"tmdb": _int(data.get("tmdb")),
                  "imdb": _norm_imdb(data.get("imdb")),
                  "tvdb": _int(data.get("tvdb")),
                  "mal": _int(data.get("mal")),
                  "category": data.get("category") or "TV",
                  "title": data.get("title") or "",
                  "year": data.get("year"),
                  "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, ensure_ascii=False)
    except Exception:                                           # noqa: BLE001
        pass


def clear_pending(path=None):
    try:
        os.remove(path or pending_path())
    except OSError:
        pass


def report_pending(entry, path=None):
    """Append one conflictive release to the pending-IDs report (JSONL)."""
    path = path or pending_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:                                           # noqa: BLE001
        pass


def load_pending(path=None):
    out = []
    try:
        with open(path or pending_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:                                           # noqa: BLE001
        pass
    return out


# ─── TMDB ──────────────────────────────────────────────────────────────────
class _TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, key, log):
        self.key, self.log = key, log

    def _get(self, path, **params):
        params["api_key"] = self.key
        r = requests.get(self.BASE + path, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def search(self, title, year, category):
        if not self.key:
            return []
        out = []
        try:
            # TMDB matches the query against every alternative title it holds,
            # but it renders the result in the requested display language and
            # defaults to en-US. Asking for Spanish keeps `title` comparable to
            # a Spanish query; `original_title` is carried alongside so a work
            # whose original language is Spanish still matches when TMDB has no
            # localised entry for it.
            if category == "MOVIE":
                p = {"query": title, "language": TMDB_LANGUAGE}
                if year:
                    p["year"] = year
                results = self._get("/search/movie", **p).get("results", [])
                kind = "MOVIE"
            else:
                p = {"query": title, "language": TMDB_LANGUAGE}
                if year:
                    p["first_air_date_year"] = year
                results = self._get("/search/tv", **p).get("results", [])
                kind = "TV"
            for r in results[:5]:
                ttl = r.get("title") or r.get("name") or ""
                alt = r.get("original_title") or r.get("original_name") or ""
                date = r.get("release_date") or r.get("first_air_date") or ""
                yr = _int(date[:4]) or None
                out.append(_cand("tmdb", ttl, yr, kind, tmdb=r.get("id"),
                                 alt=alt))
        except Exception as e:                                  # noqa: BLE001
            self.log(f"tmdb search failed: {e}")
        return out

    def fill_external(self, cand):
        """Populate imdb/tvdb for a TMDB candidate."""
        if not self.key or not cand["tmdb"]:
            return
        try:
            seg = "movie" if cand["category"] == "MOVIE" else "tv"
            ext = self._get(f"/{seg}/{cand['tmdb']}/external_ids")
            cand["imdb"] = _norm_imdb(ext.get("imdb_id")) or cand["imdb"]
            cand["tvdb"] = _int(ext.get("tvdb_id")) or cand["tvdb"]
        except Exception as e:                                  # noqa: BLE001
            self.log(f"tmdb external_ids failed: {e}")

    def verify_imdb(self, imdb):
        """By-id lookup. Returns a record dict if TMDB carries this IMDB id."""
        if not self.key or not imdb:
            return None
        try:
            res = self._get(f"/find/{imdb}", external_source="imdb_id")
            for kind, cat in (("tv_results", "TV"),
                              ("movie_results", "MOVIE")):
                arr = res.get(kind) or []
                if arr:
                    r = arr[0]
                    ttl = r.get("name") or r.get("title") or ""
                    date = (r.get("first_air_date")
                            or r.get("release_date") or "")
                    return {"title": ttl, "year": _int(date[:4]) or None,
                            "category": cat, "tmdb": _int(r.get("id")),
                            "tvdb": 0}
        except Exception as e:                                  # noqa: BLE001
            self.log(f"tmdb verify failed: {e}")
        return None


# ─── OMDb (IMDB) ───────────────────────────────────────────────────────────
def _omdb_search(key, title, year, category, log):
    if not key:
        return []
    out = []
    try:
        p = {"apikey": key, "t": title,
             "type": "movie" if category == "MOVIE" else "series"}
        if year:
            p["y"] = year
        r = requests.get("http://www.omdbapi.com/", params=p, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if d.get("Response") == "True":
            yr = _int(str(d.get("Year", ""))[:4]) or None
            out.append(_cand("omdb", d.get("Title"), yr, category,
                             imdb=d.get("imdbID")))
        else:
            # title+year exact miss -> broaden with a search list
            p2 = {"apikey": key, "s": title,
                  "type": "movie" if category == "MOVIE" else "series"}
            r2 = requests.get("http://www.omdbapi.com/", params=p2,
                              timeout=TIMEOUT)
            for hit in (r2.json().get("Search") or [])[:5]:
                yr = _int(str(hit.get("Year", ""))[:4]) or None
                out.append(_cand("omdb", hit.get("Title"), yr, category,
                                 imdb=hit.get("imdbID")))
    except Exception as e:                                      # noqa: BLE001
        log(f"omdb search failed: {e}")
    return out


# ─── TVmaze (keyless, TV only) ─────────────────────────────────────────────
def _tvmaze_search(title, category, log):
    if category == "MOVIE":
        return []
    out = []
    try:
        r = requests.get("https://api.tvmaze.com/search/shows",
                          params={"q": title}, timeout=TIMEOUT)
        r.raise_for_status()
        for hit in (r.json() or [])[:5]:
            show = hit.get("show", {})
            ext = show.get("externals", {}) or {}
            yr = _int(str(show.get("premiered", ""))[:4]) or None
            out.append(_cand("tvmaze", show.get("name"), yr, "TV",
                             tvdb=ext.get("thetvdb"), imdb=ext.get("imdb")))
    except Exception as e:                                      # noqa: BLE001
        log(f"tvmaze search failed: {e}")
    return out


# ─── by-id verification lookups ────────────────────────────────────────────
def _omdb_by_id(key, imdb, log):
    """Direct OMDb lookup by IMDB id (exact, no fuzzy search ranking)."""
    if not key or not imdb:
        return None
    try:
        r = requests.get("http://www.omdbapi.com/",
                          params={"apikey": key, "i": imdb}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if d.get("Response") == "True":
            return {"title": d.get("Title", ""),
                    "year": _int(str(d.get("Year", ""))[:4]) or None,
                    "tmdb": 0, "tvdb": 0}
    except Exception as e:                                      # noqa: BLE001
        log(f"omdb by-id failed: {e}")
    return None


def _tvmaze_by_imdb(imdb, log):
    """Direct TVmaze lookup by IMDB id."""
    if not imdb:
        return None
    try:
        r = requests.get("https://api.tvmaze.com/lookup/shows",
                          params={"imdb": imdb}, timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        show = r.json()
        if not isinstance(show, dict) or not show.get("id"):
            return None
        ext = show.get("externals", {}) or {}
        return {"title": show.get("name", ""),
                "year": _int(str(show.get("premiered", ""))[:4]) or None,
                "tmdb": 0, "tvdb": _int(ext.get("thetvdb"))}
    except Exception as e:                                      # noqa: BLE001
        log(f"tvmaze by-id failed: {e}")
    return None


# ─── MAL via Jikan (keyless, anime) ────────────────────────────────────────
def _jikan_search(title, log):
    out = []
    try:
        r = requests.get("https://api.jikan.moe/v4/anime",
                          params={"q": title, "limit": 5, "sfw": "true"},
                          timeout=TIMEOUT)
        r.raise_for_status()
        for hit in (r.json().get("data") or [])[:5]:
            ttl = hit.get("title_english") or hit.get("title") or ""
            alt = hit.get("title") or ""
            yr = _int(hit.get("year")) or None
            out.append(_cand("jikan", ttl, yr, "TV", mal=hit.get("mal_id"),
                             alt=alt))
    except Exception as e:                                      # noqa: BLE001
        log(f"jikan search failed: {e}")
    return out


# ─── AniList (keyless GraphQL, anime) ──────────────────────────────────────
_ANILIST_QUERY = """
query ($s: String) {
  Page(perPage: 5) {
    media(search: $s, type: ANIME) {
      idMal
      title { romaji english }
      startDate { year }
    }
  }
}
"""


def _anilist_search(title, log):
    out = []
    try:
        r = requests.post("https://graphql.anilist.co",
                          json={"query": _ANILIST_QUERY,
                                "variables": {"s": title}},
                          timeout=TIMEOUT)
        r.raise_for_status()
        media = (r.json().get("data", {}).get("Page", {}) or {}).get("media", [])
        for m in media[:5]:
            t = m.get("title", {}) or {}
            ttl = t.get("english") or t.get("romaji") or ""
            alt = t.get("romaji") or ""
            yr = _int((m.get("startDate") or {}).get("year")) or None
            out.append(_cand("anilist", ttl, yr, "TV", mal=m.get("idMal"),
                             alt=alt))
    except Exception as e:                                      # noqa: BLE001
        log(f"anilist search failed: {e}")
    return out


# ─── orchestrator ──────────────────────────────────────────────────────────
def resolve(title, year, category, config, aliases=None, mal_hint=False,
            override_key=None, log=None):
    """
    Resolve a release to a cross-checked id-set.

    Args:
        title     : guessed title (may be trimmed by guessit).
        year      : release/air year, or falsy.
        category  : 'TV' or 'MOVIE'.
        config    : RawLoadrr config dict (reads DEFAULT.tmdb_api / imdb_api).
        aliases   : extra title candidates (e.g. folder-derived names).
        mal_hint  : True when the release looks like anime -> the anime
                    providers (Jikan, AniList) are queried; otherwise they
                    are skipped so non-anime jobs are not spammed.

    Returns a dict:
        tmdb, tvdb (int), imdb (str 'tt..'), mal (int),
        category, title, year,
        confidence: 'high' | 'low' | 'none',
        votes: int, mal_votes: int, detail: list (per-provider candidates).

    'high'  -> >=2 providers agree, safe to use unattended.
    'low'   -> a best guess exists but unverified; caller should confirm.
    'none'  -> nothing usable; caller must ask the user.
    """
    log = log or (lambda _m: None)
    category = "MOVIE" if str(category).upper() == "MOVIE" else "TV"

    # resolved-once: a stored human decision wins immediately, no API calls
    if override_key:
        ov = load_overrides().get(_norm(override_key))
        if ov and (ov.get("tmdb") or ov.get("imdb")):
            log(f"override hit for '{override_key}'")
            return {"tmdb": _int(ov.get("tmdb")), "tvdb": _int(ov.get("tvdb")),
                    "imdb": _norm_imdb(ov.get("imdb")), "mal": _int(ov.get("mal")),
                    "category": ov.get("category") or category,
                    "title": ov.get("title") or title,
                    "year": ov.get("year") or year,
                    "confidence": "high", "votes": 99, "mal_votes": 0,
                    "detail": [], "source": "override"}

    defaults = (config or {}).get("DEFAULT", {})
    tmdb_key = defaults.get("tmdb_api") or ""
    omdb_key = defaults.get("imdb_api") or ""

    tmdb = _TMDB(tmdb_key, log)

    # Build a query set. guessit often eats a trailing year that is actually
    # part of the real name ("Disaster 2026" -> "Disaster"), so a
    # year-inclusive variant is always tried, plus folder-derived aliases.
    queries = []

    def _add_q(q):
        q = re.sub(r"\s+", " ", (q or "").strip())
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)

    _add_q(title)
    if year:
        _add_q(f"{title} {year}")
    for a in (aliases or []):
        _add_q(a)

    def _gather(cat):
        cands = []
        for q in queries:
            cands += tmdb.search(q, year, cat)
            cands += _omdb_search(omdb_key, q, year, cat, log)
            cands += _tvmaze_search(q, cat, log)
            if mal_hint:                       # anime providers, gated
                cands += _jikan_search(q, log)
                cands += _anilist_search(q, log)
        return cands

    def _best_score(cand):
        titles = [t for t in (cand["title"], cand.get("alt_title")) if t]
        return max((_score(q, year, t, cand["year"])
                    for q in queries for t in titles), default=0.0)

    def _dedupe(cands):
        seen = {}
        for c in cands:
            key = (c["provider"], c["imdb"], c["tmdb"], c["tvdb"], c["mal"],
                   _norm(c["title"]))
            if key not in seen or c["score"] > seen[key]["score"]:
                seen[key] = c
        return list(seen.values())

    candidates = _gather(category)
    for c in candidates:
        c["score"] = _best_score(c)
    candidates = _dedupe(candidates)
    valid = [c for c in candidates if c["score"] >= MIN_CANDIDATE_SCORE]

    # category may be wrong (movie tagged as TV or vice versa) -> retry once
    if len(valid) < CONSENSUS_VOTES:
        other = "MOVIE" if category == "TV" else "TV"
        log(f"weak results for {category}, retrying as {other}")
        extra = _gather(other)
        for c in extra:
            c["score"] = _best_score(c)
        candidates = _dedupe(candidates + extra)
        more = [c for c in candidates if c["score"] >= MIN_CANDIDATE_SCORE
                and c["category"] == other]
        if len(more) > len(valid):
            valid, category = more, other

    # TMDB search hits carry no IMDB id; fetch external_ids for the strongest
    # few so they can join the vote (bounded to avoid an API-call storm).
    valid.sort(key=lambda x: x["score"], reverse=True)
    expanded = 0
    for c in valid:
        if c["provider"] == "tmdb" and not c["imdb"] and expanded < 3:
            tmdb.fill_external(c)
            expanded += 1

    result = {"tmdb": 0, "tvdb": 0, "imdb": "", "mal": 0,
              "category": category, "title": title, "year": year,
              "confidence": "none", "votes": 0, "mal_votes": 0,
              "detail": sorted(candidates, key=lambda x: x["score"],
                               reverse=True)}

    # ── consensus 1: vote on the IMDB id (general providers) ──
    imdb_votes, pool = {}, {}

    def _pool(imdb, rec, score):
        """Accumulate the best title/year + any ids known for an IMDB id."""
        m = pool.setdefault(imdb, {"tmdb": 0, "tvdb": 0, "title": "",
                                   "year": None, "score": 0.0})
        m["tmdb"] = m["tmdb"] or _int(rec.get("tmdb"))
        m["tvdb"] = m["tvdb"] or _int(rec.get("tvdb"))
        if rec.get("title") and (not m["title"] or score > m["score"]):
            m["title"], m["year"] = rec["title"], rec.get("year")
        m["score"] = max(m["score"], score)

    # search votes: every valid hit votes; a set keeps each provider to one
    # vote per id, but a provider with several hits can vote for each.
    for c in valid:
        if c["provider"] in IMDB_PROVIDERS and c["imdb"]:
            imdb_votes.setdefault(c["imdb"], set()).add(c["provider"])
            _pool(c["imdb"], c, c["score"])

    # by-id cross-verification: a fuzzy title search can bury the right show
    # when regional licensing splits a title across databases (a CNN doc
    # listed as "Disaster: The Chernobyl Meltdown" on IMDB but "Chernobyl:
    # Inside the Meltdown" on TMDB). For each strong lead, ask every provider
    # DIRECTLY by IMDB id -- an exact-id hit is a real, unranked confirmation.
    have_consensus = any(len(v) >= CONSENSUS_VOTES for v in imdb_votes.values())
    if not have_consensus:
        leads = sorted({c["imdb"] for c in valid
                        if c["imdb"] and c["score"] >= STRONG_LEAD},
                       key=lambda k: -pool.get(k, {}).get("score", 0.0))
        for imdb in leads[:3]:
            voters = set(imdb_votes.get(imdb, set()))

            # Confirmations used to be pooled with a hardcoded 0.0. Today that
            # is inert -- every lead is already in the pool carrying the score
            # of the search hit that made it a lead, and _pool() keeps the max
            # -- but it stated the wrong thing: a by-id confirmation is the
            # strongest evidence there is, not the weakest. Carrying the lead's
            # own score says so, and stops a future caller that pools an
            # unseen id from silently creating a zero-scored contender that
            # the SCORE_MARGIN filter below would then have to catch.
            lead_score = pool.get(imdb, {}).get("score", 0.0)

            if "omdb" not in voters:
                rec = _omdb_by_id(omdb_key, imdb, log)
                if rec:
                    imdb_votes.setdefault(imdb, set()).add("omdb")
                    _pool(imdb, rec, lead_score)
            if "tmdb" not in voters:
                rec = tmdb.verify_imdb(imdb)
                if rec:
                    imdb_votes.setdefault(imdb, set()).add("tmdb")
                    _pool(imdb, rec, lead_score)
            if "tvmaze" not in voters:
                rec = _tvmaze_by_imdb(imdb, log)
                if rec:
                    imdb_votes.setdefault(imdb, set()).add("tvmaze")
                    _pool(imdb, rec, lead_score)

    imdb_n = 0
    if imdb_votes:
        # Votes used to be the primary key here, with the score only breaking
        # ties. That let a weak-but-popular candidate beat a near-perfect one:
        # two providers agreeing at 0.741 outranked a single provider at 1.000,
        # and the result was still reported as "high" confidence, so it was
        # applied without ever asking the operator.
        #
        # Score is now a filter: only candidates within SCORE_MARGIN of the
        # best score may compete, and votes decide among those. Consensus
        # still corrects a fuzzy-title mismatch between two plausible
        # candidates, but it can no longer overrule an exact match.
        best_score = max(pool[k]["score"] for k in imdb_votes)
        contenders = [k for k in imdb_votes
                      if pool[k]["score"] >= best_score - SCORE_MARGIN]
        winner = max(contenders,
                     key=lambda k: (len(imdb_votes[k]), pool[k]["score"]))
        imdb_n = len(imdb_votes[winner])
        merged = pool[winner]
        result.update(imdb=winner, tmdb=merged["tmdb"], tvdb=merged["tvdb"],
                      title=merged["title"] or title,
                      year=merged["year"] or year, votes=imdb_n)
        # backfill missing ids from the agreed IMDB id
        if not result["tmdb"]:
            rec = tmdb.verify_imdb(winner)
            if rec:
                result["tmdb"] = rec["tmdb"]
                result["category"] = rec.get("category") or category

    # ── consensus 2: vote on the MAL id (anime providers) ──
    mal_votes, mal_score = {}, {}
    for c in valid:
        if c["provider"] in MAL_PROVIDERS and c["mal"]:
            mal_votes.setdefault(c["mal"], set()).add(c["provider"])
            mal_score[c["mal"]] = max(mal_score.get(c["mal"], 0.0), c["score"])
    mal_n = 0
    if mal_votes:
        mwin = max(mal_votes,
                   key=lambda k: (len(mal_votes[k]), mal_score[k]))
        mal_n = len(mal_votes[mwin])
        result["mal"] = mwin
        result["mal_votes"] = mal_n

    # ── overall confidence: best of the two consensus tracks ──
    best_n = max(imdb_n, mal_n)
    if best_n >= CONSENSUS_VOTES:
        result["confidence"] = "high"
    elif best_n == 1:
        result["confidence"] = "low"
    else:
        result["confidence"] = "none"

    log(f"resolved '{title}' ({year}) -> {result['confidence']} "
        f"imdb={result['imdb']} tmdb={result['tmdb']} tvdb={result['tvdb']} "
        f"mal={result['mal']} votes={result['votes']}/{result['mal_votes']}")
    return result


# ─── CLI self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    try:
        from data.config import config as _cfg
    except Exception:                                           # noqa: BLE001
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
        from data.config import config as _cfg

    _title = sys.argv[1] if len(sys.argv) > 1 else "Disaster"
    _year = sys.argv[2] if len(sys.argv) > 2 else "2026"
    _cat = sys.argv[3] if len(sys.argv) > 3 else "TV"
    _aliases = sys.argv[4].split("|") if len(sys.argv) > 4 else None
    _mal = "--mal" in sys.argv or "anime" in (sys.argv[5:] or [])
    res = resolve(_title, _year, _cat, _cfg, aliases=_aliases,
                  mal_hint=_mal, log=print)
    print("\n=== RESULT ===")
    for k in ("confidence", "votes", "mal_votes", "imdb", "tmdb", "tvdb",
              "mal", "title", "year", "category"):
        print(f"  {k:11}: {res[k]}")
    print("\n=== CANDIDATES ===")
    for c in res["detail"]:
        print(f"  [{c['score']:.2f}] {c['provider']:8} {c['title']!r} "
              f"({c['year']}) imdb={c['imdb']} tmdb={c['tmdb']} "
              f"tvdb={c['tvdb']} mal={c['mal']}")
