# US Fuel Route Planner - Spotter Django Assessment

A deliberately small Django application: **one main API endpoint, one supplied CSV, one mapping provider**.

Enter two US locations and get a driving route, approximate inexpensive fuel stops, and estimated total fuel spending at **10 MPG** with a **500-mile maximum fuel range**. The optional home page draws the route on a Leaflet map.

## Project files

```text
spotter_fuel_route_assessment/
├── .env.example                    # Configuration example, copy it to .env
├── .gitignore
├── README.md
├── requirements.txt
├── manage.py
├── config/                         # Minimal Django settings and URLs
├── routing/
│   ├── views.py                    # The one API endpoint
│   ├── geo.py                      # Geoapify requests and location math
│   ├── fuel.py                     # CSV loading and fuel planning
│   ├── tests.py                    # Offline mocked unit tests
│   └── templates/routing/home.html # Optional map preview
└── data/
    └── fuel-prices-for-be-assessment.csv  # Original file, unchanged
```

No second geographic CSV, PostgreSQL, DRF, accounts, migrations, or separate frontend are required for this assessment. Django's **file cache** is auto-created at runtime to store API responses; this is *not* another source dataset.

## Install

Install **Python 3.12 or newer**. Commands below assume you have extracted the ZIP and opened a terminal in `spotter_fuel_route_assessment`.

**Windows (PowerShell):**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Create a **free** [Geoapify account](https://myprojects.geoapify.com/), copy the project API key, then set in `.env`:

```ini
GEOAPIFY_API_KEY=your_actual_key_from_geoapify
```

One Geoapify key handles **location search** and **driving directions**. Keep the `.env` file private. You need internet access for first-use maps/geocoding and for Leaflet map tiles.

Run:

```bash
python manage.py check
python manage.py test
python manage.py runserver
```

Open **http://127.0.0.1:8000/** for the optional map preview.

## The API

**POST** `http://127.0.0.1:8000/api/route/`

Postman: choose POST, Body > raw > JSON; send:

```json
{
  "start": "Dallas, TX",
  "destination": "Houston, TX"
}
```

The response includes `distance_miles`, `states_crossed`, `route_geometry` (GeoJSON), `fuel_gallons`, `start_fuel_gallons`, `start_fuel_cost_usd`, `fuel_stops`, `total_fuel_cost_usd`, and `notes`.

Each selected fuel stop has city, state, station name, price, approximate mile marker, and fuel purchased there. Price and stop totals vary with the CSV, actual route, and Geoapify lookup results; there is no hardcoded expected output.

Example terminal request:

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Dallas, TX","destination":"Houston, TX"}'
```

**Another test:** Dallas, TX to Chicago, IL is long enough to potentially require fuel stops. Due to the source CSV lacking coordinates, the route can sometimes respond with `422` if the shortlist cannot verify a reachable chain. Try a larger `MAX_STATION_CITIES` or `STATION_MAX_DETOUR_MILES`, and explain the approximation if presenting it.

## How it works (the five-minute explanation)

1. `views.py` validates `start` and `destination` and calls `geo.geocode()` for both.
2. `geo.driving_route()` makes **one routing request** to Geoapify and receives a GeoJSON road shape, road mileage, and states crossed.
3. `fuel.read_prices()` reads **only** the CSV, drops non-US rows, and takes the lowest station price per city.
4. If the trip is longer than 500 miles, `fuel.nearby_stations()` geocodes a small shortlist of cities in route states, cached to disk, and keeps those near the road line.
5. `fuel.plan_fuel_stops()` chooses reachable low-cost hops and calculates purchased gallons and the total bill. The view returns JSON and the home page draws the result.

## Fuel model and limitations (explain these honestly)

- Range = 500 miles; efficiency = 10 MPG; implied maximum tank capacity = **50 gallons**.
- The model assumes the vehicle **starts with an empty tank** and purchases the required fuel at an estimated departure price. This makes all consumed fuel count toward the cost. The starting price uses the cheapest priced station in the departure city when available; otherwise, it uses an explicit average or fallback.
- It buys **just enough for each chosen next hop**, never more than 50 gallons per hop. Its small dynamic-programming comparison finds the cheapest chain **under that purchase policy**. It is **not** a proof of the global cheapest schedule with partial top-ups, tank carryover, or independently routed detours.
- The CSV contains address strings, cities and states, **but no latitude/longitude**. Geocoding uses *city centers* rather than exact truck-stop coordinates. `distance_from_route_miles` is straight-line distance; it is *not* actual vehicle driving detour mileage.
- Before geocoding, the shortlist selects a limited number of inexpensive, geographically diverse cities from each route state. This keeps calls bounded but can miss valid stations; a `422` is an honest result when there is no confirmed feasible chain in that shortlist.
- **External calls:** 1 Geoapify **routing** call for each new route, **2 geocoding** calls for new start/end locations, and for journeys exceeding 500 miles **up to `MAX_STATION_CITIES` additional city geocoding calls**. Cached locations/routes are re-used. This does **not** mean only one external API request overall.
- Maps use free OpenStreetMap tile imagery through Leaflet, requiring internet in the browser; there is no map image file in the project.

## Configuration and changes

| File / setting | What to change |
|---|---|
| `.env` -> `GEOAPIFY_API_KEY` | Paste your private free API key. |
| `.env` -> `MAX_STATION_CITIES` | Default `30`; increase to `50` if a long trip cannot find stops (slower first request). |
| `.env` -> `STATION_MAX_DETOUR_MILES` | Default `25`; allowed straight-line distance from the route to each city. |
| `routing/fuel.py` -> `MAX_RANGE_MILES` | Change vehicle tank range if required. |
| `routing/fuel.py` -> `MILES_PER_GALLON` | Change assumed fuel economy. |
| `routing/fuel.py` -> `starting_price()` | Change the departure fuel price assumption. |
| `routing/fuel.py` -> `choose_cities()` | Change station shortlist logic. |
| `routing/fuel.py` -> `plan_fuel_stops()` | Change the cost calculation and optimization. |
| `routing/templates/routing/home.html` | Change the demo form/map appearance. |

Run `python manage.py test` after changes. Delete `.cache/` only if you specifically want to clear cached geocoding/routing results.

## How to demo in Loom (max 5 min)

- 0:00-0:35: explain one endpoint, one original CSV, one Geoapify provider.
- 0:35-1:20: show `views.py` - input, route, fuel planner, JSON output.
- 1:20-2:05: show `fuel.py` - CSV price lookup and 500-mile range.
- 2:05-3:30: send a sample Postman request; explain distance, stops and total bill.
- 3:30-4:15: open the Leaflet map and show its route/station markers.
- 4:15-4:45: mention caching and source-data limitations.

**Important:** This ZIP contains the code and documentation, **not** a Geoapify key, hosted demo, GitHub URL, or Loom recording. Configure and run it yourself before submitting.

## References

- Geoapify geocoding: https://apidocs.geoapify.com/docs/geocoding/
- Geoapify routing: https://apidocs.geoapify.com/docs/routing/
- Django: https://docs.djangoproject.com/
- Leaflet: https://leafletjs.com/
