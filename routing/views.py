"""HTTP views for the route planner and browser demo."""

import json
import os

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .fuel import (
    NoFuelRoute,
    nearby_stations,
    plan_fuel_stops,
    read_prices,
    starting_price,
)

from .geo import (
    MapError,
    autocomplete_cities,
    driving_route,
    geocode,
)


def home(request):
    """Render the optional browser map used for local testing and demo."""
    return render(
        request,
        "routing/home.html",
        {
            "geoapify_api_key": os.getenv(
                "GEOAPIFY_API_KEY",
                "",
            )
        },
    )


def location_search(request):
    """Return US city suggestions for the autocomplete fields."""

    if request.method != "GET":
        return JsonResponse(
            {"error": "Use GET for location search."},
            status=405,
        )

    query = request.GET.get("q", "").strip()

    # Very short searches create broad results and unnecessary API traffic.
    if len(query) < 3:
        return JsonResponse(
            {"results": []}
        )

    if len(query) > 80:
        return JsonResponse(
            {"error": "Location search is too long."},
            status=400,
        )

    try:
        results = autocomplete_cities(
            query,
            limit=8,
        )

        return JsonResponse(
            {"results": results}
        )

    except MapError as exc:
        return JsonResponse(
            {"error": str(exc)},
            status=502,
        )


@csrf_exempt
def route_api(request):
    """
    Calculate a driving route and fuel plan.

    Expected JSON:

    {
        "start": "Dallas, TX",
        "destination": "Chicago, IL"
    }
    """

    if request.method != "POST":
        return JsonResponse(
            {
                "error":
                "Use POST with start and destination."
            },
            status=405,
        )

    if len(request.body) > 4096:
        return JsonResponse(
            {
                "error":
                "Request body is too large."
            },
            status=413,
        )

    try:
        data = json.loads(
            request.body
        )

        if not isinstance(data, dict):
            raise ValueError(
                "Provide a JSON object with start and destination."
            )

        start = data.get("start")
        destination = data.get("destination")

        if (
            not isinstance(start, str)
            or not isinstance(destination, str)
        ):
            raise ValueError(
                "Start and destination must be text such as Dallas, TX."
            )

        start = start.strip()
        destination = destination.strip()

        if not (
            3 <= len(start) <= 100
            and 3 <= len(destination) <= 100
        ):
            raise ValueError(
                "Each location must be between 3 and 100 characters."
            )

        if (
            start.casefold()
            == destination.casefold()
        ):
            raise ValueError(
                "Start and destination must be different."
            )

    except (
        json.JSONDecodeError,
        ValueError,
    ) as exc:

        return JsonResponse(
            {
                "error":
                str(exc) or "Invalid JSON."
            },
            status=400,
        )


    try:
        start_location = geocode(
            start
        )

        end_location = geocode(
            destination
        )

        if (
            start_location is None
            or end_location is None
        ):
            return JsonResponse(
                {
                    "error":
                    "Both places must be found in the USA."
                },
                status=400,
            )


        # One Geoapify road-routing request.
        road = driving_route(
            start_location,
            end_location,
        )


        # Spotter's supplied fuel-price CSV.
        station_prices = read_prices()

        candidates = []


        # Intermediate fuel stops are only needed beyond one full range.
        if road["distance_miles"] > 500:

            try:
                city_limit = int(
                    os.getenv(
                        "MAX_STATION_CITIES",
                        "50",
                    )
                )

            except ValueError:
                city_limit = 50


            try:
                detour_limit = float(
                    os.getenv(
                        "STATION_MAX_DETOUR_MILES",
                        "25",
                    )
                )

            except ValueError:
                detour_limit = 25.0


            city_limit = max(
                4,
                min(
                    150,
                    city_limit,
                ),
            )

            detour_limit = max(
                5.0,
                min(
                    100.0,
                    detour_limit,
                ),
            )


            candidates = nearby_stations(
                road,
                station_prices,
                city_limit,
                detour_limit,
            )


        price, price_note = starting_price(
            start,
            station_prices,
            candidates,
        )


        fuel_plan = plan_fuel_stops(
            road["distance_miles"],
            candidates,
            price,
        )


        return JsonResponse(
            {
                "start":
                    start,

                "destination":
                    destination,

                "distance_miles":
                    round(
                        road["distance_miles"],
                        2,
                    ),

                "states_crossed":
                    road["states"],

                "vehicle_range_miles":
                    500,

                "miles_per_gallon":
                    10,

                **fuel_plan,

                "route_geometry":
                    road["geojson"],

                "notes": [
                    (
                        "Station positions approximate city centers; "
                        "the CSV has no latitude/longitude."
                    ),
                    (
                        "Straight-line city-to-road distances are "
                        "approximate, not actual driving detours."
                    ),
                    (
                        "Refuelling strategy buys only enough for each "
                        "next hop; it is not a global tank-fill optimum."
                    ),
                    (
                        "Fuel model starts with an empty tank and prices "
                        "all trip fuel, including the first purchase."
                    ),
                    (
                        f"Departure fuel pricing: {price_note}."
                    ),
                    (
                        "Routing uses one Geoapify route request; "
                        "place geocoding uses separate cached requests."
                    ),
                ],
            }
        )


    except NoFuelRoute as exc:
        return JsonResponse(
            {
                "error":
                    str(exc)
                    or "A feasible fuel-stop chain could not be found."
            },
            status=422,
        )


    except MapError as exc:
        return JsonResponse(
            {
                "error":
                    str(exc)
            },
            status=502,
        )