import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rufus


@unittest.skipUnless(shutil.which("wimlib-imagex"), "wimlib-imagex not installed")
class SplitSolidWimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        src = self.tmp / "src"
        src.mkdir()
        for i in range(3):
            (src / f"f{i}.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        self.solid = self.tmp / "solid.wim"
        self.flat = self.tmp / "flat.wim"
        subprocess.run(["wimlib-imagex", "capture", str(src), str(self.solid), "t", "--solid"],
                       check=True, capture_output=True)
        subprocess.run(["wimlib-imagex", "capture", str(src), str(self.flat), "t"],
                       check=True, capture_output=True)
        self.dst = self.tmp / "usb"
        self.dst.mkdir()
        self._orig = rufus._WIM_PART_MB
        rufus._WIM_PART_MB = 4  # force multiple parts for a tiny WIM

    def tearDown(self):
        rufus._WIM_PART_MB = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_split_would_fail_on_solid(self):
        code, _ = rufus._run_wimlib(["wimlib-imagex", "split", str(self.solid),
                                     str(self.tmp / "x.swm"), "4"])
        self.assertEqual(code, rufus._WIM_ERR_UNSUPPORTED)

    def test_solid_wim_falls_back_to_export(self):
        calls = []
        rufus.split_and_copy_wim(self.solid, self.dst, lambda ph, p: calls.append((ph, p)))
        parts = sorted((self.dst / "sources").glob("install*.swm"))
        self.assertGreater(len(parts), 1)
        self.assertIn("Re-compressing (solid WIM)", {ph for ph, _ in calls})
        self.assertEqual(calls[-1], ("Splitting", 100))
        info = subprocess.run(["wimlib-imagex", "info", str(parts[0])],
                              capture_output=True, text=True).stdout
        self.assertIn("LZX", info)

    def test_non_solid_wim_splits_directly(self):
        calls = []
        rufus.split_and_copy_wim(self.flat, self.dst, lambda ph, p: calls.append(ph))
        self.assertGreater(len(list((self.dst / "sources").glob("install*.swm"))), 1)
        self.assertNotIn("Re-compressing (solid WIM)", calls)

    def test_solid_hint(self):
        self.assertTrue(rufus.wim_may_be_solid(self.solid))
        self.assertFalse(rufus.wim_may_be_solid(self.flat))


if __name__ == "__main__":
    unittest.main()
