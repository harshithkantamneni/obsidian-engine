#!/usr/bin/env python3
"""
Agent 00 — Topic Discovery Agent (v3)
Finds viral dark history topics, scores them, adds to Supabase queue.

Enhancements over v2:
- Dynamic scoring adjustments that scale with channel maturity
- New signals: subscriber conversion, engagement, search demand, traffic source,
  content patterns, demographic alignment
- Topic quality pre-filter (specificity, emotional weight, comparison format)
- Enhanced experiment selection: underexplored eras, untried content types
- Configurable thresholds via pipeline_config.SCORING_CONFIG
- Detailed score reporting with signal strength and data confidence
"""
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", interpolate=False)
sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.agent_wrapper import call_agent
from clients.supabase_client import add_topic, get_all_topics_done, get_client
from intel.dna_loader import get_agent_guidance
from intel.channel_insights import get_topic_discovery_intelligence, load_insights

# ── Import scoring config from pipeline_config (with safe fallbacks) ────────

try:
    from core.pipeline_config import (
        SCORING_CONFIG,
        SCORING_ADJUSTMENTS_EARLY,
        SCORING_ADJUSTMENTS_GROWING,
        SCORING_ADJUSTMENTS_MATURE,
        SCORING_THRESHOLDS,
    )
except ImportError:
    SCORING_CONFIG = {
        "maturity_threshold": 15,
        "max_score": 1.0,
        "min_score": 0.0,
        "default_topic_score": 0.5,
        "max_topics_per_discovery": 20,
        "experiment_cadence_default": 5,
        "experiment_cadence_throttled": 8,
    }
    SCORING_ADJUSTMENTS_EARLY = {
        "era_fatigue": -0.15, "audience_request": +0.20, "trending": +0.15,
        "cost_efficiency": +0.03, "shorts_subs": +0.03, "search_demand": +0.12,
        "engagement": +0.03, "subscriber_conversion": +0.05,
        "traffic_source": +0.03, "content_pattern": +0.03,
        "demographic_alignment": +0.02,
        "shorts_correlation": +0.03,
    }
    SCORING_ADJUSTMENTS_GROWING = {
        "era_fatigue": -0.20, "audience_request": +0.15, "trending": +0.10,
        "cost_efficiency": +0.05, "shorts_subs": +0.05, "search_demand": +0.10,
        "engagement": +0.05, "subscriber_conversion": +0.07,
        "traffic_source": +0.05, "content_pattern": +0.05,
        "demographic_alignment": +0.03,
        "shorts_correlation": +0.04,
    }
    SCORING_ADJUSTMENTS_MATURE = {
        "era_fatigue": -0.25, "audience_request": +0.12, "trending": +0.08,
        "cost_efficiency": +0.07, "shorts_subs": +0.05, "search_demand": +0.08,
        "engagement": +0.07, "subscriber_conversion": +0.10,
        "traffic_source": +0.05, "content_pattern": +0.05,
        "demographic_alignment": +0.03,
        "shorts_correlation": +0.05,
    }
    SCORING_THRESHOLDS = {}

# Shorthand for reading thresholds with safe defaults
def _th(key: str, default):
    return SCORING_THRESHOLDS.get(key, default)


PROMPT = """You are a YouTube strategist for "The Obsidian Archive" — dark untold history documentaries targeting Indian audiences aged 18-35.

Find {topic_count} compelling video topics that:
1. Have a shocking twist most people don't know
2. Involve betrayal, conspiracy, poison, hidden power, or suppressed truth
3. Cover Ancient, Medieval, Colonial, or Early Modern history
4. Have strong visual storytelling potential
5. Would resonate with Indian audiences (Indian history welcome, global also fine)

Already done (SKIP THESE):
{done_topics}

Return ONLY a JSON array:
[
  {{
    "topic": "Exact topic title",
    "hook": "One sentence — the shocking twist",
    "era": "Ancient/Medieval/Colonial/Modern",
    "score": 0.0-1.0,
    "reason": "Why this gets clicks"
  }}
]

Score using creative judgement AND channel performance data (if provided above):
- 0.9+: India angle OR extremely shocking twist OR top-performing era (check analytics above)
- 0.7-0.9: Strong hook, clear villain/victim, visual story OR solid-performing era
- 0.5-0.7: Good story, less unique OR underperforming era (check analytics above)

Return ONLY the JSON array."""


# ── Dynamic adjustment engine ──────────────────────────────────────────────

def _get_dynamic_adjustments(video_count: int) -> dict:
    """
    Returns adjustment magnitudes that scale with channel maturity.
    Early channels need bigger swings to find what works.
    Mature channels need smaller, refined adjustments.
    """
    threshold = SCORING_CONFIG.get("maturity_threshold", 15)
    if video_count < 5:
        return dict(SCORING_ADJUSTMENTS_EARLY)
    elif video_count < threshold:
        return dict(SCORING_ADJUSTMENTS_GROWING)
    else:
        return dict(SCORING_ADJUSTMENTS_MATURE)


def _get_video_count() -> int:
    """Get total published video count from channel insights or Supabase."""
    insights = load_insights()
    health = insights.get("channel_health", {})
    count = health.get("total_videos_published", 0)
    if count:
        return count
    # Fallback: count from Supabase
    try:
        client = get_client()
        result = client.table("videos").select("id").execute()
        return len(result.data) if result.data else 0
    except Exception:
        return 0


# ── Helper: Fetch recent videos from Supabase ────────────────────────────────

def _get_recent_video_eras(weeks: int = 4) -> list[str]:
    """Fetch eras of videos uploaded in the last N weeks from Supabase."""
    try:
        client = get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
        result = client.table("videos") \
            .select("topic, pipeline_state, created_at") \
            .gte("created_at", cutoff) \
            .order("created_at", desc=True) \
            .execute()
        eras = []
        for v in (result.data or []):
            ps = v.get("pipeline_state") or {}
            if isinstance(ps, str):
                try:
                    ps = json.loads(ps)
                except Exception:
                    ps = {}
            era = ps.get("era", "")
            if era:
                eras.append(era.lower())
        return eras
    except Exception as e:
        print(f"  [era rotation] Could not fetch recent videos: {e}")
        return []


