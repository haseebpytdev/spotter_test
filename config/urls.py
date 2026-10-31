from django.urls import path

from routing.views import (
    home,
    location_search,
    route_api,
)


urlpatterns = [
    path(
        "",
        home,
        name="home",
    ),

    path(
        "api/locations/",
        location_search,
        name="location_search",
    ),

    path(
        "api/route/",
        route_api,
        name="route_api",
    ),
]