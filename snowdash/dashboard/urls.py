from django.urls import path
from . import views

urlpatterns = [
    path("", views.map_view, name="index"),  # Map is now home
    path("tables/", views.index, name="tables"),  # Old home moved to /tables
    path("snowcams/", views.snowcams, name="snowcams"),  # Snow cam timelapses
    path("heatmap_test/", views.heatmap_test, name="heatmap_test"),
    path("api/forecast_stations", views.api_forecast_stations, name="api_forecast_stations"),
    path("api/observed_stations", views.api_observed_stations, name="api_observed_stations"),
    path("api/regional_forecast", views.api_regional_forecast, name="api_regional_forecast"),
    path("api/regional_observed", views.api_regional_observed, name="api_regional_observed"),
    path("api/snowcam_videos", views.api_snowcam_videos, name="api_snowcam_videos"),
]
