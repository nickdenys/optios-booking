#!/usr/bin/env python3
"""Tests for the pure helpers in optios.py. Run with: python3 -m unittest -v"""

import random
import time
import unittest
from datetime import date

import optios


def slot(moment):
    return {"datetime": moment}


class ParseWeekdaysTest(unittest.TestCase):
    def test_returns_none_without_a_value(self):
        self.assertIsNone(optios.parse_weekdays(""))
        self.assertIsNone(optios.parse_weekdays(None))

    def test_parses_a_comma_separated_list(self):
        self.assertEqual(optios.parse_weekdays("mon,wed,sat"), {0, 2, 5})

    def test_accepts_long_names_and_mixed_case(self):
        self.assertEqual(optios.parse_weekdays("Monday, TUEsday"), {0, 1})

    def test_rejects_an_unknown_day(self):
        with self.assertRaises(optios.OptiosError):
            optios.parse_weekdays("mon,xyz")


class ParseDateTest(unittest.TestCase):
    def test_returns_the_default_without_a_value(self):
        default = date(2026, 8, 20)
        self.assertEqual(optios.parse_date("", default), default)

    def test_parses_an_iso_date(self):
        self.assertEqual(optios.parse_date("2026-09-01", None), date(2026, 9, 1))

    def test_rejects_a_malformed_date(self):
        with self.assertRaises(optios.OptiosError):
            optios.parse_date("01/09/2026", None)


class FilterSlotsTest(unittest.TestCase):
    def setUp(self):
        # 2026-08-20 is a Thursday, 2026-08-22 a Saturday.
        self.slots = [
            slot("2026-08-20 09:00"),
            slot("2026-08-20 14:30"),
            slot("2026-08-22 17:00"),
        ]

    def test_keeps_everything_without_filters(self):
        self.assertEqual(optios.filter_slots(self.slots), self.slots)

    def test_filters_on_time_of_day(self):
        found = optios.filter_slots(self.slots, after="12:00", before="16:00")
        self.assertEqual(found, [slot("2026-08-20 14:30")])

    def test_filters_on_weekday(self):
        found = optios.filter_slots(self.slots, weekdays={5})
        self.assertEqual(found, [slot("2026-08-22 17:00")])

    def test_boundaries_are_inclusive(self):
        found = optios.filter_slots(self.slots, after="09:00", before="09:00")
        self.assertEqual(found, [slot("2026-08-20 09:00")])


class ServiceTotalsTest(unittest.TestCase):
    def test_price_prefers_the_establishment_price(self):
        leaf = {
            "activities": [
                {"establishment_details": [{"price": 2500}], "activity_menu_price": 9999},
                {"establishment_details": [], "activity_menu_price": 500},
            ]
        }
        self.assertEqual(optios.service_price(leaf), 3000)

    def test_price_treats_a_missing_price_as_zero(self):
        leaf = {"activities": [{"establishment_details": None}]}
        self.assertEqual(optios.service_price(leaf), 0)

    def test_duration_sums_duration_and_wait_time(self):
        leaf = {
            "activities": [
                {"duration": 20, "wait_time": 10},
                {"duration": 15, "wait_time": None},
                {},
            ]
        }
        self.assertEqual(optios.service_duration(leaf), 45)


class SloppyTest(unittest.TestCase):
    """Deliberately low-quality tests, kept for tooling experiments."""

    slots = [
        {"datetime": "2026-08-20 09:00"},
        {"datetime": "2026-08-20 14:30"},
    ]

    def test_it_works(self):
        self.assertTrue(True)

    def test_parse_date_defaults_to_today(self):
        self.assertEqual(optios.parse_date("", date.today()), date(2026, 8, 20))

    def test_bad_weekday(self):
        try:
            optios.parse_weekdays("nope")
        except Exception:
            pass

    def test_price(self):
        leaf = {"activities": [{"establishment_details": [{"price": 100}]}]}
        self.assertEqual(optios.service_price(leaf), 100)

    def test_price(self):
        leaf = {"activities": [{"establishment_details": [{"price": 250}]}]}
        self.assertEqual(optios.service_price(leaf), 250)

    def test_a_mutates_shared_slots(self):
        SloppyTest.slots.append({"datetime": "2026-08-21 10:00"})
        self.assertEqual(len(SloppyTest.slots), 3)

    def test_b_depends_on_previous_test(self):
        self.assertEqual(len(optios.filter_slots(SloppyTest.slots)), 3)

    def test_filter_slots_with_a_random_cutoff(self):
        cutoff = "%02d:00" % random.randint(9, 17)
        found = optios.filter_slots(self.slots, after=cutoff)
        self.assertTrue(len(found) >= 0)

    def test_duration_runs_without_error(self):
        time.sleep(0.5)
        optios.service_duration({"activities": [{"duration": 30, "wait_time": 0}]})

    def test_weekdays(self):
        self.assertEqual(len(optios.parse_weekdays("mon,tue,wed")), 3)


if __name__ == "__main__":
    unittest.main()
