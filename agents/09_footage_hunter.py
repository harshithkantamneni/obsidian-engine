"""
Agent 09 - Footage Hunter v2
Wikimedia-first for historical content. Stock video (the configured footage
provider — providers.footage, Pexels by default) only for pure atmosphere.
"""

import sys
import json
import requests
import time
from pathlib import Path
_BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(_BASE))
from dotenv import load_dotenv
load_dotenv()

_CACHE_FILE = _BASE / "outputs" / "footage_cache.json"
_CACHE_TTL = 7 * 24 * 3600  # 7 days

def _load_cache():
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            # Prune expired entries
            now = time.time()
            data = {k: v for k, v in data.items() if now - v.get("_ts", 0) < _CACHE_TTL}
            return data
    except Exception:
        pass
    return {}

def _save_cache(cache):
    try:
        _CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass

_footage_cache = _load_cache()
_session_cache = {}  # In-memory cache for this pipeline run to prevent duplicate API calls

def get_topic_fallbacks(topic: str) -> list:
    """Detect era from topic string and return appropriate Wikimedia search queries."""
    t = topic.lower()
    if any(w in t for w in ["roman", "caesar", "emperor", "legion", "senate", "nero", "claudius", "augustus"]):
        return [
            "ancient Roman emperor portrait painting",
            "Roman Senate ancient ruins",
            "Roman forum historical illustration",
            "ancient Rome gladiator mosaic",
            "Roman empire architecture ruins",
        ]
    elif any(w in t for w in ["egypt", "pharaoh", "pyramid", "cleopatra", "nile", "hieroglyph"]):
        return [
            "ancient Egyptian pharaoh painting",
            "pyramid Giza historical illustration",
            "Egyptian temple hieroglyphs",
            "ancient Egypt tomb art",
            "Nile river ancient Egyptian",
        ]
    elif any(w in t for w in ["medieval", "knight", "crusade", "plague", "castle", "inquisition", "feudal"]):
        return [
            "medieval knight illuminated manuscript",
            "medieval castle siege painting",
            "Black Death plague medieval art",
            "medieval court scene illustration",
            "crusades battle historical painting",
        ]
    elif any(w in t for w in ["colonial", "empire", "conquest", "settler", "slave", "revolution", "independence"]):
        return [
            "colonial era historical painting",
            "empire conquest historical illustration",
            "revolutionary war painting",
            "colonial settlement historical art",
            "historical slave trade illustration",
        ]
    elif any(w in t for w in ["greek", "greece", "athens", "sparta", "alexander", "troy", "socrates"]):
        return [
            "ancient Greek vase painting",
            "Athens Acropolis historical illustration",
            "Sparta warrior ancient Greek art",
            "Alexander Great historical painting",
            "ancient Greece temple ruins",
        ]
    else:
        return [
            "dark history ancient ruins painting",
            "historical conspiracy secret manuscript",
            "ancient illuminated manuscript scrolls",
            "historical execution public square painting",
            "medieval dungeon dark historical art",
        ]


_fallback_state: dict = {"topic": "", "queries": [], "idx": 0}


def next_fallback(topic: str = "") -> dict:
    """Return next fallback Wikimedia image appropriate for the given topic's era.
    Cycles through all era-specific queries, then tries generic fallbacks."""
    if topic != _fallback_state["topic"]:
        _fallback_state["topic"] = topic
        _fallback_state["queries"] = get_topic_fallbacks(topic)
        _fallback_state["idx"] = 0
    queries = _fallback_state["queries"]
    # Try up to len(queries) different fallback queries before giving up
    attempts = len(queries) if queries else 1
    for _ in range(attempts):
        query = queries[_fallback_state["idx"] % len(queries)] if queries else "ancient historical art painting"
        _fallback_state["idx"] += 1
        result = search_wikimedia(query, prefer_painting=True)
        if result and result.get("url"):
            return result
    # Last resort: try a very generic query
    generic_result = search_wikimedia("ancient ruins historical painting", prefer_painting=True)
    if generic_result and generic_result.get("url"):
        return generic_result
    print(f"  [Wikimedia] WARNING: All fallback queries exhausted for topic '{topic}' — scene will have no image")
    return {"source": "wikimedia", "url": "", "width": 800, "height": 600, "credit": "Wikimedia Commons"}

def search_wikimedia(query, prefer_painting=True):
    """Search Wikimedia Commons - returns thumbnail URL that actually downloads."""
    cache_key = f"wiki:{query}:{prefer_painting}"
    # Check in-memory session cache first (prevents duplicate API calls within same run)
    if cache_key in _session_cache:
        return _session_cache[cache_key]
    if cache_key in _footage_cache:
        cached = dict(_footage_cache[cache_key])
        cached.pop("_ts", None)
        return cached
    api_url = "https://commons.wikimedia.org/w/api.php"
    search_terms = [query + " painting", query + " portrait", query] if prefer_painting else [query]

    for term in search_terms:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": term,
            "gsrnamespace": 6,
            "gsrlimit": 10,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|thumburl",
            "iiurlwidth": 1920,
            "format": "json"
        }
        try:
            r = requests.get(api_url, params=params, timeout=10,
                           headers={"User-Agent": "ObsidianArchiveBot/1.0"})
            if r.status_code == 200:
                pages = json.loads(r.text, strict=False).get("query", {}).get("pages", {})
                candidates = []
                for page in pages.values():
                    info = page.get("imageinfo", [{}])[0]
                    mime = info.get("mime", "")
                    w = info.get("width", 0)
                    # Use thumburl (resized) which is more reliable to download
                    thumb_url = info.get("thumburl", "")
                    img_url = thumb_url or info.get("url", "")
                    if mime in ["image/jpeg", "image/png"] and w >= 1280 and img_url:
                        candidates.append({
                            "source": "wikimedia",
                            "url": img_url,
                            "width": min(w, 1920),
                            "height": info.get("height", 1080),
                            "title": page.get("title", ""),
                            "credit": f"Wikimedia Commons: {page.get('title', '')}"
                        })
                if candidates:
                    result = sorted(candidates, key=lambda x: x["width"], reverse=True)[0]
                    _footage_cache[cache_key] = {**result, "_ts": time.time()}
                    _session_cache[cache_key] = result
                    _save_cache(_footage_cache)
                    return result
        except Exception as e:
            print(f"  [Wikimedia] Error for '{term}': {e}")
    # Cache negative results in session to avoid re-querying within same run
    _session_cache[cache_key] = None
    return None

