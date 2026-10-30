import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rufus


def _proc(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class SetMbrActivePartitionTest(unittest.TestCase):
    def _run(self, unmount_result):
        mbr = bytes(512)
        opener = mock.mock_open(read_data=mbr)
        with mock.patch.object(rufus, "run", return_value=unmount_result) as run_mock, \
             mock.patch("builtins.open", opener):
            rufus.set_mbr_active_partition("/dev/disk4")
        return run_mock, opener

    def test_unmount_is_forced(self):
        run_mock, _ = self._run(_proc())
        run_mock.assert_called_once_with(["diskutil", "unmountDisk", "force", "/dev/disk4"],
                                         check=False)

    def test_failed_unmount_is_logged(self):
        with self.assertLogs("rufus", level="WARNING") as cm:
            self._run(_proc(1, err="dissented by PID 1"))
        self.assertIn("dissented by PID 1", cm.output[0])

    def test_active_flag_written_to_first_entry(self):
        _, opener = self._run(_proc())
        written = opener().write.call_args[0][0]
        self.assertEqual(written[446], 0x80)
        self.assertEqual(written[462], 0x00)


if __name__ == "__main__":
    unittest.main()
