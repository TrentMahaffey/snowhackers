from django.contrib import admin
from django.urls import path, include
from django.http import FileResponse, Http404
from pathlib import Path

def serve_snowcam_video(request, filename):
    """Serve snow cam video files"""
    video_path = Path("/snowcam-timelapses") / filename
    if video_path.exists() and video_path.suffix == '.mp4':
        return FileResponse(open(video_path, 'rb'), content_type='video/mp4')
    raise Http404("Video not found")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("media/snowcams/<str:filename>", serve_snowcam_video, name="serve_snowcam"),
    path("", include("dashboard.urls")),
]
