"""Forecast data must cover the whole requested hike, not merely overlap it."""
import unittest
import copy
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from backend.tools.weather import hike_window
from backend.tools import weather, met


def period(start, end):
    return {"startTime": f"2026-11-15T{start}:00:00+00:00", "endTime": f"2026-11-15T{end}:00:00+00:00",
            "temperature": -15, "temperatureUnit": "F", "shortForecast": "Cold",
            "probabilityOfPrecipitation": {"value": 5}, "windSpeed": "5 mph"}


class CoverageTests(unittest.TestCase):
    def summarize(self, periods):
        return hike_window(periods, "2026-11-15", "10:00", 180, ZoneInfo("UTC"))

    def test_one_hour_cannot_describe_three_hour_hike(self):
        self.assertIsNone(self.summarize([period("10", "11")]))

    def test_missing_start(self):
        self.assertIsNone(self.summarize([period("11", "13")]))

    def test_gap_inside_window(self):
        self.assertIsNone(self.summarize([period("10", "11"), period("12", "13")]))

    def test_unsorted_full_coverage_preserves_negative_temperature(self):
        result = self.summarize([period("12", "13"), period("10", "12")])
        self.assertEqual(result["window"], "10:00-13:00")
        self.assertEqual(result["min_temp_f"], -15)

    def test_overlapping_coverage_is_valid(self):
        self.assertIsNotNone(self.summarize([period("09", "12"), period("11", "14")]))

    def test_exact_boundaries_and_outside_periods(self):
        self.assertIsNotNone(self.summarize([period("09", "10"), period("10", "13"), period("13", "14")]))
        self.assertIsNone(self.summarize([period("09", "10"), period("13", "14")]))

    def test_midnight_and_differing_offsets(self):
        a = period("10", "11")
        a.update(startTime="2026-11-15T23:00:00-05:00", endTime="2026-11-16T05:00:00+00:00")
        b = {**a, 'startTime': '2026-11-16T00:00:00-05:00', 'endTime': '2026-11-16T02:00:00-05:00'}
        result = hike_window([b, a], '2026-11-15', '23:00', 180, ZoneInfo('America/Detroit'))
        self.assertEqual(result['window'], '23:00-02:00')
        self.assertEqual(result['min_temp_f'], -15)
        b['startTime'] = '2026-11-16T00:01:00-05:00'
        self.assertIsNone(hike_window([a, b], '2026-11-15', '23:00', 180, ZoneInfo('America/Detroit')))

    def test_dst_duration_is_elapsed_time(self):
        a = period('10', '11')
        a.update(startTime='2026-11-01T01:00:00-04:00', endTime='2026-11-01T02:00:00-05:00')
        result = hike_window([a], '2026-11-01', '01:00', 120, ZoneInfo('America/Detroit'))
        self.assertEqual(result['window'], '01:00-02:00')

    def test_incomplete_nws_result_has_no_full_window_summary(self):
        with patch.object(weather, 'hourly_periods', return_value={'periods': [period('10', '11')]}), \
             patch.object(weather, 'timezone_for', return_value=timezone.utc):
            result = weather.get_hike_window(0, 0, '2026-11-15', '10:00', 180)
        self.assertFalse(result['available'])
        self.assertIn('complete', result['error'])
        self.assertNotIn('summary', result)
        self.assertNotIn('window', result)


def met_step(start, hours=1, amount=1):
    return (datetime.fromisoformat(start), hours,
            {'temp_c': -15, 'wind_ms': 1, 'symbol': 'snow', 'precip_mm': amount})


