import hashlib
import math
import os
import time
from threading import Lock

import requests
from django.core.cache import cache


API_BASE = "https://api.geoapify.com/v1"

METERS_PER_MILE = 1609.344

# Keep backend requests comfortably below provider burst limits.
REQUEST_GAP_SECONDS = 0.35

TEMPORARY_HTTP_ERRORS = {
    429,
    500,
    502,
    503,
    504,
}


class MapError(Exception):
    """Raised when the mapping provider cannot return a usable result."""


_rate_lock = Lock()
_last_request = 0.0


def _cache_key(namespace, *parts):
    """
    Build a cache-safe key.

    Raw coordinates and city names can contain spaces, commas and brackets.
    Hashing prevents Django cache-key warnings and keeps keys short.
    """

    value = "|".join(
        str(part).strip().casefold()
        for part in parts
    )

    digest = hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()

    return f"{namespace}:{digest}"


def _api_key():
    """Read and validate the Geoapify API key."""

    api_key = os.getenv(
        "GEOAPIFY_API_KEY",
        "",
    ).strip()

    if (
        not api_key
        or api_key == "your_free_geoapify_key_here"
    ):
        raise MapError(
            "Add your free GEOAPIFY_API_KEY to the .env file first."
        )

    return api_key


def _retry_delay(reply, attempt):
    """Calculate a short delay before retrying a temporary HTTP failure."""

    retry_after = reply.headers.get(
        "Retry-After"
    )

    if retry_after:
        try:
            return min(
                3.0,
                max(
                    0.5,
                    float(retry_after),
                ),
            )

        except ValueError:
            pass

    return 0.75 * (
        attempt + 1
    )


def _get(path, params):
    """
    Send a Geoapify GET request.

    Backend requests are rate-limited and temporary provider failures
    receive a small number of retries.
    """

    global _last_request

    api_key = _api_key()

    last_error = None


    for attempt in range(3):

        # Ensure requests start a safe distance apart.
        with _rate_lock:

            elapsed = (
                time.monotonic()
                - _last_request
            )

            waiting = max(
                0.0,
                REQUEST_GAP_SECONDS
                - elapsed,
            )

            if waiting:
                time.sleep(
                    waiting
                )

            _last_request = (
                time.monotonic()
            )


        try:
            reply = requests.get(
                f"{API_BASE}/{path}",
                params={
                    **params,
                    "apiKey": api_key,
                },
                timeout=18,
            )


            # Retry only failures that are normally temporary.
            if (
                reply.status_code
                in TEMPORARY_HTTP_ERRORS
                and attempt < 2
            ):
                time.sleep(
                    _retry_delay(
                        reply,
                        attempt,
                    )
                )

                continue


            reply.raise_for_status()

            return reply.json()


        except (
            requests.RequestException,
            ValueError,
        ) as exc:

            last_error = exc

            response = getattr(
                exc,
                "response",
                None,
            )

            status = getattr(
                response,
                "status_code",
                None,
            )


            # Network failures or temporary HTTP failures may recover.
            should_retry = (
                status is None
                or status
                in TEMPORARY_HTTP_ERRORS
            )


            if (
                should_retry
                and attempt < 2
            ):
                time.sleep(
                    0.75
                    * (
                        attempt + 1
                    )
                )

                continue


            break


    raise MapError(
        "Map service unavailable or request rejected. "
        "Check your API key, location, and connection."
    ) from last_error


