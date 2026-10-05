"""Recurrence expansion. Each acceptance criterion keeps its own class."""

import pytest

from ics_cal_mcp.errors import InvalidRangeError
from ics_cal_mcp.ics.expand import AllDay, day_window, expand_events, range_window

from conftest import TZ, instance_of, instances_on, parsed, start_ms, uids_on, utc_ms


class TestAC1RruleExpansion:
    def test_weekly_monday_lands_on_mondays_only(self):
        assert "weekly-mon@fixture" in uids_on("2026-03-02")
        assert "weekly-mon@fixture" not in uids_on("2026-03-03")
        assert "weekly-mon@fixture" in uids_on("2026-03-09")

    def test_keeps_0900_local_wall_time_across_the_dst_change(self):
        before = instance_of("2026-03-23", "weekly-mon@fixture")
        after = instance_of("2026-03-30", "weekly-mon@fixture")
        assert before is not None and after is not None
        assert not before.is_all_day
        assert start_ms(before) == utc_ms("2026-03-23T08:00:00Z")
        assert start_ms(after) == utc_ms("2026-03-30T07:00:00Z")

    def test_biweekly_keeps_its_phase(self):
        assert "biweekly-tue@fixture" in uids_on("2026-03-03")
        assert "biweekly-tue@fixture" not in uids_on("2026-03-10")
        assert "biweekly-tue@fixture" in uids_on("2026-03-17")
        assert "biweekly-tue@fixture" in uids_on("2026-03-31")

    def test_until_in_utc_boundary_occurrence_included_later_ones_not(self):
        # UNTIL=20260414T080000Z == 2026-04-14 10:00 CEST, the occurrence instant.
        boundary = instance_of("2026-04-14", "biweekly-tue@fixture")
        assert boundary is not None
        assert start_ms(boundary) == utc_ms("2026-04-14T08:00:00Z")
        assert "biweekly-tue@fixture" not in uids_on("2026-04-28")

    def test_monthly_2nd_friday_lands_on_the_2nd_friday(self):
        assert "monthly-2fr@fixture" in uids_on("2026-03-13")
        assert "monthly-2fr@fixture" not in uids_on("2026-03-06")
        assert "monthly-2fr@fixture" in uids_on("2026-04-10")
        assert "monthly-2fr@fixture" in uids_on("2026-07-10")


class TestAC2Exdate:
    def test_skips_excluded_instances_multi_value_tzid_qualified_folded(self):
        assert "weekly-exdate@fixture" in uids_on("2026-03-02")
        assert "weekly-exdate@fixture" not in uids_on("2026-03-09")
        assert "weekly-exdate@fixture" in uids_on("2026-03-16")
        assert "weekly-exdate@fixture" not in uids_on("2026-03-23")
        assert "weekly-exdate@fixture" not in uids_on("2026-04-06")
        assert "weekly-exdate@fixture" in uids_on("2026-04-13")


class TestAC3RecurrenceIdOverrides:
    def test_an_override_replaces_the_base_occurrence_on_the_same_day_exactly_once(self):
        found = [i for i in instances_on("2026-03-18") if i.event.uid == "weekly-ovr@fixture"]
        assert len(found) == 1
        inst = found[0]
        assert not inst.is_all_day
        assert inst.is_recurring
        assert start_ms(inst) == utc_ms("2026-03-18T14:00:00Z")  # 15:00 CET, not 13:00
        assert inst.event.summary == "Team sync (moved)"
        assert inst.event.location == "Zasedačka C"

    def test_other_weeks_of_the_overridden_series_stay_untouched(self):
        normal = instance_of("2026-03-11", "weekly-ovr@fixture")
        assert normal is not None
        assert start_ms(normal) == utc_ms("2026-03-11T12:00:00Z")
        assert normal.event.summary == "Team sync"

    def test_an_override_moved_across_days_disappears_from_old_day_appears_once_on_new(self):
        assert "weekly-ovr-moved@fixture" not in uids_on("2026-03-25")
        moved = [i for i in instances_on("2026-03-26") if i.event.uid == "weekly-ovr-moved@fixture"]
        assert len(moved) == 1
        assert start_ms(moved[0]) == utc_ms("2026-03-26T08:00:00Z")
        assert moved[0].event.summary == "Product check-in (rescheduled)"

    def test_never_returns_the_same_uid_and_slot_twice_across_a_week_window(self):
        win = range_window("2026-03-23", "2026-03-29", TZ)
        moved = [
            i for i in expand_events(parsed(), win) if i.event.uid == "weekly-ovr-moved@fixture"
        ]
        assert len(moved) == 1


