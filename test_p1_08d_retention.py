"""Synthetic remote metadata fixtures; not real external timestamp evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from test_p1_08d_receiver import ReceiverTests
from p1_08c_registry import sha_file
import p1_08d_retention as mod


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.case=ReceiverTests('test_closed_evidence_and_raw_receipt_link_to_original_registry')
        self.case.setUp();self.addCleanup(self.case.doCleanups);self.case.collect()
        self.root=self.case.root.parent;self.bundle=self.root/'bundle';mod.publish_bundle(self.case.r,self.bundle,context={'commit':'c'*40,'run_id':'123','run_attempt':'1'})
        self.archive=self.root/'github-artifact.zip'
        with ZipFile(self.archive,'x') as z:
            for p in self.bundle.iterdir():z.write(p,p.name)
        self.meta=dict(id=1,size_in_bytes=self.archive.stat().st_size,digest='sha256:'+sha_file(self.archive),expired=False,
            created_at='2030-01-01T02:01:00Z',updated_at='2030-01-01T02:02:00Z',
            workflow_run=dict(id=123,head_sha='c'*40,repository_id=1419214883,head_repository_id=1419214883))

    def verify(self,**changes):
        meta=dict(self.meta,**changes)
        return mod.verify_retained_bundle(self.archive,meta,self.root/'independent',expected_commit='c'*40,expected_run_id=123)

    def test_independent_digest_and_receiver_checkpoint_verified_but_no_notarization(self):
        r=self.verify();self.assertTrue(r['platform_artifact_digest_verified']);self.assertTrue(r['receiver_checkpoint_verified'])
        self.assertFalse(r['external_timestamp_attestation_verified']);self.assertEqual(r['mode'],'synthetic')
        self.assertEqual(r['candidate_versions'],0)

    def test_wrong_platform_digest_rejected(self):
        with self.assertRaises(ValueError):self.verify(digest='sha256:'+'b'*64)

    def test_wrong_repository_or_commit_rejected(self):
        for patch in [dict(head_sha='b'*40),dict(repository_id=1),dict(id=999)]:
            meta=dict(self.meta['workflow_run'],**patch)
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.verify(workflow_run=meta)

    def test_expired_or_wrong_size_rejected(self):
        for patch in [dict(expired=True),dict(size_in_bytes=1)]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.verify(**patch)

    def test_existing_output_refused(self):
        self.verify()
        with self.assertRaises(ValueError):self.verify()

    def test_original_bundle_not_modified_by_independent_verification(self):
        before=sha_file(self.archive);self.verify();self.assertEqual(before,sha_file(self.archive))


if __name__=='__main__':unittest.main()
