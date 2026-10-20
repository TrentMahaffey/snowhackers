from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="index"),  # Storm showcase is home (2026-10)
    path("map/", views.map_view, name="map"),
    path("forecast/", views.forecast_page, name="forecast_page"),
    path("resort/<str:prefix>/", views.resort_page, name="resort_page"),
    path("curate/", views.curate_page, name="curate_page"),
    path("api/curate", views.api_curate, name="api_curate"),
    path("tables/", views.index, name="tables"),  # Old home moved to /tables
    path("snowcams/", views.snowcams, name="snowcams"),  # Timelapses (default)
    path("snowcams/search/", views.snowcam_search, name="snowcam_search"),  # Advanced per-image search
    path("snowcams/predictions/", views.snowcam_predictions_page, name="snowcam_predictions_page"),  # Simple per-image feed
    path("snowcams/accuracy/", views.snowcam_accuracy_page, name="snowcam_accuracy_page"),  # Labeled-truth vs model demo
    path("heatmap_test/", views.heatmap_test, name="heatmap_test"),
    path("api/forecast_stations", views.api_forecast_stations, name="api_forecast_stations"),
    path("api/observed_stations", views.api_observed_stations, name="api_observed_stations"),
    path("api/regional_forecast", views.api_regional_forecast, name="api_regional_forecast"),
    path("api/regional_observed", views.api_regional_observed, name="api_regional_observed"),
    path("api/snowcam_videos", views.api_snowcam_videos, name="api_snowcam_videos"),
    path("api/snowcam_predictions", views.api_snowcam_predictions, name="api_snowcam_predictions"),
    path("api/snowcam_images", views.api_snowcam_images, name="api_snowcam_images"),
    path("api/snowcam_resorts", views.api_snowcam_resorts, name="api_snowcam_resorts"),
    path("api/snowcam_depth_grid", views.api_snowcam_depth_grid, name="api_snowcam_depth_grid"),
]