def _get_queue_depth() -> int:
    """Count topics currently queued in Supabase."""
    try:
        client = get_client()
        result = client.table("topics") \
            .select("id") \
            .eq("status", "queued") \
            .execute()
        return len(result.data) if result.data else 0
    except Exception as e:
        print(f"  [queue depth] Could not fetch queue: {e}")
        return 10  # safe default


# ── Helper: Audience request mining ──────────────────────────────────────────

def _get_audience_requests() -> list[str]:
    """Extract audience topic requests from channel_insights.json."""
    insights = load_insights()
    sentiment = insights.get("comment_sentiment", {})
    requests = sentiment.get("audience_topic_requests", [])
    return [r.lower().strip() for r in requests if isinstance(r, str)]


# ── Helper: Trending awareness ───────────────────────────────────────────────

def _get_trending_history_topics() -> list[str]:
    """Use web search to find currently trending history topics on YouTube/Google."""
    try:
        print("  [trending] Searching for trending history topics...")
        response = call_agent(
            "00_topic_discovery",
            system_prompt="You are a research assistant. Return ONLY a JSON array of strings.",
            user_prompt=(
                "What history topics are trending on YouTube and Google right now? "
                "Focus on dark history, untold stories, ancient civilizations, conspiracies, "
                "and documentary-style content. "
                "Return ONLY a JSON array of 10-15 short topic keywords/phrases, e.g.:\n"
                '[\"Cleopatra conspiracy\", \"Lost Roman legion\", \"Mughal poison plots\"]'
            ),
            max_tokens=1000,
            use_search=True,
            expect_json=False,
            stage_num=0,
        )
        # Extract JSON array from response
        text = response.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        # Try to find JSON array in the text
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            topics = json.loads(text[start:end + 1], strict=False)
            print(f"  [trending] Found {len(topics)} trending topics")
            return [t.lower().strip() for t in topics if isinstance(t, str)]
    except Exception as e:
        print(f"  [trending] Web search failed (non-fatal): {e}")
    return []


# ── Helper: Experiment gating ────────────────────────────────────────────────

def _should_experiment() -> bool:
    """
    Check past experiment performance from channel_insights.
    Only flag experiments if last experiment had views above channel average.
    Falls back to legacy every-5th behavior if no data available.
    """
    insights = load_insights()
    health = insights.get("channel_health", {})
    avg_views = health.get("avg_views_per_video", 0)

    if not avg_views:
        # No analytics data — fall back to legacy behavior (always allow)
        return True

    # Check experiment recommendations from analytics agent
    exp_recs = insights.get("experiment_recommendations", [])

    # If experiment_recommendations exist, the analytics agent thinks we should experiment
    if exp_recs:
        return True

    # Analytics data exists but no experiment recommendation — skip experiments
    # The analytics agent actively recommends experiments when warranted
    return False


# ── Helper: Cost-per-view optimization ───────────────────────────────────────

def _get_cost_efficient_eras() -> dict[str, float]:
    """
    Return a dict of era -> bonus/penalty based on views-per-dollar efficiency.
    Eras with above-average efficiency get +0.05, others get 0.
    """
    insights = load_insights()
    era_perf = insights.get("era_performance", {})
    if not era_perf:
        return {}

    # Build efficiency scores (using avg_views as proxy since we don't have cost data,
    # but eras with more views per video are inherently more cost-efficient)
    efficiencies = {}
    for era, data in era_perf.items():
        if data.get("video_count", 0) >= 1 and data.get("avg_views", 0) > 0:
            # Use avg_views * avg_ctr as a composite efficiency metric
            views = data.get("avg_views", 0)
            ctr = data.get("avg_ctr", 1.0)
            efficiencies[era.lower().replace("_", " ")] = views * ctr

    if not efficiencies:
        return {}

    avg_efficiency = sum(efficiencies.values()) / len(efficiencies)
    result = {}
    for era, eff in efficiencies.items():
        if eff > avg_efficiency:
            result[era] = 0.05
            print(f"  [cost-efficiency] {era.title()} is cost-efficient — +0.05 boost")
    return result


# ── Helper: Shorts subscriber boost ────────────────────────────────────────

def _get_shorts_era_boosts() -> dict[str, float]:
    """
    Return era -> score bonus based on shorts subscriber performance.
    Eras where shorts generate above-average subs get +0.05.
    """
    insights = load_insights()
    shorts = insights.get("shorts_intelligence", {})
    era_perf = shorts.get("era_performance", {})
    if not era_perf:
        return {}

    subs_per_era = {
        era.lower().replace("_", " "): data.get("avg_subs_per_short", 0)
        for era, data in era_perf.items()
        if data.get("short_count", 0) >= 1
    }
    if not subs_per_era:
        return {}

    avg_subs = sum(subs_per_era.values()) / len(subs_per_era)
    result = {}
    for era, subs in subs_per_era.items():
        if subs > avg_subs:
            result[era] = 0.05
            print(f"  [shorts-boost] {era.title()} shorts generate above-avg subs — +0.05 boost")
    return result