class TestAC5AllDayEvents:
    def test_a_one_day_all_day_event_appears_on_its_day_only(self):
        assert "allday-1@fixture" in uids_on("2026-03-16")
        assert "allday-1@fixture" not in uids_on("2026-03-17")
        inst = instance_of("2026-03-16", "allday-1@fixture")
        assert inst is not None and isinstance(inst.time, AllDay)
        assert inst.time.start.isoformat() == "2026-03-16"
        assert inst.time.end.isoformat() == "2026-03-17"

    def test_a_multi_day_all_day_event_appears_on_each_covered_day_not_the_exclusive_end(self):
        for day in ("2026-03-16", "2026-03-17", "2026-03-18"):
            assert "allday-multi@fixture" in uids_on(day)
        assert "allday-multi@fixture" not in uids_on("2026-03-19")


class TestOverlapSemantics:
    def test_a_midnight_spanning_event_appears_on_both_days(self):
        assert "midnight@fixture" in uids_on("2026-03-20")
        assert "midnight@fixture" in uids_on("2026-03-21")

    def test_an_event_ending_exactly_at_midnight_belongs_to_the_previous_day_only(self):
        assert "midnight-end@fixture" in uids_on("2026-03-20")
        assert "midnight-end@fixture" not in uids_on("2026-03-21")


class TestAC4TimezoneHandling:
    def test_interprets_the_second_windows_tzid_name_correctly(self):
        inst = instance_of("2026-03-18", "cest-tzid@fixture")
        assert inst is not None
        assert start_ms(inst) == utc_ms("2026-03-18T15:00:00Z")  # 16:00 CET

    def test_interprets_an_unknown_tzid_in_tz_default_not_the_system_zone(self):
        inst = instance_of("2026-03-19", "unknown-tzid@fixture")
        assert inst is not None
        assert start_ms(inst) == utc_ms("2026-03-19T08:00:00Z")  # 09:00 Prague


class TestWindows:
    def test_day_window_covers_exactly_one_local_day_23h_on_the_spring_dst_day(self):
        dst = day_window("2026-03-29", TZ, 0)
        assert dst.end_ms - dst.start_ms == 23 * 3_600_000
        normal = day_window("2026-03-30", TZ, 0)
        assert normal.end_ms - normal.start_ms == 24 * 3_600_000

    def test_day_window_is_25h_on_the_autumn_dst_day(self):
        dst = day_window("2026-10-25", TZ, 0)
        assert dst.end_ms - dst.start_ms == 25 * 3_600_000

    def test_range_window_accepts_31_days_and_rejects_32(self):
        range_window("2026-03-01", "2026-03-31", TZ)
        with pytest.raises(InvalidRangeError, match="maximum is 31"):
            range_window("2026-03-01", "2026-04-01", TZ)

    def test_rejects_from_after_to_and_invalid_calendar_dates(self):
        with pytest.raises(InvalidRangeError) as exc:
            range_window("2026-03-02", "2026-03-01", TZ)
        assert exc.value.code == "INVALID_RANGE"
        with pytest.raises(InvalidRangeError, match="valid calendar date"):
            day_window("2026-02-30", TZ, 0)


class TestResultOrdering:
    def test_sorts_by_start_with_all_day_events_first(self):
        summaries = [i.event.summary for i in instances_on("2026-03-16")]
        assert summaries == [
            "Dovolená",  # all-day, tie with Školení broken by summary
            "Školení",
            "Weekly standup",  # 09:00
            "Q3 review, part 1; plán",  # 10:00 (unescaped \, and \;)
            "Architecture board",  # 11:00
        ]
