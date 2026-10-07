"""Configuration for Favorite Hunter.

Every threshold the strategy depends on lives here so it can be tuned from
``config.yaml`` without touching code. Secrets (alert tokens, API keys) are
read from environment variables only and never stored in the YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

DEFAULT_CONFIG_PATHS = ("config.yaml", "config.yml")


class ScannerConfig(BaseModel):
    # Executable-price band for a "favorite". Both ends are inclusive.
    price_min: float = 0.80
    price_max: float = 0.98
    # Gamma's cached bestBid/bestAsk is only used to decide which order books to
    # fetch; the pads keep markets whose cached quote is slightly stale. The
    # upper pad is small because thousands of near-resolved markets sit at 0.99+.
    prefilter_pad_low: float = 0.03
    prefilter_pad_high: float = 0.01
    # Only consider markets resolving within this many hours (None = no limit).
    max_hours_to_end: float | None = 24 * 30
    # Keep markets whose scheduled end has passed but that are still trading.
    include_past_end: bool = True
    min_gamma_liquidity: float = 0.0
    min_gamma_volume: float = 0.0
    page_size: int = 500
    max_markets: int = 50_000
    book_batch_size: int = 50
    # Ask-side depth (USDC) is measured within this distance of the best ask.
    depth_window: float = 0.02
    # The displayed purchase price is the VWAP of buying this many USDC.
    reference_stake_usd: float = 100.0

    @model_validator(mode="after")
    def _check_band(self) -> "ScannerConfig":
        if not 0 < self.price_min < self.price_max < 1:
            raise ValueError("scanner.price_min/price_max must satisfy 0 < min < max < 1")
        return self


class FilterConfig(BaseModel):
    min_edge: float = 0.04  # estimated probability - effective cost (fees included)
    min_roi: float = 0.03
    min_liquidity_usd: float = 250.0  # ask depth inside scanner.depth_window
    max_spread: float = 0.03
    max_book_age_seconds: float = 120.0
    max_source_age_seconds: float = 300.0
    min_confidence: float = 60.0
    min_rule_clarity: float = 40.0
    conflict_threshold: float = 0.05  # max disagreement between independent estimates
    skip_conflicting_sources: bool = True
    require_accepting_orders: bool = True
    # Probability edge must also be positive at the lower end of the estimate's
    # uncertainty band (estimate - z * uncertainty).
    require_positive_lower_bound_edge: bool = True
    lower_bound_z: float = 1.0


class FeeConfig(BaseModel):
    # Used only when a market's fee schedule cannot be fetched. The highest
    # published category rate is assumed so that unknown fees never inflate EV.
    unknown_fee_rate: float = 0.07
    unknown_fee_exponent: float = 1.0


class PaperConfig(BaseModel):
    enabled: bool = True
    starting_bankroll: float = 10_000.0
    sizing: Literal["fixed", "kelly"] = "fixed"
    fixed_stake_usd: float = 100.0
    kelly_multiplier: float = 0.10  # fraction of full Kelly
    max_stake_usd: float = 250.0
    min_stake_usd: float = 5.0
    max_open_positions: int = 50
    max_exposure_per_event_usd: float = 500.0
    max_total_exposure_pct: float = 0.60
    allow_reentry: bool = False
    # Record every in-band favorite (not traded) as a baseline so the model's
    # picks can be compared against blindly buying favorites.
    record_baseline: bool = True
    baseline_stake_usd: float = 100.0
    # How often open positions are checked for resolution.
    settle_interval_seconds: float = 300.0


class ScoreWeights(BaseModel):
    """Favorite Edge Score weights (normalised to 100 at runtime)."""

    probability_edge: float = 30
    source_reliability: float = 20
    time_remaining: float = 15
    liquidity: float = 15
    spread: float = 10
    event_certainty: float = 10


class ScoringConfig(BaseModel):
    # Probability edge (after fees) that earns the full edge component.
    edge_full_marks: float = 0.10


class ConfidenceWeights(BaseModel):
    """Confidence score weights (normalised to 100 at runtime)."""

    model_edge: float = 18
    source_quality: float = 15
    independent_sources: float = 10
    time_remaining: float = 8
    event_uncertainty: float = 15
    liquidity: float = 8
    spread: float = 6
    depth: float = 5
    rule_clarity: float = 15


class AlertConfig(BaseModel):
    enabled: bool = False
    telegram: bool = False
    discord: bool = False
    min_favorite_edge_score: float = 65.0
    min_confidence: float = 70.0
    # Re-alert on an already-alerted market/side only if edge improved this much.
    realert_edge_improvement: float = 0.02
    cooldown_minutes: float = 60.0


class SourcesConfig(BaseModel):
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    binance_url: str = "https://data-api.binance.vision"
    coinbase_url: str = "https://api.exchange.coinbase.com"
    kraken_url: str = "https://api.kraken.com"
    deribit_url: str = "https://www.deribit.com"
    espn_url: str = "https://site.api.espn.com"
    odds_api_url: str = "https://api.the-odds-api.com"
    kalshi_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    request_timeout: float = 15.0
    max_retries: int = 3
    user_agent: str = "favorite-hunter/0.1 (paper trading research)"
    # Requests per second per host (token bucket).
    rate_limits: dict[str, float] = Field(
        default_factory=lambda: {
            "gamma-api.polymarket.com": 10.0,
            "clob.polymarket.com": 20.0,
            "data-api.polymarket.com": 10.0,
            "default": 5.0,
        }
    )
    # Optional file with user-supplied, sourced evidence (polls, vote counts).
    manual_evidence_path: str = "manual_evidence.yaml"


class Settings(BaseModel):
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    score_weights: ScoreWeights = Field(default_factory=ScoreWeights)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    database_path: str = "data/favorite_hunter.db"
    loop_interval_seconds: float = 60.0
    # Paper trading only. Real-money execution is intentionally not implemented.
    mode: Literal["paper"] = "paper"

    # Secrets: environment only.
    @property
    def telegram_bot_token(self) -> str | None:
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    @property
    def telegram_chat_id(self) -> str | None:
        return os.environ.get("TELEGRAM_CHAT_ID") or None

    @property
    def discord_webhook_url(self) -> str | None:
        return os.environ.get("DISCORD_WEBHOOK_URL") or None

    @property
    def odds_api_key(self) -> str | None:
        return os.environ.get("ODDS_API_KEY") or None


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from YAML (if present) on top of the defaults."""
    candidates = [Path(path)] if path else [Path(p) for p in DEFAULT_CONFIG_PATHS]
    for candidate in candidates:
        if candidate.is_file():
            raw = yaml.safe_load(candidate.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"{candidate} must contain a YAML mapping")
            return Settings.model_validate(raw)
        if path:
            raise FileNotFoundError(f"config file not found: {candidate}")
    return Settings()
