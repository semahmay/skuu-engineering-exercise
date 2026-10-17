"""Cross-shop product matching and dashboard metrics."""

from app.matching.matcher import (
    ALGORITHM_VERSION,
    Cluster,
    Metrics,
    NormalizedSKU,
    build_clusters,
    build_colour_vocabulary,
    colour_compatible,
    compute_metrics,
    matched_group_samples,
    normalize_sku,
    normalized_skus,
    skus_match,
)
from app.matching.normalization import derive_colour_from_handle
from app.matching.service import (
    get_matched_groups,
    get_metrics,
    load_normalized_skus,
)

__all__ = [
    "ALGORITHM_VERSION",
    "Cluster",
    "Metrics",
    "NormalizedSKU",
    "build_clusters",
    "build_colour_vocabulary",
    "colour_compatible",
    "compute_metrics",
    "derive_colour_from_handle",
    "matched_group_samples",
    "normalize_sku",
    "normalized_skus",
    "skus_match",
    "get_matched_groups",
    "get_metrics",
    "load_normalized_skus",
]
