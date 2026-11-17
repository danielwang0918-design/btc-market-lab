"""Test-only compatibility for a legacy assertion tied to yesterday's date.

The inherited P0 source/test files stay byte-identical. Only that one guard
case receives a scoped clock; production execution never imports this module.
"""
from datetime import datetime,timezone,timedelta
from functools import wraps
import unittest
from unittest import mock
import test_prospective_daily_archive as legacy


def fixture_clock(moment):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)
    return Clock


def install_legacy_guard_fixture():
    original=legacy.DailyArchiveTests.test_daily_future_rejected_before_fetch
    if getattr(original,'_p108b_scoped_clock',False):return
    @wraps(original)
    def with_prestart_clock(case):
        instant=datetime.fromtimestamp(legacy.START/1000,timezone.utc)-timedelta(seconds=1)
        with mock.patch.object(legacy.archive,'datetime',fixture_clock(instant)):
            return original(case)  # Original assertions and fetch stub unchanged.
    with_prestart_clock._p108b_scoped_clock=True
    legacy.DailyArchiveTests.test_daily_future_rejected_before_fetch=with_prestart_clock


# unittest discovery imports this fixture before the inherited archive suite.
# No production module imports test files; the clock is patched only per call.
install_legacy_guard_fixture()


class ClockFixtureTests(unittest.TestCase):
    def fixture(self):
        case=legacy.DailyArchiveTests('test_daily_future_rejected_before_fetch')
        case.setUp();self.addCleanup(case.doCleanups)
        return case

    def test_inherited_future_guard_remains_deterministic_and_restores_clock(self):
        case=self.fixture();before=legacy.archive.datetime
        case.test_daily_future_rejected_before_fetch()
        self.assertIs(legacy.archive.datetime,before)

    def test_cutoff_equal_to_now_is_valid_with_no_new_data_or_real_requests(self):
        case=self.fixture();case.seed_it()
        instant=datetime.fromtimestamp(legacy.START/1000,timezone.utc)
        with mock.patch.object(legacy.archive,'datetime',fixture_clock(instant)):
            result=legacy.archive.daily(case.dir,case.lock,legacy.START,fetcher=lambda *a,**k:())
        self.assertEqual(result['status'],'daily_data_archived_not_scored')
        self.assertEqual(result['new_closed_bars'],0)


if __name__=='__main__':unittest.main()
