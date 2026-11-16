import tempfile
import unittest
from pathlib import Path
from vas_bench.data import read_json
from vas_bench.evidence import snapshot, seal, verify
from vas_bench.storage import write_json

class EvidenceTests(unittest.TestCase):
    def fixture(self,root):
        rows=[{'id':'A'}]
        evidence=root/'evaluation/evidence/check.txt';evidence.parent.mkdir(parents=True)
        evidence.write_text('Observed check output')
        write_json(root/'evaluation/result.json',{'results':[{'id':'A','answer':'Yes','reason':'See evaluation/evidence/check.txt'}]})
        write_json(root/'evaluation/evidence.json',{'observations':[{'ids':['A'],'files':['evaluation/evidence/check.txt'],'note':'Observed state'}]})
        return rows
    def test_index_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows=self.fixture(root)
            digest=seal(root,rows);verify(root,digest,rows)
            (root/'evaluation/evidence/check.txt').write_text('Changed')
            with self.assertRaises(ValueError):verify(root,digest,rows)
    def test_missing_evidence_and_wrong_item_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows=self.fixture(root)
            (root/'evaluation/evidence/check.txt').write_text('')
            with self.assertRaises(ValueError):snapshot(root,rows)
            (root/'evaluation/evidence/check.txt').write_text('Restored')
            write_json(root/'evaluation/result.json',{'results':[{'id':'A','answer':'Yes','reason':'No cited evidence'}]})
            with self.assertRaises(ValueError):snapshot(root,rows)
