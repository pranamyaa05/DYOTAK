"""DYOTAK Configuration loader and settings schema.

Loads config/default.yaml into typed Pydantic models. Fails startup on unknown keys.
Permits deployment overrides via DYOTAK_* environment variables.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.common.cache import compute_config_hash


class AOIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_side_km: float
    max_area_km2: float


class PairingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_repeat_days: int
    search_window_days: int
    max_pre_scenes: int
    allow_degraded_offsets: bool


class FetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution_m: float
    timeout_seconds: float
    retries: int
    backoff_factor: float


class FloodConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probability_threshold: float
    tile_size: int
    overlap_pixels: int
    speckle_filter_size: int


class TerrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slope_cutoff_degrees: float
    hand_cutoff_m: float


class OpticalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cloud_limit_pct: float
    mndwi_threshold: float


class OSMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_policy: str
    feature_filters: List[str]
    # ohsome endpoint + transport tunables (added with pipeline/ohsome.py).
    # v2 is the supported extraction API (v1 geometry is forbidden and v1 is
    # scheduled for shutdown on 2026-11-30).
    backend: str = "v2"
    v2_base_url: str = "https://api.heigit.org/ohsome-api/v2-rc"
    base_url: str = "https://api.ohsome.org/v1"
    request_timeout_seconds: float = 60.0
    retries: int = 3


class DamageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affected_overlap_fraction: float
    possibly_affected_overlap_fraction: float
    bridge_buffer_m: float


class IsolationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flood_threshold_edge: float
    settlement_snap_distance_m: float
    facility_tags: List[str]
    place_tags: List[str]


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accumulation_threshold: int
    hand_corridor_m: float


class LandingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_slope_degrees: float
    min_area_m2: float
    max_distance_from_settlement_m: float


class CopilotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    max_tokens: int
    template_fallback_enabled: bool


class JobsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_jobs: int
    stage_timeout_seconds: float
    queue_limit: int


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_dir: str
    ttl_days: int


class AppConfig(BaseModel):
    """Full hierarchical configuration matching config/default.yaml."""
    model_config = ConfigDict(extra="forbid")

    aoi: AOIConfig
    pairing: PairingConfig
    fetch: FetchConfig
    flood: FloodConfig
    terrain: TerrainConfig
    optical: OpticalConfig
    osm: OSMConfig
    damage: DamageConfig
    isolation: IsolationConfig
    upstream: UpstreamConfig
    landing: LandingConfig
    copilot: CopilotConfig
    jobs: JobsConfig
    cache: CacheConfig


class Settings(BaseSettings):
    """Runtime environment settings."""
    model_config = SettingsConfigDict(
        env_prefix="DYOTAK_",
        env_file=".env",
        extra="ignore"
    )

    app_name: str = "DYOTAK"
    app_version: str = "0.1.0"
    mock_mode: bool = False
    config_path: Path = Path("config/default.yaml")

    # Secrets
    cdse_client_id: Optional[str] = None
    cdse_client_secret: Optional[str] = None
    ohsome_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None


def load_app_config(config_file: Optional[Path] = None) -> AppConfig:
    """Load and validate config/default.yaml."""
    path = config_file or Path("config/default.yaml")
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found at {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return AppConfig.model_validate(data)


# Global singletons
settings = Settings()
app_config = load_app_config()
CONFIG_HASH = compute_config_hash(app_config.model_dump())
