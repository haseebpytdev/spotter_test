"""Fuel prices and a small, explainable route-planning algorithm.

The assessment CSV is our only fuel-price dataset. It has no coordinates,
so we locate shortlisted *cities* with Geoapify, then compare their prices.
"""

import csv
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from .geo import MapError, geocode, nearest_route_mile

MAX_RANGE_MILES = 500
MILES_PER_GALLON = 10
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
CSV_PATH = Path(settings.BASE_DIR) / "data" / "fuel-prices-for-be-assessment.csv"


class NoFuelRoute(Exception):
    """A route cannot be shown reliably with the shortlisted fuel locations."""


@lru_cache(maxsize=1)
def read_prices():
    """Read the original CSV once and retain the cheapest station in each US city.

    Grouping by city avoids thousands of duplicate geocoding requests. It also
    makes an important limitation explicit: we use city-center coordinates,
    not exact driveway locations of truck stops.
    """
    cheapest_by_city = {}
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            state = row["State"].strip().upper()
            city = row["City"].strip()
            if state not in US_STATES or not city:
                continue
            try:
                price = float(row["Retail Price"])
            except (ValueError, TypeError):
                continue
            if not 0 < price < 100:
                continue

            station = {
                "name": row["Truckstop Name"].strip(),
                "city": city,
                "state": state,
                "address": row["Address"].strip(),
                "price_per_gallon": price,
            }
            key = (city.casefold(), state)
            if key not in cheapest_by_city or price < cheapest_by_city[key]["price_per_gallon"]:
                cheapest_by_city[key] = station
    return list(cheapest_by_city.values())


def _route_highways(road_names):
    """Optional preference for stations whose CSV address mentions a route interstate."""
    found = set()
    for name in road_names:
        for match in re.finditer(r"(?:interstate\s*|\bi[-\s]*)(\d{1,3})\b", name, re.I):
            found.add(match.group(1))
    return found