class MetCoverageTests(unittest.TestCase):
    def summarize(self, steps, start='10:00', date='2026-11-15', duration=180):
        with patch.object(met, 'timeseries', return_value=(steps, 'checked')), \
             patch.object(met, 'timezone_for', return_value=timezone.utc):
            return met.get_met_hike_window(0, 0, date, start, duration)

    def test_missing_beginning_end_and_middle(self):
        steps = [met_step(f'2026-11-15T{h}:00:00+00:00') for h in (10, 11, 12)]
        for omitted in range(3):
            with self.subTest(omitted=omitted):
                result = self.summarize([s for i, s in enumerate(steps) if i != omitted])
                self.assertFalse(result['available'])
                self.assertNotIn('summary', result)
                self.assertNotIn('window', result)

    def test_full_unsorted_exact_coverage(self):
        result = self.summarize([met_step(f'2026-11-15T{h}:00:00+00:00') for h in (12, 10, 11)])
        self.assertEqual(result['window'], '10:00-13:00')
        self.assertEqual(result['min_temp_f'], 5)  # -15 Celsius, correctly converted.
        self.assertEqual(result['max_precip_mm'], 3)

    def test_overlap_can_cover_but_must_not_double_count_rain(self):
        result = self.summarize([met_step('2026-11-15T09:00:00+00:00', 3),
                                 met_step('2026-11-15T11:00:00+00:00', 3)])
        self.assertNotIn('available', result)
        self.assertIsNone(result['max_precip_mm'])
        self.assertIn('unknown for this exact window', result['summary'])

    def test_six_hour_period_covers_without_inventing_subhour_rain(self):
        result = self.summarize([met_step('2026-11-15T09:00:00+00:00', 6, 6)])
        self.assertNotIn('available', result)
        self.assertIsNone(result['max_precip_mm'])
        self.assertIsNone(result['min_temp_f'])  # 09:00 instant is outside the 10:00-13:00 hike.
        self.assertIsNone(result['max_wind_mph'])
        self.assertIn('temperature samples unavailable', result['summary'])

    def test_final_instant_can_supply_temperature_but_no_extra_interval(self):
        start = met_step('2026-11-15T10:00:00+00:00', 3)
        endpoint = met_step('2026-11-15T12:00:00+00:00', 0)
        endpoint[2]['temp_c'] = -20
        result = self.summarize([start, endpoint])
        self.assertEqual(result['min_temp_f'], -4)
        self.assertFalse(self.summarize([start, endpoint], duration=181)['available'])

    def test_midnight_offsets_and_exact_boundary(self):
        steps = [met_step('2026-11-15T18:00:00-05:00', 1), met_step('2026-11-16T00:00:00+00:00', 2)]
        result = self.summarize(steps, start='23:00')
        self.assertEqual(result['window'], '23:00-02:00')
        self.assertEqual(result['max_precip_mm'], 2)

    def test_instant_only_is_not_a_forecast_interval(self):
        payload = {'timeseries': [
            {'time': '2026-11-15T10:00:00Z', 'data': {'instant': {'details': {'air_temperature': -15}}}},
            {'time': '2026-11-15T16:00:00Z', 'data': {'next_6_hours': {'summary': {'symbol_code': 'fair_day'},
                      'details': {'precipitation_amount': 2}}}},
            {'time': '2026-11-16T00:00:00Z', 'data': {'next_12_hours': {'summary': {'symbol_code': 'fair_day'}}}},
        ], 'checked_at': 'checked'}
        with patch.object(met, 'cache_get', return_value=payload):
            steps, _ = met.timeseries(0, 0)
        self.assertEqual([s[1] for s in steps], [0, 6, 12])
        result = self.summarize(steps)
        self.assertFalse(result['available'])

    def test_instant_only_last_point_does_not_extend_forecast(self):
        result = self.summarize([met_step('2026-11-15T10:00:00+00:00', 1),
                                 met_step('2026-11-15T11:00:00+00:00', 0)])
        self.assertFalse(result['available'])

    def test_missing_amount_is_unknown_not_zero(self):
        result = self.summarize([met_step('2026-11-15T10:00:00+00:00', 3, None)])
        self.assertIsNone(result['max_precip_mm'])

    def test_partial_daily_forecast_is_unavailable(self):
        with patch.object(met, 'timeseries', return_value=([met_step('2026-11-15T10:00:00+00:00')], 'checked')), \
             patch.object(met, 'timezone_for', return_value=timezone.utc):
            result = met.get_met_weather(0, 0, '2026-11-15')
        self.assertFalse(result['available'])
        self.assertNotIn('summary', result)
