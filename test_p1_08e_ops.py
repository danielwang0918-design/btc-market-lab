"""Synthetic fixtures and local bare-Git remotes, not seven-day observations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import json

import test_p1_08d_receiver as fixture
import p1_08e_ops as ops


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.ReceiverTests('test_closed_evidence_and_raw_receipt_link_to_original_registry')
        self.case.setUp();self.addCleanup(self.case.doCleanups);self.case.collect()
        self.base=self.case.root.parent;self.remote=self.base/'external.git'
        subprocess.run(['git','init','--bare','--quiet',str(self.remote)],check=True)
        self.snapshot=self.base/'seed.zip';self.case.r.snapshot(self.snapshot)
        self.root=self.base/'supervisor';self.clock=lambda:self.case.now
        self.o=ops.Supervisor.create(self.root,remote=str(self.remote),branch='research/p1-08e-test-archive',
            snapshot=self.snapshot,snapshot_sha256=fixture.sha_file(self.snapshot),checkpoint=self.case.r.checkpoint(),
            mode='synthetic',clock=self.clock,opener=self.case.open_it)

    def test_fresh_remote_head_and_parent_checkpoint_required(self):
        head=self.o.head();self.case.now+=1000
        result=self.o.cycle(expected_head=head)
        self.assertEqual(result['parent_head'],head);self.assertNotEqual(result['published_head'],head)
        self.assertTrue(result['remote_readback_verified']);self.assertFalse(result['external_timestamp_attestation_verified'])

    def test_stale_parent_rejected_before_market_request(self):
        head=self.o.head();self.o.cycle(expected_head=head);n=len(self.case.calls)
        with self.assertRaises(ValueError):self.o.cycle(expected_head=head)
        self.assertEqual(len(self.case.calls),n)

    def test_lease_prevents_second_collector_before_io(self):
        head=self.o.head();lease=self.o.reserve(expected_head=head);n=len(self.case.calls)
        other=ops.Supervisor.restore(self.base/'other',remote=str(self.remote),branch=self.o.branch,expected_head=lease,
            mode='synthetic',clock=self.clock,opener=self.case.open_it)
        with self.assertRaises(ValueError):other.cycle(expected_head=lease)
        self.assertEqual(len(self.case.calls),n)

    def test_low_disk_stops_before_api_or_lease(self):
        head=self.o.head();n=len(self.case.calls)
        with patch.object(ops.shutil,'disk_usage',return_value=type('Disk',(),{'free':1})()):
            with self.assertRaises(ValueError):self.o.cycle(expected_head=head)
        self.assertEqual(self.o.head(),head);self.assertEqual(len(self.case.calls),n)

    def test_external_publish_failure_stops_and_is_logged(self):
        head=self.o.head()
        with patch.object(self.o.archive,'publish',side_effect=ValueError('synthetic remote failure')):
            with self.assertRaises(ValueError):self.o.cycle(expected_head=head)
        self.assertTrue(list((self.root/'diagnostics').glob('*.json')))

    def test_readback_failure_keeps_lease_and_prevents_more_collection(self):
        head=self.o.head()
        with patch.object(self.o.archive,'independent_readback',side_effect=ValueError('synthetic readback failure')):
            with self.assertRaises(ValueError):self.o.cycle(expected_head=head)
        catalog=self.o.archive.catalog(self.o.head());self.assertIsNotNone(catalog['lease'])

    def test_restore_from_remote_preserves_raw_ids_and_times(self):
        head=self.o.head();cp=self.o.receiver().checkpoint()
        r=ops.Supervisor.restore(self.base/'restored',remote=str(self.remote),branch=self.o.branch,expected_head=head,
            mode='synthetic',clock=self.clock,opener=self.case.open_it)
        self.assertEqual(r.receiver().checkpoint(),cp)
        self.assertEqual(r.receiver().registry._records(),self.o.receiver().registry._records())

    def test_local_raw_drift_stops_before_api(self):
        f=next((self.root/'state/receiver/batches').glob('*/*.body'));f.chmod(0o644);f.write_bytes(b'bad')
        n=len(self.case.calls)
        with self.assertRaises(ValueError):self.o.cycle(expected_head=self.o.head())
        self.assertEqual(len(self.case.calls),n)

    def test_unpublished_local_receiver_advance_cannot_be_silently_adopted(self):
        head=self.o.head();self.case.now+=1000;self.o.receiver().collect(include_liquidity=False);n=len(self.case.calls)
        with self.assertRaises(ValueError):self.o.cycle(expected_head=head)
        self.assertEqual(len(self.case.calls),n)

    def test_monitor_survives_corrupt_raw_and_does_not_report_zero_as_known(self):
        import p1_08e_monitor as monitor
        f=next((self.root/'state/receiver/batches').glob('*/*.body'));f.chmod(0o644);f.write_bytes(b'corrupt')
        r=monitor.daily_report(self.o,'2030-01-01')
        self.assertFalse(r['continue_conditions_met'])
        self.assertIn('raw_integrity_mismatch',[x['kind'] for x in r['alerts']])
        self.assertIsNone(r['feeds'][0]['confirmed'])

    def test_monitor_exposes_low_disk_and_unresolved_incidents(self):
        import p1_08e_monitor as monitor
        self.o.diagnostic('backup_failed','synthetic failure')
        with patch.object(ops.shutil,'disk_usage',return_value=type('Disk',(),{'free':1})()):
            r=monitor.daily_report(self.o,'2030-01-01')
        kinds=[x['kind'] for x in r['alerts']]
        self.assertIn('low_disk',kinds);self.assertIn('unresolved_local_incidents',kinds)
        self.assertFalse(r['continue_conditions_met'])

    def test_pilot_requires_explicit_durable_host_and_is_not_auto_started(self):
        with self.assertRaises(ValueError):self.o.start_trial(expected_head=self.o.head(),operator_id='fixture',durable_host='test',persistence_confirmed=False)
        self.assertEqual(self.o.status()['seven_day_status'],'not_started')

    def test_explicit_activation_uses_actual_clock_not_old_market_time(self):
        head=self.o.head();self.case.now+=1000
        self.o.start_trial(expected_head=head,operator_id='SYNTHETIC',durable_host='fixture-only',persistence_confirmed=True)
        cat=self.o.archive.catalog(self.o.head())
        self.assertEqual(cat['trial_started_at'],fixture.utc(self.case.now))
        self.assertFalse(self.o.status()['level2_pass'])

    def test_live_local_pid_cannot_be_claimed_dead_in_real_recovery(self):
        import os,socket
        self.o.local['mode']='real_public'
        fake=dict(lease=dict(pid=os.getpid(),host=socket.gethostname()))
        with patch.object(self.o,'_guard',return_value=fake),patch.object(self.o,'_finish',return_value=('unexpected',{})):
            with self.assertRaises(ValueError):self.o.recover_stopped(expected_head='a'*40,death_evidence=dict(process_dead=True,returncode=-9,pid=os.getpid()))

    def test_unreviewed_http_failure_is_archived_but_stops_continuation(self):
        self.case.failure='network';head=self.o.head()
        result=self.o.cycle(expected_head=head)
        self.assertEqual(result['status'],'blocked_quality')
        with self.assertRaises(ValueError):self.o.cycle(expected_head=result['published_head'])

    def test_original_data_and_seven_day_status_are_preserved(self):
        records=self.o.receiver().registry._records();head=self.o.head();self.o.cycle(expected_head=head)
        self.assertEqual(self.o.receiver().registry._records(),records)
        self.assertEqual(self.o.status()['seven_day_status'],'not_started')
        self.assertFalse(self.o.status()['level2_pass'])

    def test_unsafe_branch_and_existing_root_refused(self):
        with self.assertRaises(ValueError):ops.GitArchive(self.base/'bad-cache',str(self.remote),'main')
        with self.assertRaises(ValueError):ops.Supervisor.restore(self.root,remote=str(self.remote),branch=self.o.branch,expected_head=self.o.head(),mode='synthetic',clock=self.clock,opener=self.case.open_it)

    def test_unverified_process_death_cannot_clear_lease(self):
        head=self.o.reserve(expected_head=self.o.head())
        with self.assertRaises(ValueError):self.o.recover_stopped(expected_head=head,death_evidence={'process_dead':False})

    def test_verified_stopped_worker_is_recovered_without_resetting_data(self):
        cp=self.o.receiver().checkpoint();head=self.o.reserve(expected_head=self.o.head())
        result=self.o.recover_stopped(expected_head=head,death_evidence={'process_dead':True,'returncode':-9,'stopped_at':fixture.utc(self.case.now),'reason':'SYNTHETIC fixture, not real drill'})
        self.assertEqual(self.o.receiver().checkpoint(),cp);self.assertIsNone(self.o.archive.catalog(result)['lease'])


if __name__=='__main__':unittest.main()