def _get_shorts_long_correlation_boost(topic: str, era: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics in eras where shorts→long-form correlation is positive.
    If posting a short boosts its parent long-form's views, favor those eras.
    """
    correlation = insights.get("shorts_long_correlation", {})
    if not correlation or correlation.get("topics_with_shorts", 0) < 2:
        return (0.0, "")

    lift = correlation.get("view_lift_pct", 0)
    if lift <= 0:
        return (0.0, "")

    # Check per-era correlation (era data has paired_count, avg_long_views, avg_short_subs)
    era_corr = correlation.get("era_correlation", {})
    topic_era = era.lower().replace("_", " ")
    for era_key, data in era_corr.items():
        normalized = era_key.lower().replace("_", " ")
        if normalized in topic_era or topic_era in normalized:
            paired = data.get("paired_count", 0)
            if paired >= 1:
                return (0.05, f"shorts boost long-form views +{lift:.0f}% ({paired} pairs in {era_key})")

    # Channel-wide lift is positive even if no per-era match
    if lift > 10:
        return (0.03, f"shorts boost long-form views +{lift:.0f}% channel-wide")

    return (0.0, "")


# ── NEW: Subscriber conversion boost ────────────────────────────────────────

def _get_subscriber_conversion_boost(topic: str, era: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics in eras with high subscriber conversion rate (subs/views).
    Subscribers matter more than views for long-term growth.
    Returns (adjustment_value, reason_string).
    """
    era_perf = insights.get("era_performance", {})
    if not era_perf:
        return (0.0, "")

    # Collect subscriber conversion rates from long-form era performance
    conversion_rates = {}
    for era_key, data in era_perf.items():
        conv_rate = data.get("sub_conversion_rate", 0)
        if conv_rate > 0:
            normalized_key = era_key.lower().replace("_", " ")
            conversion_rates[normalized_key] = conv_rate

    # Supplement with shorts conversion data (often higher signal)
    shorts = insights.get("shorts_intelligence", {})
    shorts_era = shorts.get("era_performance", {})
    for era_key, data in shorts_era.items():
        normalized_key = era_key.lower().replace("_", " ")
        conv_rate = data.get("sub_conversion_rate", 0)
        if conv_rate > 0 and normalized_key not in conversion_rates:
            conversion_rates[normalized_key] = conv_rate

    if not conversion_rates:
        return (0.0, "")

    avg_rate = sum(conversion_rates.values()) / len(conversion_rates)
    high_mult = _th("sub_conversion_high_multiplier", 1.5)
    topic_era = era.lower().replace("_", " ")

    for era_key, rate in conversion_rates.items():
        if era_key in topic_era or topic_era in era_key:
            if rate > avg_rate * high_mult:
                return (0.10, f"high sub conversion era ({era_key})")
            elif rate > avg_rate:
                return (0.05, f"above-avg sub conversion ({era_key})")
            break

    return (0.0, "")


# ── NEW: Engagement boost ────────────────────────────────────────────────────

def _get_engagement_boost(topic: str, era: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics in eras with high engagement rate (likes+comments/views).
    High engagement signals algorithm favorability.
    Returns (adjustment_value, reason_string).
    """
    era_perf = insights.get("era_performance", {})
    if not era_perf:
        return (0.0, "")

    # Use retention as a proxy for engagement (avg_retention is engagement signal)
    engagement_by_era = {}
    for era_key, data in era_perf.items():
        retention = data.get("avg_retention", 0)
        video_count = data.get("video_count", 0)
        if retention > 0 and video_count >= _th("engagement_min_video_count", 1):
            normalized_key = era_key.lower().replace("_", " ")
            engagement_by_era[normalized_key] = retention

    if not engagement_by_era:
        return (0.0, "")

    avg_engagement = sum(engagement_by_era.values()) / len(engagement_by_era)
    topic_era = era.lower().replace("_", " ")

    for era_key, engagement in engagement_by_era.items():
        if era_key in topic_era or topic_era in era_key:
            if engagement > avg_engagement:
                return (0.05, f"high engagement era ({era_key}: {engagement:.0f}% retention)")
            break

    return (0.0, "")


# ── NEW: Search demand boost ────────────────────────────────────────────────

def _get_search_demand_boost(topic: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics whose words match top search terms driving traffic.
    Topics with proven search demand get guaranteed floor traffic.
    Returns (adjustment_value, reason_string).
    """
    # Check for search terms in search intelligence data
    search_intel = insights.get("search_intelligence", {})
    search_terms = search_intel.get("top_search_terms", [])

    # Also check tag performance for high-performing keywords
    tags = insights.get("tag_performance", {})
    high_tags = tags.get("high_performing_tags", [])

    # Also check top video titles for keyword extraction
    top_videos = insights.get("top_performing_videos", [])
    top_keywords = []
    for v in top_videos:
        title = v.get("title", "").lower()
        # Extract meaningful words (min_word_len+ chars)
        min_wl = _th("search_demand_min_word_len", 4)
        words = [w for w in re.split(r'[^a-zA-Z]+', title) if len(w) >= min_wl]
        top_keywords.extend(words)

    # Combine all search-relevant terms
    all_search_terms = []
    for term in search_terms:
        # search_intelligence.top_search_terms is a list of dicts: [{"term": str, "views": int}]
        t = term.get("term", "") if isinstance(term, dict) else str(term)
        all_search_terms.extend(t.lower().split())
    for tag in high_tags:
        all_search_terms.extend(tag.lower().split())
    all_search_terms.extend(top_keywords)

    if not all_search_terms:
        return (0.0, "")

    # Count significant word matches (min_word_len+ chars to avoid noise)
    min_wl = _th("search_demand_min_word_len", 4)
    min_matches = _th("search_demand_min_matches", 2)
    topic_lower = topic.lower()
    significant_terms = [t for t in all_search_terms if len(t) >= min_wl]
    matches = [t for t in set(significant_terms) if t in topic_lower]

    if len(matches) >= min_matches:
        matched_str = ", ".join(matches[:3])
        return (0.10, f"search demand: matches [{matched_str}]")

    return (0.0, "")


# ── NEW: Traffic source adjustment ──────────────────────────────────────────

def _get_traffic_source_adjustment(topic: str, era: str, insights: dict) -> tuple[float, str]:
    """
    Adjust scores based on the channel's dominant traffic source.
    Search-dependent channels: boost keyword-rich topics.
    Browse-dependent channels: boost sensational/click-worthy topics.
    Returns (adjustment_value, reason_string).
    """
    traffic = insights.get("traffic_sources", {})
    if not traffic:
        return (0.0, "")

    search_pct = traffic.get("search", {}).get("pct", 0)
    browse_pct = traffic.get("browse", {}).get("pct", 0)
    suggested_pct = traffic.get("suggested", {}).get("pct", 0)

    topic_lower = topic.lower()

    search_dom = _th("traffic_search_dominant_pct", 50)
    browse_dom = _th("traffic_browse_dominant_pct", 50)
    suggested_dom = _th("traffic_suggested_dominant_pct", 40)
    min_spec = _th("traffic_min_specificity_signals", 2)
    min_click = _th("traffic_min_click_signals", 2)

    if search_pct > search_dom:
        # Search-dominant: reward keyword-rich, specific topics
        specificity_signals = 0
        # Check for proper nouns (capitalized words in original topic)
        if re.search(r'[A-Z][a-z]+', topic):
            specificity_signals += 1
        # Check for dates/numbers
        if re.search(r'\d{2,4}', topic):
            specificity_signals += 1
        # Check for historically searchable terms
        search_terms = ["who", "what", "how", "why", "secret", "true story", "real", "history of"]
        if any(term in topic_lower for term in search_terms):
            specificity_signals += 1

        if specificity_signals >= min_spec:
            return (0.05, f"search-optimized topic (search traffic: {search_pct:.0f}%)")

    elif browse_pct > browse_dom or suggested_pct > suggested_dom:
        # Browse/suggested-dominant: reward sensational, click-worthy topics
        click_signals = 0
        click_words = ["secret", "hidden", "dark", "shocking", "untold", "forbidden",
                       "deadly", "mysterious", "terrifying", "cursed", "betrayal", "murder",
                       "conspiracy", "banned", "lost", "never told"]
        for word in click_words:
            if word in topic_lower:
                click_signals += 1
        if click_signals >= min_click:
            return (0.05, f"click-optimized topic (browse traffic: {browse_pct:.0f}%)")

    return (0.0, "")


# ── NEW: Content pattern boost ──────────────────────────────────────────────

def _get_content_pattern_boost(topic: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics matching content patterns that consistently get better retention/CTR.
    Uses content_pattern data from analytics, or infers from top performers.
    Returns (adjustment_value, reason_string).
    """
    # Infer content patterns from top performing videos (no explicit content_patterns key)
    high_retention_patterns = []

    # Infer patterns from top performing videos
    top_videos = insights.get("top_performing_videos", [])
    if not high_retention_patterns and top_videos:
        # Extract common patterns from top videos
        for v in top_videos:
            title = (v.get("title", "") or "").lower()
            if "vs" in title or "versus" in title:
                high_retention_patterns.append("comparison")
            if any(w in title for w in ["secret", "hidden", "untold"]):
                high_retention_patterns.append("mystery_reveal")
            if any(w in title for w in ["dark", "deadly", "cursed"]):
                high_retention_patterns.append("dark_theme")
            if any(w in title for w in ["true story", "real", "actually"]):
                high_retention_patterns.append("truth_reveal")

    if not high_retention_patterns:
        return (0.0, "")

    topic_lower = topic.lower()
    pattern_checks = {
        "comparison": "vs" in topic_lower or "versus" in topic_lower,
        "mystery_reveal": any(w in topic_lower for w in ["secret", "hidden", "untold", "mystery"]),
        "dark_theme": any(w in topic_lower for w in ["dark", "deadly", "cursed", "forbidden"]),
        "truth_reveal": any(w in topic_lower for w in ["true story", "real", "truth", "actually"]),
    }

    for pattern, matches in pattern_checks.items():
        if matches and pattern in high_retention_patterns:
            return (0.05, f"matches high-retention pattern: {pattern}")

    return (0.0, "")


# ── NEW: Demographic alignment boost ────────────────────────────────────────

def _get_sentiment_boost(topic: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics matching high-engagement or debate topics from comment sentiment.
    These topics drive passionate audience discussion and watch time.
    Returns (adjustment_value, reason_string).
    """
    sentiment = insights.get("comment_sentiment", {})
    if not sentiment:
        return (0.0, "")

    engagement = sentiment.get("engagement_signals", {})
    high_topics = engagement.get("high_engagement_topics", [])
    debate_topics = engagement.get("debate_topics", [])

    topic_lower = topic.lower()
    _mwl = _th("matching_min_word_len", 4)
    _mwm = _th("matching_min_word_matches", 2)

    # Check high-engagement topics
    for ht in high_topics:
        ht_words = [w.lower() for w in ht.split() if len(w) >= _mwl]
        matches = [w for w in ht_words if w in topic_lower]
        if len(matches) >= _mwm:
            return (0.07, f"high-engagement topic: {ht[:40]}")

    # Check debate topics (slightly lower boost — controversial but drives watch time)
    for dt in debate_topics:
        dt_words = [w.lower() for w in dt.split() if len(w) >= _mwl]
        matches = [w for w in dt_words if w in topic_lower]
        if len(matches) >= _mwm:
            return (0.05, f"debate topic: {dt[:40]}")

    return (0.0, "")


def _get_demographic_alignment_boost(topic: str, insights: dict) -> tuple[float, str]:
    """
    Boost topics relevant to the channel's primary audience regions.
    E.g., if 60% US/UK audience, Western history gets slight boost.
    Returns (adjustment_value, reason_string).
    """
    demographics = insights.get("audience_demographics", {})
    top_countries = demographics.get("top_countries", [])

    if not top_countries:
        return (0.0, "")

    topic_lower = topic.lower()

    # Region -> topic keyword mapping
    region_keywords = {
        "IN": ["india", "mughal", "maratha", "delhi", "rajput", "ashoka", "maurya",
               "gupta", "chola", "vijayanagara", "bengal", "tamil", "hindu", "sikh"],
        "US": ["america", "washington", "civil war", "native", "wild west", "cia",
               "fbi", "lincoln", "kennedy"],
        "GB": ["britain", "british", "london", "english", "tudor", "victorian",
               "empire", "churchill", "colonial"],
        "PK": ["pakistan", "mughal", "lahore", "indus", "partition"],
    }

    # top_countries is a list of dicts: [{"country": "IN", "pct": 60.0}, ...]
    sorted_countries = sorted(top_countries, key=lambda x: x.get("pct", 0), reverse=True)
    for entry in sorted_countries:
        country_code = entry.get("country", "")
        pct = entry.get("pct", 0)
        if pct < _th("demographic_min_audience_pct", 20):
            continue  # Only boost for significant audience segments
        keywords = region_keywords.get(country_code.upper(), [])
        if any(kw in topic_lower for kw in keywords):
            return (0.03, f"aligns with {country_code} audience ({pct:.0f}%)")

    return (0.0, "")


# ── NEW: Topic quality pre-filter ────────────────────────────────────────────

def _score_topic_quality(topic: str) -> tuple[float, str]:
    """
    Score a topic's inherent quality based on title/content analysis.
    Returns a (multiplier, reason_string) where multiplier is 0.8 to 1.2
    that adjusts the base score.

    Considers:
    - Specificity (names, dates, places)
    - Format (comparison/"vs" topics perform well)
    - Emotional weight (dark, shocking, secret)
    - Focus (too broad is bad, focused is good)
    """
    score = 1.0
    reasons = []
    topic_lower = topic.lower()

    # Specificity: proper nouns / named entities (capitalized multi-letter words)
    proper_nouns = re.findall(r'\b[A-Z][a-z]{2,}\b', topic)
    if len(proper_nouns) >= _th("quality_min_proper_nouns", 2):
        score += _th("quality_proper_noun_bonus", 0.05)
        reasons.append("specific names")
    elif len(proper_nouns) == 0:
        score += _th("quality_no_proper_noun_penalty", -0.05)
        reasons.append("no named entities")

    # Dates/numbers add specificity
    if re.search(r'\b\d{2,4}\b', topic):
        score += _th("quality_date_bonus", 0.03)
        reasons.append("has dates/numbers")

    # Comparison format (high-engagement pattern)
    if " vs " in topic_lower or " versus " in topic_lower:
        score += _th("quality_comparison_bonus", 0.05)
        reasons.append("comparison format")

    # Emotional weight words
    emotional_words = ["secret", "hidden", "dark", "shocking", "forbidden", "deadly",
                       "cursed", "murder", "betrayal", "conspiracy", "poison", "massacre",
                       "brutal", "terrifying", "mysterious", "untold", "banned"]
    emotion_count = sum(1 for w in emotional_words if w in topic_lower)
    if emotion_count >= _th("quality_min_emotional_words", 2):
        score += _th("quality_emotional_high_bonus", 0.05)
        reasons.append("strong emotional weight")
    elif emotion_count == 1:
        score += _th("quality_emotional_low_bonus", 0.02)
        reasons.append("some emotional hook")

    # Drama density: topics with inherent conflict/death/betrayal score higher
    drama_words = ["assassination", "murder", "betrayal", "siege", "war", "plague",
                   "execution", "revolt", "collapse", "famine", "massacre", "duel",
                   "escape", "sacrifice", "madness", "treason", "cannibalism",
                   "cult", "disaster", "extinction", "torture", "mutiny"]
    drama_count = sum(1 for w in drama_words if w in topic_lower)
    if drama_count >= 2:
        score += 0.06
        reasons.append("high drama density")
    elif drama_count == 1:
        score += 0.03
        reasons.append("some drama density")

    # Penalize overly broad topics
    broad_indicators = ["history of", "the story of", "everything about",
                        "complete guide", "the rise and fall of"]
    topic_word_count = len(topic.split())
    if any(phrase in topic_lower for phrase in broad_indicators) and topic_word_count <= 6:
        score += _th("quality_broad_penalty", -0.10)
        reasons.append("too broad")

    # Penalize very short or very long titles
    min_words = _th("quality_min_title_words", 3)
    max_words = _th("quality_max_title_words", 15)
    len_penalty = _th("quality_title_length_penalty", -0.05)
    if topic_word_count < min_words:
        score += len_penalty
        reasons.append("too short")
    elif topic_word_count > max_words:
        score += len_penalty
        reasons.append("too long")

    # Clamp to configurable range
    q_min = _th("quality_multiplier_min", 0.8)
    q_max = _th("quality_multiplier_max", 1.2)
    score = max(q_min, min(q_max, score))
    reason_str = ", ".join(reasons) if reasons else "neutral quality"

    return (round(score, 2), reason_str)


# ── Enhanced experiment strategy ─────────────────────────────────────────────

def _get_experiment_recommendation(insights: dict) -> dict:
    """
    Generate experiment recommendations weighted toward:
    - Underexplored eras (eras with < 3 videos)
    - Content types not yet tried
    Returns dict with 'underexplored_eras' and 'untried_hooks'.
    """
    era_perf = insights.get("era_performance", {})
    exp_recs = insights.get("experiment_recommendations", [])

    # Find underexplored eras
    underexplored_eras = []
    all_known_eras = ["ancient_rome", "ancient_egypt", "ancient_greece",
                      "medieval", "colonial", "indian_history", "modern"]
    for era in all_known_eras:
        era_data = era_perf.get(era, {})
        video_count = era_data.get("video_count", 0)
        if video_count < _th("experiment_underexplored_videos", 3):
            underexplored_eras.append(era.replace("_", " ").title())

    # Detect hook types from top videos to find untried types
    top_videos = insights.get("top_performing_videos", [])
    seen_hook_types = set()
    for v in top_videos:
        title = (v.get("title", "") or "").lower()
        if "vs" in title:
            seen_hook_types.add("comparison")
        if any(w in title for w in ["secret", "hidden"]):
            seen_hook_types.add("mystery")
        if any(w in title for w in ["dark", "deadly"]):
            seen_hook_types.add("dark_reveal")
        if any(w in title for w in ["true story"]):
            seen_hook_types.add("truth_expose")
        if any(w in title for w in ["what if", "could have"]):
            seen_hook_types.add("counterfactual")

    all_hook_types = {"mystery", "bold_claim", "comparison", "dark_reveal",
                      "truth_expose", "counterfactual", "villain_profile"}
    untried_hooks = list(all_hook_types - seen_hook_types)

    return {
        "underexplored_eras": underexplored_eras,
        "untried_hooks": untried_hooks,
        "analytics_recs": exp_recs,
    }


# ── Competitive intelligence scoring signals ─────────────────────────────────

def _get_competitive_gap_boost(topic: str, era: str, competitive_data: dict) -> tuple:
    """Boost topics matching proven competitor content gaps."""
    if not competitive_data or not competitive_data.get("data_available"):
        return 0.0, ""

    gaps = competitive_data.get("gaps", [])
    if not gaps:
        return 0.0, ""

    topic_lower = topic.lower()
    min_views = _th("competitive_gap_min_views", 10000)
    min_matches = _th("competitive_gap_min_word_matches", 2)
    topic_words = set(topic_lower.split())

    best_gap = None
    best_overlap = 0
    best_is_near_copy = False

    for gap in gaps:
        if gap.get("views", 0) < min_views:
            continue
        gap_words = set(gap.get("title", "").lower().split())
        matches = len(topic_words & gap_words)
        if matches >= min_matches and matches > best_overlap:
            total_words = len(topic_words | gap_words)
            overlap_pct = matches / max(total_words, 1)
            is_near_copy = overlap_pct > 0.80
            best_gap = gap
            best_overlap = matches
            best_is_near_copy = is_near_copy

    if not best_gap:
        return 0.0, ""

    # Use performance ratio for scoring, not raw views
    channel = best_gap.get("channel", "")
    channel_avg = competitive_data.get("channel_avg_views", {}).get(channel, 1)
    ratio = best_gap.get("views", 0) / max(channel_avg, 1)

    # Scale boost: ratio 1.0 = small, ratio 3.0+ = full boost
    boost = min(1.0, ratio / 3.0)

    # Reduce boost for near-copies (me-too content risk)
    if best_is_near_copy:
        boost *= 0.5

    return round(boost, 3), f"gap: {best_gap.get('title', '')[:40]} ({best_gap.get('views',0):,} views, {channel})"


def _get_competitor_trending_boost(topic: str, competitive_data: dict) -> tuple:
    """Boost topics matching currently trending competitor videos."""
    if not competitive_data or not competitive_data.get("data_available"):
        return 0.0, ""

    trending = competitive_data.get("trending", [])
    if not trending:
        return 0.0, ""

    topic_lower = topic.lower()
    min_matches = _th("competitive_gap_min_word_matches", 2)
    topic_words = set(topic_lower.split())

    for t in trending:
        t_words = set(t.get("title", "").lower().split())
        matches = len(topic_words & t_words)
        if matches >= min_matches:
            ratio = t.get("performance_ratio", 1.0)
            # Scale: ratio 1.5 = small boost, ratio 5.0+ = full
            boost = min(1.0, (ratio - 1.0) / 4.0)
            return round(boost, 3), f"trending: {t.get('title','')[:40]} ({ratio:.1f}x avg, {t.get('channel','')})"

    return 0.0, ""


def _get_niche_saturation_penalty(topic: str, competitive_data: dict) -> tuple:
    """Penalize topics covered by 3+ competitors (oversaturated niche)."""
    if not competitive_data or not competitive_data.get("data_available"):
        return 0.0, ""

    saturated = competitive_data.get("saturated", [])
    if not saturated:
        return 0.0, ""

    topic_lower = topic.lower()
    topic_words = set(topic_lower.split())
    min_matches = _th("niche_saturation_min_word_matches", 2)
    min_competitors = _th("niche_saturation_min_competitors", 3)

    for entry in saturated:
        if entry.get("count", 0) < min_competitors:
            continue
        sat_words = entry.get("words", [])
        if isinstance(sat_words, (list, tuple)):
            sat_words = set(sat_words)
        matches = len(topic_words & sat_words)
        if matches >= min_matches:
            # Stronger penalty for more competitors
            count = entry.get("count", 3)
            penalty = -min(1.0, (count - 2) / 5.0)  # 3 competitors = -0.2, 7+ = -1.0
            channels_str = ", ".join(entry.get("channels", [])[:3])
            return round(penalty, 3), f"saturated: {count} competitors ({channels_str})"

    return 0.0, ""


# ── Score adjustment engine ──────────────────────────────────────────────────

def _apply_score_adjustments(topics: list[dict], recent_eras: list[str],
                              audience_requests: list[str],
                              trending_topics: list[str],
                              cost_efficient_eras: dict[str, float],
                              shorts_era_boosts: dict[str, float] = None,
                              insights: dict = None,
                              competitive_data: dict = None) -> list[dict]:
    """
    Post-process Claude's scored list with data-driven adjustments.
    Uses dynamic thresholds based on channel maturity.
    Modifies scores in-place and returns the list.
    """
    if insights is None:
        insights = load_insights()

    video_count = _get_video_count()
    adj_magnitudes = _get_dynamic_adjustments(video_count)
    confidence = insights.get("data_quality", {}).get("confidence_level", "none")

    print(f"  [scoring] Channel maturity: {video_count} videos — "
          f"using {'early' if video_count < 5 else 'growing' if video_count < SCORING_CONFIG.get('maturity_threshold', 15) else 'mature'} adjustments")

    # Determine which eras were overrepresented (last 2 videos same era)
    penalized_eras = set()
    if len(recent_eras) >= 2 and recent_eras[0] == recent_eras[1]:
        penalized_eras.add(recent_eras[0])
        print(f"  [era rotation] Penalizing '{recent_eras[0]}' era — last 2 videos used it")

    max_score = SCORING_CONFIG.get("max_score", 1.0)
    min_score = SCORING_CONFIG.get("min_score", 0.0)

    for t in topics:
        original_score = float(t.get("score", SCORING_CONFIG.get("default_topic_score", 0.5)))
        adjustments = []
        topic_text = t.get("topic", "").lower()
        topic_raw = t.get("topic", "")
        topic_era = t.get("era", "").lower()

        # 0. Topic quality pre-filter (multiplier on base score)
        quality_mult, quality_reason = _score_topic_quality(topic_raw)
        if quality_mult != 1.0:
            quality_adj = original_score * (quality_mult - 1.0)
            adjustments.append(("quality", round(quality_adj, 3), quality_reason))

        # 1. Era rotation penalty
        if topic_era in penalized_eras or any(era in topic_era for era in penalized_eras):
            mag = adj_magnitudes.get("era_fatigue", -0.20)
            adjustments.append(("era_fatigue", mag, f"last 2 videos were {topic_era}"))

        # 2. Audience request boost
        # Require min_word_matches overlap OR one long word to avoid false positives
        _mwl = _th("matching_min_word_len", 4)
        _mwm = _th("matching_min_word_matches", 2)
        _mlw = _th("matching_long_word_len", 6)
        for req in audience_requests:
            req_words = [w for w in req.split() if len(w) > _mwl - 1]
            matches = [w for w in req_words if w in topic_text]
            if len(matches) >= _mwm or any(len(w) >= _mlw and w in topic_text for w in req_words):
                mag = adj_magnitudes.get("audience_request", +0.15)
                adjustments.append(("audience_request", mag, f"matches: {req[:40]}"))
                break

        # 3. Trending boost
        # Same stricter matching — require min_word_matches or one long word
        for trend in trending_topics:
            trend_words = [w for w in trend.split() if len(w) > _mwl - 1]
            matches = [w for w in trend_words if w in topic_text]
            if len(matches) >= _mwm or any(len(w) >= _mlw and w in topic_text for w in trend_words):
                mag = adj_magnitudes.get("trending", +0.10)
                adjustments.append(("trending", mag, f"trend: {trend[:30]}"))
                break

        # 4. Cost-efficiency boost
        for era_key, bonus in cost_efficient_eras.items():
            if era_key in topic_era or topic_era in era_key:
                mag = adj_magnitudes.get("cost_efficiency", bonus)
                adjustments.append(("cost_efficiency", mag, f"{era_key} era efficient"))
                break

        # 5. Shorts subscriber boost
        if shorts_era_boosts:
            for era_key, bonus in shorts_era_boosts.items():
                if era_key in topic_era or topic_era in era_key:
                    mag = adj_magnitudes.get("shorts_subs", bonus)
                    adjustments.append(("shorts_subs", mag, f"{era_key} shorts drive subs"))
                    break

        # 6. NEW: Subscriber conversion boost
        if confidence != "none":
            sub_adj, sub_reason = _get_subscriber_conversion_boost(topic_text, topic_era, insights)
            if sub_adj != 0:
                scaled = adj_magnitudes.get("subscriber_conversion", sub_adj)
                # Use the direction from the function but magnitude from config
                final = scaled if sub_adj > 0 else -abs(scaled)
                adjustments.append(("subscriber_conversion", final, sub_reason))

        # 7. NEW: Engagement boost
        if confidence != "none":
            eng_adj, eng_reason = _get_engagement_boost(topic_text, topic_era, insights)
            if eng_adj != 0:
                scaled = adj_magnitudes.get("engagement", eng_adj)
                adjustments.append(("engagement", scaled, eng_reason))

        # 8. NEW: Search demand boost
        if confidence != "none":
            search_adj, search_reason = _get_search_demand_boost(topic_raw, insights)
            if search_adj != 0:
                scaled = adj_magnitudes.get("search_demand", search_adj)
                adjustments.append(("search_demand", scaled, search_reason))

        # 9. NEW: Traffic source adjustment
        if confidence != "none":
            traffic_adj, traffic_reason = _get_traffic_source_adjustment(topic_raw, topic_era, insights)
            if traffic_adj != 0:
                scaled = adj_magnitudes.get("traffic_source", traffic_adj)
                adjustments.append(("traffic_source", scaled, traffic_reason))

        # 10. NEW: Content pattern boost
        if confidence != "none":
            pattern_adj, pattern_reason = _get_content_pattern_boost(topic_raw, insights)
            if pattern_adj != 0:
                scaled = adj_magnitudes.get("content_pattern", pattern_adj)
                adjustments.append(("content_pattern", scaled, pattern_reason))

        # 11. NEW: Demographic alignment boost
        if confidence != "none":
            demo_adj, demo_reason = _get_demographic_alignment_boost(topic_raw, insights)
            if demo_adj != 0:
                scaled = adj_magnitudes.get("demographic_alignment", demo_adj)
                adjustments.append(("demographic_alignment", scaled, demo_reason))

        # 12. NEW: Shorts→long correlation boost
        if confidence != "none":
            corr_adj, corr_reason = _get_shorts_long_correlation_boost(topic_raw, topic_era, insights)
            if corr_adj != 0:
                scaled = adj_magnitudes.get("shorts_correlation", corr_adj)
                adjustments.append(("shorts_correlation", scaled, corr_reason))

        # 13. NEW: Comment sentiment boost (high-engagement / debate topics)
        if confidence != "none":
            sent_adj, sent_reason = _get_sentiment_boost(topic_raw, insights)
            if sent_adj != 0:
                scaled = adj_magnitudes.get("sentiment", sent_adj)
                adjustments.append(("sentiment", scaled, sent_reason))

        # 14-16: Competitive intelligence signals (NOT gated on confidence — use external data)
        if competitive_data and competitive_data.get("data_available"):
            comp_adjustments = []

            # 14. Competitive gap boost
            gap_adj, gap_reason = _get_competitive_gap_boost(topic_raw, topic_era, competitive_data)
            if gap_adj != 0:
                scaled = adj_magnitudes.get("competitive_gap", gap_adj)
                comp_adjustments.append(("competitive_gap", scaled if gap_adj > 0 else -abs(scaled), gap_reason))

            # 15. Competitor trending boost
            trend_adj, trend_reason = _get_competitor_trending_boost(topic_raw, competitive_data)
            if trend_adj != 0:
                scaled = adj_magnitudes.get("competitor_trending", trend_adj)
                comp_adjustments.append(("competitor_trending", scaled, trend_reason))

            # 16. Niche saturation penalty
            sat_adj, sat_reason = _get_niche_saturation_penalty(topic_raw, competitive_data)
            if sat_adj != 0:
                scaled = adj_magnitudes.get("niche_saturation", sat_adj)
                comp_adjustments.append(("niche_saturation", -abs(scaled), sat_reason))

            # Cap total competitive contribution per topic
            max_comp = _th("competitive_max_total_boost", 0.15)
            comp_total = sum(adj for _, adj, _ in comp_adjustments)
            if comp_total > max_comp:
                scale_factor = max_comp / comp_total if comp_total > 0 else 1.0
                comp_adjustments = [(n, round(a * scale_factor, 3), r) for n, a, r in comp_adjustments]

            adjustments.extend(comp_adjustments)

        # Apply adjustments
        if adjustments:
            total_adj = sum(adj for _, adj, _ in adjustments)
            new_score = max(min_score, min(max_score, original_score + total_adj))
            t["score"] = round(new_score, 2)

            # Enhanced logging: show signal name, value, and reason
            adj_parts = []
            for name, val, reason in adjustments:
                adj_parts.append(f"{name}:{val:+.2f}")
            adj_str = ", ".join(adj_parts)

            # Log reasons on separate line for readability when many signals fire
            topic_display = t.get("topic", "")[:50]
            print(f"  [scoring] {topic_display}... {original_score:.2f} -> {new_score:.2f} ({adj_str})")
            if len(adjustments) >= 3:
                reason_parts = [f"    {name}: {reason}" for name, _, reason in adjustments if reason]
                if reason_parts:
                    print(f"  [scoring]   signals ({len(adjustments)}):")
                    for part in reason_parts:
                        print(f"  [scoring] {part}")

    return topics


# ── Main run function ────────────────────────────────────────────────────────

def run(is_experiment: bool = False):
    print("[Topic Discovery] Finding new topics...")
    done_list = get_all_topics_done()
    done = set(done_list)
    print(f"[Topic Discovery] Skipping {len(done)} already-done topics")
    done_str = "\n".join(f"- {t}" for t in done_list[:50]) if done else "None yet"

    # ── Dynamic topic count based on queue depth ──
    queue_depth = _get_queue_depth()
    max_topics = SCORING_CONFIG.get("max_topics_per_discovery", 20)
    q_low = _th("queue_low_threshold", 5)
    q_high = _th("queue_high_threshold", 15)
    q_min_topics = _th("queue_minimum_topics", 10)
    if queue_depth < q_low:
        topic_count = max_topics
    elif queue_depth > q_high:
        topic_count = max(q_min_topics, int(max_topics * _th("queue_high_ratio", 0.50)))
    else:
        topic_count = max(q_min_topics, int(max_topics * _th("queue_medium_ratio", 0.75)))
    print(f"[Topic Discovery] Queue has {queue_depth} topics — generating {topic_count}")

    # ── Gather intelligence signals ──
    insights = load_insights()
    recent_eras = _get_recent_video_eras(weeks=4)
    audience_requests = _get_audience_requests()
    trending_topics = _get_trending_history_topics()
    cost_efficient_eras = _get_cost_efficient_eras()
    shorts_era_boosts = _get_shorts_era_boosts()

    # Load competitive intelligence
    competitive_data = None
    try:
        from intel.competitive_intel import get_competitive_signals, get_competitor_summary
        done_list_for_gaps = list(done)
        competitive_data = get_competitive_signals(our_topics=done_list_for_gaps)
        if competitive_data and competitive_data.get("data_available"):
            print(f"[Topic Discovery] Competitive intel loaded: {len(competitive_data.get('gaps', []))} gaps, "
                  f"{len(competitive_data.get('trending', []))} trending")
    except Exception as e:
        print(f"[Topic Discovery] Competitive intel not available: {e}")

    if audience_requests:
        print(f"[Topic Discovery] Found {len(audience_requests)} audience requests")
    if recent_eras:
        print(f"[Topic Discovery] Recent eras: {', '.join(recent_eras[:5])}")

    # ── Smarter experiment gating ──
    exp_recommendation = _get_experiment_recommendation(insights)
    if is_experiment and not _should_experiment():
        print("[Topic Discovery] Skipping experiment — last experiment underperformed")
        is_experiment = False

    # Merge channel insights (data-backed) with legacy guidance
    era_intel = get_topic_discovery_intelligence()
    guidance  = era_intel or get_agent_guidance("agent_00")
    system_str = "You are a YouTube content strategist. Return only valid JSON."
    if guidance:
        system_str += f"\n\nANALYTICS GUIDANCE:\n{guidance}"

    # Inject competitive intelligence into Claude's prompt
    try:
        comp_summary = get_competitor_summary(max_chars=2000)
        if comp_summary:
            system_str += f"\n\n{comp_summary}"
    except Exception:
        pass

    experiment_note = ""
    if is_experiment:
        # Enhanced experiment note with specific recommendations
        exp_parts = [
            "\n\nNOTE: The next video is an EXPERIMENT. Include at least one topic that "
            "deliberately breaks a standard DNA rule (e.g. different tone, non-dark angle, modern era). "
            "Flag it with score 0.95 and set reason to 'experiment'."
        ]
        if exp_recommendation.get("underexplored_eras"):
            eras_str = ", ".join(exp_recommendation["underexplored_eras"][:3])
            exp_parts.append(f"\nUNDEREXPLORED ERAS (< 3 videos, try these): {eras_str}")
        if exp_recommendation.get("untried_hooks"):
            hooks_str = ", ".join(exp_recommendation["untried_hooks"][:3])
            exp_parts.append(f"\nUNTRIED HOOK TYPES (experiment with these): {hooks_str}")
        experiment_note = "".join(exp_parts)

    # ── Inject content opportunities from comment sentiment ──
    content_opp_note = ""
    sentiment = insights.get("comment_sentiment", {})
    content_opps = sentiment.get("content_opportunities", [])
    if content_opps:
        opps_str = "\n".join(f"  - {o}" for o in content_opps[:5])
        content_opp_note = (
            f"\n\nAUDIENCE-DERIVED CONTENT OPPORTUNITIES (from comment analysis):\n{opps_str}\n"
            "Consider generating topics that address these latent audience interests."
        )

    # ── Inject trending context into the prompt ──
    trending_note = ""
    if trending_topics:
        trending_str = ", ".join(trending_topics[:10])
        trending_note = (
            f"\n\nCURRENTLY TRENDING HISTORY TOPICS (consider aligning some topics with these): "
            f"{trending_str}"
        )

    response = call_agent(
        "00_topic_discovery",
        system_prompt=system_str,
        user_prompt=PROMPT.format(
            done_topics=done_str,
            topic_count=topic_count,
        ) + experiment_note + trending_note + content_opp_note,
        max_tokens=4000,
        expect_json=False,
        stage_num=0,
    )

    text = response.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        topics = json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        print(f"[Topic Discovery] JSON parse error: {e}")
        print(f"[Topic Discovery] Raw response: {text[:500]}")
        return {"topics_added": 0, "topics": []}
    print(f"[Topic Discovery] Found {len(topics)} candidates — applying score adjustments...")

    # ── Post-processing: apply all score adjustments ──
    topics = _apply_score_adjustments(
        topics,
        recent_eras=recent_eras,
        audience_requests=audience_requests,
        trending_topics=trending_topics,
        cost_efficient_eras=cost_efficient_eras,
        shorts_era_boosts=shorts_era_boosts,
        insights=insights,
        competitive_data=competitive_data,
    )

    added = 0
    for t in sorted(topics, key=lambda x: x.get("score",0), reverse=True):
        topic = t.get("topic","").strip()
        try:
            score = float(t.get("score", SCORING_CONFIG.get("default_topic_score", 0.5)))
        except (TypeError, ValueError):
            score = SCORING_CONFIG.get("default_topic_score", 0.5)
        if not topic:
            continue
        if topic.lower() in done:
            continue
        add_topic(topic, source="auto_discovery", score=score)
        print(f"  [{score:.2f}] {topic} — {t.get('hook','')}")
        added += 1

    print(f"[Topic Discovery] Added {added} topics to queue")
    return {"topics_added": added, "topics": topics}

if __name__ == "__main__":
    run()