def geocode(place, expected_state=None):
    """
    Return (longitude, latitude) for a US city or address.

    Successful results are cached so repeated route calculations do not
    repeatedly call the provider.
    """

    key = _cache_key(
        "geocode-v3",
        place,
        expected_state or "",
    )


    coords = cache.get(
        key
    )


    if coords is not None:
        return coords


    result = _get(
        "geocode/search",
        {
            "text":
                f"{place}, USA",

            "filter":
                "countrycode:us",

            "limit":
                5,

            "format":
                "json",

            "lang":
                "en",
        },
    )


    places = result.get(
        "results",
        [],
    )


    match = next(
        (
            item
            for item in places
            if (
                item.get(
                    "country_code",
                    "",
                ).lower()
                == "us"
            )
            and (
                not expected_state
                or item.get(
                    "state_code",
                    "",
                ).upper()
                == expected_state.upper()
            )
        ),
        None,
    )


    if not match:
        return None


    try:
        coords = (
            float(
                match["lon"]
            ),
            float(
                match["lat"]
            ),
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:

        raise MapError(
            "The location provider returned invalid coordinates."
        ) from exc


    cache.set(
        key,
        coords,
        timeout=60 * 60 * 24 * 14,
    )


    return coords


def autocomplete_cities(query, limit=8):
    """
    Return useful US city suggestions for partial user input.

    Geoapify performs the underlying search. We request more results than
    are displayed, remove duplicates, then rank exact/prefix matches before
    returning the best suggestions.
    """

    query = query.strip()


    if len(query) < 3:
        return []


    cache_key = _cache_key(
        "city-autocomplete-v2",
        query,
    )


    cached = cache.get(
        cache_key
    )


    if cached is not None:
        return cached


    data = _get(
        "geocode/autocomplete",
        {
            "text":
                query,

            "type":
                "city",

            "filter":
                "countrycode:us",

            "limit":
                20,

            "format":
                "json",

            "lang":
                "en",
        },
    )


    city_query = (
        query
        .split(",", 1)[0]
        .strip()
        .casefold()
    )


    state_query = ""

    if "," in query:
        state_query = (
            query
            .split(",", 1)[1]
            .strip()
            .upper()
        )


    suggestions = []

    seen = set()


    for provider_order, result in enumerate(
        data.get(
            "results",
            [],
        )
    ):

        city = (
            result.get("city")
            or result.get("name")
        )

        state = result.get(
            "state_code"
        )


        if (
            not city
            or not state
        ):
            continue


        if (
            result.get(
                "country_code",
                "",
            ).lower()
            != "us"
        ):
            continue


        city = str(
            city
        ).strip()

        state = str(
            state
        ).strip().upper()


        # The assessment asks for cities, not township-style admin names.
        if (
            "township"
            in city.casefold()
        ):
            continue


        label = (
            f"{city}, {state}"
        )

        unique_key = (
            label.casefold()
        )


        if unique_key in seen:
            continue


        seen.add(
            unique_key
        )


        city_lower = (
            city.casefold()
        )


        # Rank the text match.
        if (
            city_lower
            == city_query
        ):
            match_rank = 0

        elif (
            city_lower.startswith(
                city_query
            )
        ):
            match_rank = 1

        elif any(
            word.startswith(
                city_query
            )
            for word
            in city_lower
            .replace(
                "-",
                " ",
            )
            .split()
        ):
            match_rank = 2

        elif (
            city_query
            in city_lower
        ):
            match_rank = 3

        else:
            match_rank = 4


        # If the user typed a state fragment, prefer that state.
        if state_query:
            state_rank = (
                0
                if state.startswith(
                    state_query
                )
                else 1
            )

        else:
            state_rank = 0


        suggestions.append(
            {
                "label":
                    label,

                "city":
                    city,

                "state":
                    state,

                "latitude":
                    result.get(
                        "lat"
                    ),

                "longitude":
                    result.get(
                        "lon"
                    ),

                "_state_rank":
                    state_rank,

                "_match_rank":
                    match_rank,

                "_provider_order":
                    provider_order,
            }
        )


    suggestions.sort(
        key=lambda item: (
            item["_state_rank"],
            item["_match_rank"],
            item["city"].casefold(),
            item["_provider_order"],
        )
    )


    clean_results = []


    for item in suggestions[:limit]:

        clean_results.append(
            {
                "label":
                    item["label"],

                "city":
                    item["city"],

                "state":
                    item["state"],

                "latitude":
                    item["latitude"],

                "longitude":
                    item["longitude"],
            }
        )


    cache.set(
        cache_key,
        clean_results,
        timeout=60 * 60 * 24 * 7,
    )


    return clean_results


def driving_route(start, end):
    """
    Return road distance, route geometry, states and road-name hints.
    """

    key = _cache_key(
        "route-v3",
        start,
        end,
    )


    saved = cache.get(
        key
    )


    if saved is not None:
        return saved


    payload = _get(
        "routing",
        {
            "waypoints":
                (
                    f"{start[1]},{start[0]}"
                    f"|"
                    f"{end[1]},{end[0]}"
                ),

            "mode":
                "drive",

            "details":
                "admin_areas",
        },
    )


    try:
        feature = (
            payload["features"][0]
        )

        properties = (
            feature["properties"]
        )

        coordinates = (
            feature["geometry"]["coordinates"]
        )


        # Two input waypoints normally produce one route line.
        line = coordinates[0]


        if len(line) < 2:
            raise ValueError(
                "Empty road geometry"
            )


        miles = (
            float(
                properties["distance"]
            )
            / METERS_PER_MILE
        )


        states = sorted(
            {
                area.get(
                    "state_code",
                    "",
                ).upper()

                for area
                in properties.get(
                    "admin_areas",
                    [],
                )

                if area.get(
                    "country_code",
                    "",
                ).lower()
                == "us"
            }
            - {""}
        )


        country_codes = properties.get(
            "country_code",
            ["us"],
        )


        if isinstance(
            country_codes,
            str,
        ):
            country_codes = [
                country_codes
            ]


        if any(
            str(code).lower()
            != "us"

            for code
            in country_codes
        ):
            raise ValueError(
                "Route leaves the United States"
            )


        result = {
            "distance_miles":
                miles,

            "states":
                states,

            "line":
                line,

            "geojson":
                feature["geometry"],

            "road_names":
                _road_names(
                    properties
                ),
        }


    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:

        raise MapError(
            "No supported US driving route was returned. "
            "Try two mainland US cities."
        ) from exc


    cache.set(
        key,
        result,
        timeout=60 * 60 * 12,
    )


    return result


def _road_names(properties):
    """
    Collect optional road names from route steps.

    Highway matching in fuel.py uses these only as a preference.
    """

    names = []


    for leg in properties.get(
        "legs",
        [],
    ):

        for step in leg.get(
            "steps",
            [],
        ):

            name = step.get(
                "name"
            )

            if name:
                names.append(
                    str(name)
                )


    return names


def distance_miles(point_a, point_b):
    """Return straight-line Haversine distance in miles."""

    lon1, lat1 = map(
        math.radians,
        point_a,
    )

    lon2, lat2 = map(
        math.radians,
        point_b,
    )


    a = (
        math.sin(
            (
                lat2 - lat1
            )
            / 2
        )
        ** 2

        +

        math.cos(
            lat1
        )

        * math.cos(
            lat2
        )

        * math.sin(
            (
                lon2 - lon1
            )
            / 2
        )
        ** 2
    )


    return (
        3958.7613
        * 2
        * math.asin(
            min(
                1,
                math.sqrt(a),
            )
        )
    )


def nearest_route_mile(
    city,
    line,
    total_miles,
):
    """
    Estimate a city's mile marker and straight-line offset from the route.

    The city is projected onto sampled road segments rather than merely
    compared with route vertices.
    """

    stride = max(
        1,
        len(line) // 400,
    )


    points = list(
        line[::stride]
    )


    if (
        points[-1]
        != line[-1]
    ):
        points.append(
            line[-1]
        )


    segment_lengths = [
        distance_miles(
            point_a,
            point_b,
        )

        for point_a, point_b
        in zip(
            points,
            points[1:],
        )
    ]


    whole_line_miles = sum(
        segment_lengths
    )


    if whole_line_miles == 0:
        return (
            0.0,
            distance_miles(
                city,
                points[0],
            ),
        )


    longitude_scale = math.cos(
        math.radians(
            city[1]
        )
    )


    best = (
        float("inf"),
        0.0,
    )


    miles_so_far = 0.0


    for (
        (a_lon, a_lat),
        (b_lon, b_lat),
        segment_miles,
    ) in zip(
        points,
        points[1:],
        segment_lengths,
    ):

        ax = (
            a_lon
            * longitude_scale
        )

        ay = a_lat

        bx = (
            b_lon
            * longitude_scale
        )

        by = b_lat

        px = (
            city[0]
            * longitude_scale
        )

        py = city[1]


        dx = (
            bx - ax
        )

        dy = (
            by - ay
        )


        denominator = (
            dx * dx
            + dy * dy
        )


        if denominator:
            fraction = (
                (
                    (px - ax) * dx
                    + (py - ay) * dy
                )
                / denominator
            )

            fraction = max(
                0,
                min(
                    1,
                    fraction,
                ),
            )

        else:
            fraction = 0


        projection = (
            a_lon
            + fraction
            * (
                b_lon - a_lon
            ),

            a_lat
            + fraction
            * (
                b_lat - a_lat
            ),
        )


        offset = distance_miles(
            city,
            projection,
        )


        projected_miles = (
            miles_so_far
            + segment_miles
            * fraction
        )


        if offset < best[0]:

            best = (
                offset,
                projected_miles,
            )


        miles_so_far += (
            segment_miles
        )


    mile_marker = (
        total_miles
        * best[1]
        / whole_line_miles
    )


    return (
        mile_marker,
        best[0],
    )