def search_stock_video(query):
    """Search the configured footage provider. Only used for pure atmospheric scenes.

    Returns {"source", "url", "width", "height", "duration", "credit"} or None.
    """
    try:
        from providers.registry import get_provider, get_provider_name
        provider_name = get_provider_name("footage")
        provider = get_provider("footage")
    except Exception as e:
        print(f"  [Footage] Provider unavailable: {e}")
        return None

    cache_key = f"{provider_name}:{query}"
    if cache_key in _session_cache:
        return _session_cache[cache_key]
    if cache_key in _footage_cache:
        cached = dict(_footage_cache[cache_key])
        cached.pop("_ts", None)
        return cached
    try:
        results = provider.search(query, orientation="landscape", min_duration=0, max_results=5)
        for item in results or []:
            if not item.get("url"):
                continue
            result = {
                "source": provider_name,
                "url": item["url"],
                "width": item.get("width", 1920),
                "height": item.get("height", 1080),
                "duration": item.get("duration", 10),
                "credit": item.get("credit") or f"Footage via {provider.name}",
            }
            _footage_cache[cache_key] = {**result, "_ts": time.time()}
            _session_cache[cache_key] = result
            _save_cache(_footage_cache)
            return result
    except Exception as e:
        print(f"  [{provider.name}] Error for '{query}': {e}")
    # Cache negative results in session to avoid re-querying within same run
    _session_cache[cache_key] = None
    return None


# Backward-compatible alias (pre-provider name)
search_pexels_video = search_stock_video

# Scene type routing
ATMOSPHERIC_QUERIES = {
    "dark": "dark stone texture ancient",
    "tense": "fire candle flame dark",
    "dramatic": "dramatic shadows torchlight",
    "cold": "marble stone cold ancient",
    "reverent": "ancient ruins golden light",
}

def run(scenes_data):
    scenes = scenes_data.get("scenes", [])
    topic = scenes_data.get("topic", "")
    print(f"[Footage Hunter v2] Finding visuals for {len(scenes)} scenes (Wikimedia-first)")

    (_BASE / "outputs" / "media" / "visuals").mkdir(parents=True, exist_ok=True)

    results = []
    for i, scene in enumerate(scenes):
        scene_id = scene.get("scene_id", i+1)
        visual_type = scene.get("visual_type", "historical_art")
        wikimedia_query = scene.get("wikimedia_query", "")
        pexels_query = scene.get("pexels_query", "")
        mood = scene.get("mood", "dark")

        print(f"  Scene {scene_id:02d}/{len(scenes)}: [{visual_type}] {wikimedia_query[:45] or pexels_query[:45]}")

        visual = None

        if visual_type in ["historical_art", "map", "text_overlay"]:
            # Always try Wikimedia first for historical content
            if wikimedia_query:
                visual = search_wikimedia(wikimedia_query, prefer_painting=True)
            if not visual and pexels_query:
                visual = search_wikimedia(pexels_query, prefer_painting=False)

        elif visual_type in ["broll_atmospheric", "broll_nature"]:
            # Atmospheric: try stock video first, Wikimedia as fallback
            atm_query = ATMOSPHERIC_QUERIES.get(mood, pexels_query) or pexels_query
            visual = search_stock_video(atm_query)
            if not visual and wikimedia_query:
                visual = search_wikimedia(wikimedia_query)

        # Universal fallback chain
        if not visual and wikimedia_query and wikimedia_query.strip():
            visual = search_wikimedia(wikimedia_query.split()[0] + " ancient historical")
        if not visual:
            visual = next_fallback(topic)
            print("    ↳ using era-matched fallback")

        results.append({
            "scene_id": scene_id,
            "narration": scene.get("narration", ""),
            "duration_seconds": scene.get("duration_seconds", 10),
            "visual": visual,
            "mood": mood
        })

        time.sleep(0.2)

    manifest = {
        "total_scenes": len(results),
        "scenes": results,
        "credits": list(set(s["visual"].get("credit", "") for s in results if s.get("visual")))
    }

    manifest_path = str(_BASE / "outputs" / "media" / "visuals_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    wikimedia_count = sum(1 for s in results if (s.get("visual") or {}).get("source") == "wikimedia")
    stock_count = sum(1 for s in results if (s.get("visual") or {}).get("source") not in (None, "wikimedia"))
    print(f"[Footage Hunter v2] Wikimedia: {wikimedia_count} | Stock video: {stock_count}")
    return manifest
