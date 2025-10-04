from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("map/", views.map_view, name="map"),
    path("api/forecast_stations", views.api_forecast_stations, name="api_forecast_stations"),
]