def choose_cities(stations, states, road_names, limit):
    """Create a small, price-aware shortlist across all route states.

    Without coordinates in the CSV, we cannot know which cities lie directly
    beside the route before geocoding them. This shortlist intentionally mixes
    inexpensive cities with other cities, so we do not search only one area.
    """
    highways = _route_highways(road_names)
    by_state = defaultdict(list)
    for station in stations:
        if station["state"] not in states:
            continue
        on_highway = any(re.search(rf"\bI[-\s]*{number}\b", station["address"], re.I)
                         for number in highways)
        by_state[station["state"]].append((on_highway, station))

    queues = {}
    for state, entries in by_state.items():
        # Prefer a matching interstate, but always include non-matching cities.
        matching = sorted((s for yes, s in entries if yes), key=lambda s: s["price_per_gallon"])
        others = sorted((s for yes, s in entries if not yes), key=lambda s: s["price_per_gallon"])

        def spread(items):
            cheap = items[:4]
            remaining = sorted(items[4:], key=lambda s: s["city"].lower())
            step = max(1, len(remaining) // 8)
            return cheap + remaining[::step]

        queues[state] = spread(matching) + spread(others)

    chosen = []
    # Round-robin prevents a large state from taking every lookup slot.
    while len(chosen) < limit and any(queues.values()):
        for state in sorted(queues):
            if queues[state]:
                chosen.append(queues[state].pop(0))
                if len(chosen) == limit:
                    break
    return chosen


def nearby_stations(route, stations, limit=30, max_detour_miles=25):
    """Geocode shortlisted cities; retain candidates near the route geometry.

    One *routing* call is enough. Geocoding city lookups are additional Geoapify
    calls on the first run, cached to disk for subsequent use.
    """
    if not route["states"]:
        raise NoFuelRoute("The routing provider did not return the states crossed.")
    shortlisted = choose_cities(stations, set(route["states"]), route["road_names"], limit)
    if not shortlisted:
        raise NoFuelRoute("No US fuel-price data found for the states on this route.")

    def locate(station):
        point = geocode(f"{station['city']}, {station['state']}", expected_state=station["state"])
        if point is None:
            return None
        mile, distance = nearest_route_mile(point, route["line"], route["distance_miles"])
        if distance > max_detour_miles or not 2 < mile < route["distance_miles"] - 2:
            return None
        return {
            **station,
            "longitude": point[0],
            "latitude": point[1],
            "mile_marker": round(mile, 2),
            "distance_from_route_miles": round(distance, 2),
        }

    nearby = []
    # Small concurrency speeds up cold-start geocoding without flooding the provider.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(locate, station) for station in shortlisted]):
            try:
                located = future.result()
                if located:
                    nearby.append(located)
            except MapError:
                continue  # Some places might not have a usable geocode match.
    return sorted(nearby, key=lambda item: item["mile_marker"])


def starting_price(start, stations, candidates):
    """Use the departure city's cheapest CSV fuel, or a labelled estimate."""
    match = re.fullmatch(r"\s*([^,]+)\s*,\s*([A-Za-z]{2})\s*", start)
    if match:
        city, state = match.group(1).casefold(), match.group(2).upper()
        local = [s["price_per_gallon"] for s in stations
                 if s["city"].casefold() == city and s["state"] == state]
        if local:
            return min(local), "cheapest CSV station in the departure city"
        regional = [s["price_per_gallon"] for s in stations if s["state"] == state]
        if regional:
            return sum(regional) / len(regional), "average lowest city price in departure state"
    if candidates:
        closest = min(candidates, key=lambda s: s["mile_marker"])
        return closest["price_per_gallon"], "price of the first shortlisted nearby station"
    if stations:
        return sum(s["price_per_gallon"] for s in stations) / len(stations), "US city-price average from the supplied CSV"
    raise NoFuelRoute("The supplied fuel-price CSV did not contain valid US prices.")


def plan_fuel_stops(distance, candidates, departure_price):
    """Select affordable hops, each no longer than the 500-mile tank range.

    At each stop, the model buys just enough gallons to reach the *next* stop.
    A small dynamic-programming table compares the prices of all feasible hops.
    This is optimal for that simplified purchase policy, not for arbitrary
    partial-fuelling strategies or real-world road-detour distances.

    Assumption: the tank is initially empty and trip fuel is purchased at the
    departure-city price. We do not charge for fuel left in the tank at arrival.
    """
    if distance <= 0 or departure_price <= 0:
        raise NoFuelRoute("Distance and departure fuel price must both be positive.")

    points = [{"mile_marker": 0.0, "price_per_gallon": departure_price, "name": "Departure"}]
    points.extend(station for station in sorted(candidates, key=lambda s: s["mile_marker"])
                  if 1 < station["mile_marker"] < distance - 1)
    points.append({"mile_marker": distance, "name": "Destination"})
    destination_index = len(points) - 1

    # best[j]: (lowest cost to reach j, number of refuelling stops, prior point).
    best = [None] * len(points)
    best[0] = (0.0, 0, None)
    for j in range(1, len(points)):
        choices = []
        for i in range(j):
            miles = points[j]["mile_marker"] - points[i]["mile_marker"]
            if best[i] is None or not 0 < miles <= MAX_RANGE_MILES:
                continue
            gallons = miles / MILES_PER_GALLON
            cost = best[i][0] + gallons * points[i]["price_per_gallon"]
            stops = best[i][1] + (0 if j == destination_index else 1)
            choices.append((cost, stops, i))
        if choices:
            best[j] = min(choices)

    if best[destination_index] is None:
        raise NoFuelRoute(
            "No nearby fuel-stop chain can safely cover this trip within 500 miles per hop. "
            "This is a shortlist limitation, not proof that no real stations exist. "
            "Try increasing MAX_STATION_CITIES or STATION_MAX_DETOUR_MILES."
        )

    # Follow the chosen steps backwards to calculate fuel bought at every stop.
    path = [destination_index]
    while path[-1] != 0:
        path.append(best[path[-1]][2])
    path.reverse()

    purchases = []
    for current, following in zip(path, path[1:]):
        origin = points[current]
        gallons = (points[following]["mile_marker"] - origin["mile_marker"]) / MILES_PER_GALLON
        purchases.append((current, gallons, gallons * origin["price_per_gallon"]))

    stops = []
    for idx, gallons, fuel_cost in purchases[1:]:
        stops.append({
            **points[idx],
            "price_per_gallon": round(points[idx]["price_per_gallon"], 3),
            "gallons_purchased": round(gallons, 2),
            "purchase_cost_usd": round(fuel_cost, 2),
        })

    return {
        "fuel_stops": stops,
        "fuel_gallons": round(distance / MILES_PER_GALLON, 2),
        "start_fuel_gallons": round(purchases[0][1], 2),
        "start_fuel_cost_usd": round(purchases[0][2], 2),
        "initial_fuel_price_per_gallon": round(departure_price, 3),
        "total_fuel_cost_usd": round(best[destination_index][0], 2),
    }
