"""Market category detection from Gamma tags, categories and question text."""

from __future__ import annotations

import re

from .models import Market

CRYPTO = "crypto"
SPORTS = "sports"
POLITICS = "politics"
ECONOMICS = "economics"
FINANCE = "finance"
WEATHER = "weather"
CULTURE = "culture"
TECH = "tech"
MENTIONS = "mentions"
OTHER = "other"

CATEGORIES = (CRYPTO, SPORTS, POLITICS, ECONOMICS, FINANCE, WEATHER, CULTURE, TECH, MENTIONS, OTHER)

_SPORTS_TAGS = {
    "sports", "nba", "nfl", "mlb", "nhl", "wnba", "soccer", "football", "epl", "premier-league",
    "ucl", "champions-league", "la-liga", "serie-a", "bundesliga", "ligue-1", "mls", "uefa",
    "tennis", "atp", "wta", "golf", "pga", "ufc", "mma", "boxing", "f1", "formula-1", "nascar",
    "cricket", "ipl", "ncaa", "cfb", "cbb", "ncaab", "ncaaf", "college-football",
    "college-basketball", "esports", "cs2", "counter-strike", "league-of-legends", "lol",
    "dota", "dota-2", "valorant", "rugby", "games", "world-cup", "olympics", "baseball",
    "basketball", "hockey", "kbo", "npb", "afl", "euroleague",
}
_CRYPTO_TAGS = {
    "crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple", "dogecoin",
    "doge", "crypto-prices", "cryptocurrency", "memecoins", "stablecoins", "hyperliquid", "bnb",
}
_POLITICS_TAGS = {
    "politics", "elections", "us-politics", "us-elections", "world-elections", "geopolitics",
    "trump", "congress", "senate", "house", "president", "presidential", "primaries", "polls",
    "global-elections", "uk-politics", "france", "germany", "government", "supreme-court",
    "white-house", "democrats", "republicans", "mayor", "governor",
}
_ECONOMICS_TAGS = {
    "economy", "economics", "fed", "fed-rates", "interest-rates", "inflation", "cpi", "gdp",
    "jobs", "unemployment", "recession", "fomc", "tariffs", "macro",
}
_FINANCE_TAGS = {
    "finance", "stocks", "equities", "earnings", "s&p-500", "sp500", "nasdaq", "dow", "ipos",
    "ipo", "commodities", "gold", "oil", "forex", "indices", "business", "companies",
}
_WEATHER_TAGS = {"weather", "temperature", "climate", "hurricanes", "hurricane", "snow", "rain"}
_CULTURE_TAGS = {
    "culture", "pop-culture", "movies", "music", "awards", "oscars", "grammys", "celebrities",
    "entertainment", "tv", "box-office", "twitter", "tweets", "youtube", "tiktok", "spotify",
}
_TECH_TAGS = {"tech", "ai", "openai", "science", "space", "spacex", "apple", "google", "chatgpt"}
_MENTIONS_TAGS = {"mentions", "mention-markets", "what-will-say"}

_CRYPTO_WORDS = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|xrp|dogecoin|bnb|cardano|litecoin|crypto|hyperliquid)\b",
    re.IGNORECASE,
)
_SPORTS_WORDS = re.compile(
    r"\b(vs\.?|win the|match|game \d|series|championship|super bowl|world series|stanley cup|"
    r"nba finals|grand slam|playoffs?|qualif|relegat|goalscorer|touchdown|quarterback)\b",
    re.IGNORECASE,
)
_POLITICS_WORDS = re.compile(
    r"\b(election|elected|president|senate|governor|mayor|prime minister|parliament|primary|"
    r"nominee|nomination|congress|vote share|electoral|referendum|impeach|cabinet|poll)\b",
    re.IGNORECASE,
)
_ECONOMICS_WORDS = re.compile(
    r"\b(fed|fomc|interest rates?|rate cut|rate hike|cpi|inflation|gdp|unemployment|jobs report|"
    r"nonfarm|payrolls|recession)\b",
    re.IGNORECASE,
)
_WEATHER_WORDS = re.compile(r"\b(temperature|°f|°c|hurricane|rainfall|snowfall|weather)\b", re.IGNORECASE)
_MENTIONS_WORDS = re.compile(r"\b(say|mention|tweet)\b.*\b(times?|during|at least)\b", re.IGNORECASE)


def detect_category(market: Market) -> str:
    tags = set(market.all_tags())
    raw_categories = {
        (c or "").strip().lower() for c in [market.category_raw, *(e.category for e in market.events)] if c
    }
    if market.game_id or market.sports_market_type or tags & _SPORTS_TAGS or "sports" in raw_categories:
        return SPORTS
    if tags & _MENTIONS_TAGS:
        return MENTIONS
    if tags & _CRYPTO_TAGS or "crypto" in raw_categories:
        return CRYPTO
    if tags & _ECONOMICS_TAGS or raw_categories & {"economics", "economy"}:
        return ECONOMICS
    if tags & _POLITICS_TAGS or raw_categories & {"politics", "elections", "us-current-affairs"}:
        return POLITICS
    if tags & _FINANCE_TAGS or raw_categories & {"finance", "business"}:
        return FINANCE
    if tags & _WEATHER_TAGS or "weather" in raw_categories:
        return WEATHER
    if tags & _TECH_TAGS or raw_categories & {"tech", "science"}:
        return TECH
    if tags & _CULTURE_TAGS or raw_categories & {"pop-culture", "culture", "entertainment"}:
        return CULTURE

    text = " ".join(filter(None, [market.question, market.event_title or ""]))
    if _CRYPTO_WORDS.search(text):
        return CRYPTO
    if _ECONOMICS_WORDS.search(text):
        return ECONOMICS
    if _POLITICS_WORDS.search(text):
        return POLITICS
    if _WEATHER_WORDS.search(text):
        return WEATHER
    if _MENTIONS_WORDS.search(text):
        return MENTIONS
    if _SPORTS_WORDS.search(text):
        return SPORTS
    return OTHER
