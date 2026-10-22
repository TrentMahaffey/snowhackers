"""Dashboard views, split by area (2026-10-21). urls.py uses these names via `views.<name>`."""

from .forecast import forecast_page  # noqa: F401
from .showcase import api_curate, curate_page, home, resort_page  # noqa: F401
from .snowcams import (  # noqa: F401
    api_snowcam_depth_grid, api_snowcam_images, api_snowcam_predictions, api_snowcam_resorts,
    api_snowcam_videos, snowcam_accuracy_page, snowcam_predictions_page, snowcam_search, snowcams,
)
from .weather import (  # noqa: F401
    api_forecast_stations, api_observed_stations, api_regional_forecast, api_regional_observed,
    heatmap_test, index, map_view,
)
