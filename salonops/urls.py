from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from operations import pwa_views

urlpatterns=[
    path('manifest.webmanifest', pwa_views.manifest, name='pwa_manifest'),
    path('service-worker.js', pwa_views.service_worker, name='pwa_service_worker'),
    path('offline/', pwa_views.offline, name='pwa_offline'),
    path('admin/',admin.site.urls),
    path('favicon.ico', RedirectView.as_view(url='/static/img/favicon.ico', permanent=False)),
    path('',include('operations.urls')),
]
