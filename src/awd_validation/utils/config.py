"""
Configuration loader for AWD Validation Pipeline.
"""
from pathlib import Path
from typing import Optional
import yaml
from loguru import logger


def get_utm_epsg(longitude: float, latitude: float) -> str:
    """Return the EPSG string for the UTM zone that contains (longitude, latitude).

    Never hardcode a UTM zone — always compute from the actual data centroid.
    """
    zone = int((longitude + 180) / 6) + 1
    base = 32600 if latitude >= 0 else 32700
    return f"EPSG:{base + zone}"


def compute_working_epsg(gdf, fallback_epsg: int = 32644) -> int:
    """Derive the correct UTM EPSG from a GeoDataFrame's centroid.

    The GDF is expected to be in WGS84 (EPSG:4326) on entry.
    Returns an integer EPSG code.
    """
    import geopandas as gpd

    wgs = gdf if (gdf.crs is None or gdf.crs.to_epsg() == 4326) else gdf.to_crs(epsg=4326)
    valid = wgs[wgs.geometry.notna() & ~wgs.geometry.is_empty]
    if len(valid) == 0:
        logger.warning(
            "compute_working_epsg: no valid geometries — using fallback EPSG:{}", fallback_epsg
        )
        return fallback_epsg
    # Use bounding-box midpoint to avoid centroid-on-geographic-CRS warnings.
    # Accuracy is sufficient: we only need to know which 6° UTM zone to use.
    bounds = valid.geometry.total_bounds  # [minx, miny, maxx, maxy]
    mean_lon = (float(bounds[0]) + float(bounds[2])) / 2
    mean_lat = (float(bounds[1]) + float(bounds[3])) / 2
    epsg_str = get_utm_epsg(mean_lon, mean_lat)
    epsg_int = int(epsg_str.split(":")[1])
    logger.debug("Dynamic UTM EPSG: {} (centroid {:.3f}°E, {:.3f}°N)", epsg_int, mean_lon, mean_lat)
    return epsg_int


class Config:
    """Loads and provides access to settings.yaml configuration."""

    def __init__(self, config_path: str = "/app/config/settings.yaml"):
        self.config_path = Path(config_path)
        self._config = self._load()
        logger.debug(f"Configuration loaded from {self.config_path}")

    def _load(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self.config_path}"
            )
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    def get(self, *keys, default=None):
        """
        Get nested config value using dot-notation keys.
        Example: config.get('crs', 'working_epsg')
        """
        value = self._config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default
        return value

    @property
    def project(self) -> dict:
        return self._config.get("project", {})

    @property
    def input(self) -> dict:
        return self._config.get("input", {})

    @property
    def crs(self) -> dict:
        return self._config.get("crs", {})

    @property
    def schema(self) -> dict:
        return self._config.get("schema", {})

    @property
    def thresholds(self) -> dict:
        return self._config.get("thresholds", {})

    @property
    def checks(self) -> dict:
        return self._config.get("checks", {})

    @property
    def database(self) -> dict:
        return self._config.get("database", {})
