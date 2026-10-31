"""Offline unit tests: no Geoapify key or internet connection required."""

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, SimpleTestCase

from .fuel import (
    NoFuelRoute,
    choose_cities,
    plan_fuel_stops,
    read_prices,
    starting_price,
)
from .geo import (
    distance_miles,
    driving_route,
    nearest_route_mile,
)


class FuelTests(SimpleTestCase):
    def setUp(self):
        # Each test should start independently of routes cached by local app use.
        cache.clear()

    def test_original_csv_contains_us_prices(self):
        prices = read_prices()
        self.assertGreater(len(prices), 1000)
        self.assertNotIn("ON", {row["state"] for row in prices})

    def test_short_trip_no_stop(self):
        plan = plan_fuel_stops(180, [], 3.50)

        self.assertEqual(plan["fuel_stops"], [])
        self.assertEqual(plan["fuel_gallons"], 18)
        self.assertEqual(plan["start_fuel_cost_usd"], 63)
        self.assertEqual(plan["total_fuel_cost_usd"], 63)

    def test_long_trip_prefers_affordable_reachable_stops(self):
        stops = [
            {
                "name": "Expensive",
                "mile_marker": 350,
                "price_per_gallon": 4.1,
            },
            {
                "name": "Affordable",
                "mile_marker": 400,
                "price_per_gallon": 2.9,
            },
            {
                "name": "Second",
                "mile_marker": 790,
                "price_per_gallon": 3.0,
            },
        ]

        answer = plan_fuel_stops(1100, stops, 3.6)

        self.assertEqual(
            [station["name"] for station in answer["fuel_stops"]],
            ["Affordable", "Second"],
        )
        self.assertEqual(answer["fuel_gallons"], 110)
        self.assertEqual(answer["start_fuel_gallons"], 40)
        self.assertEqual(
            answer["fuel_stops"][0]["gallons_purchased"],
            39,
        )
        self.assertAlmostEqual(
            answer["total_fuel_cost_usd"],
            350.1,
            places=2,
        )

    def test_disconnected_trip_returns_error(self):
        with self.assertRaises(NoFuelRoute):
            plan_fuel_stops(1100, [], 3.5)

    def test_departure_city_and_average_fallback(self):
        prices = read_prices()

        self.assertGreater(
            starting_price(
                "Dallas, TX",
                prices,
                [],
            )[0],
            0,
        )

        price, note = starting_price(
            "Unknown town",
            prices,
            [],
        )

        self.assertGreater(price, 0)
        self.assertIn("average", note)

    def test_nearby_city_geometry(self):
        self.assertAlmostEqual(
            distance_miles(
                (-96, 33),
                (-96, 33),
            ),
            0,
        )

        mile, detour = nearest_route_mile(
            (-95.5, 33),
            [
                [-96, 33],
                [-95, 33],
            ],
            100,
        )

        self.assertLess(detour, 40)
        self.assertGreater(mile, 0)

    def test_shortlist_state_filter(self):
        rows = [
            {
                "city": "Dallas",
                "state": "TX",
                "address": "I-35",
                "price_per_gallon": 3.1,
            },
            {
                "city": "Miami",
                "state": "FL",
                "address": "I-95",
                "price_per_gallon": 2.1,
            },
        ]

        self.assertEqual(
            len(
                choose_cities(
                    rows,
                    {"TX"},
                    ["I-35"],
                    10,
                )
            ),
            1,
        )

    @patch("routing.geo._get")
    def test_route_admin_areas_and_one_call(self, mock_get):
        mock_get.return_value = {
            "features": [
                {
                    "properties": {
                        "distance": 1609344,
                        "country_code": ["us"],
                        "admin_areas": [
                            {
                                "country_code": "us",
                                "state_code": "tx",
                            }
                        ],
                        "legs": [
                            {
                                "steps": [],
                            }
                        ],
                    },
                    "geometry": {
                        "type": "MultiLineString",
                        "coordinates": [
                            [
                                [-96, 32],
                                [-95, 33],
                            ]
                        ],
                    },
                }
            ]
        }

        result = driving_route(
            (-96, 32),
            (-95, 33),
        )

        self.assertEqual(
            result["states"],
            ["TX"],
        )

        self.assertAlmostEqual(
            result["distance_miles"],
            1000,
        )

        self.assertEqual(
            mock_get.call_count,
            1,
        )


class ApiTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def post(self, payload):
        return self.client.post(
            "/api/route/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_bad_requests(self):
        self.assertEqual(
            self.client.get(
                "/api/route/"
            ).status_code,
            405,
        )

        self.assertEqual(
            self.post(
                {
                    "start": "Boston, MA",
                    "destination": "Boston, MA",
                }
            ).status_code,
            400,
        )

        self.assertEqual(
            self.post(
                {
                    "start": 12,
                    "destination": "Dallas, TX",
                }
            ).status_code,
            400,
        )

        self.assertEqual(
            self.post(
                [
                    "Dallas",
                    "Houston",
                ]
            ).status_code,
            400,
        )

    @patch("routing.views.nearby_stations")
    @patch("routing.views.driving_route")
    @patch("routing.views.geocode")
    def test_successful_short_route_skips_station_geocoding(
        self,
        mock_geocode,
        mock_route,
        mock_stations,
    ):
        mock_geocode.side_effect = [
            (-96.8, 32.8),
            (-95.4, 29.8),
        ]

        mock_route.return_value = {
            "distance_miles": 239,
            "states": ["TX"],
            "road_names": [],
            "line": [
                [-96.8, 32.8],
                [-95.4, 29.8],
            ],
            "geojson": {
                "type": "MultiLineString",
                "coordinates": [
                    [
                        [-96.8, 32.8],
                        [-95.4, 29.8],
                    ]
                ],
            },
        }

        response = self.post(
            {
                "start": "Dallas, TX",
                "destination": "Houston, TX",
            }
        )

        self.assertEqual(
            response.status_code,
            200,
            response.content,
        )

        answer = response.json()

        self.assertEqual(
            answer["fuel_stops"],
            [],
        )

        self.assertEqual(
            answer["fuel_gallons"],
            23.9,
        )

        self.assertIn(
            "route_geometry",
            answer,
        )

        mock_stations.assert_not_called()