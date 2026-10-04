import io
import os
import select
import shutil
import sys
import tempfile
import time
import unicodedata
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rufus
from rich.console import Console
from rich.text import Text

try:
    import pty
except ImportError:  # not available on every platform
    pty = None

REPO = Path(__file__).resolve().parent.parent


class IsoTreeMixin:
    """A temporary folder holding "My ISOs/Win 11.iso", removed afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "My ISOs").mkdir()
        self.iso = self.tmp / "My ISOs" / "Win 11.iso"
        self.iso.touch()


class SanitizeInputTest(unittest.TestCase):
    CASES = [
        ("plain", "3", "3"),
        ("bracketed paste", "\x1b[200~3\x1b[201~", "3"),
        ("caret-echoed paste markers", "^[[200~/a/b.iso^[[201~", "/a/b.iso"),
        ("arrow keys", "abc\x1b[D\x1b[Cd", "abcd"),
        ("ss3 arrow keys", "abc\x1bOAd", "abcd"),
        ("trailing newline and CR", "yes\r\n", "yes"),
        ("tab becomes space", "a\tb", "a b"),
        ("other control chars", "a\x00b\x07c\x7f", "abc"),
        ("NFD to NFC", "Español", "Español"),
        ("surrounding whitespace", "  y  ", "y"),
        ("only garbage", "\x1b[200~\x1b[201~", ""),
        ("libedit-eaten paste markers", "00~/a/b.iso01~", "/a/b.iso"),
        ("libedit remnants with padding", " 00~2 01~\r\n", "2"),
        ("00~ inside text kept", "/a/x00~y.iso", "/a/x00~y.iso"),
        ("lone leading 00~ kept", "00~DATA", "00~DATA"),
        ("lone trailing 01~ kept", "/Volumes/stick/win01~", "/Volumes/stick/win01~"),
        # Accepted trade-off: a paired 00~ … 01~ is indistinguishable from
        # libedit's paste remnants, so it is always removed.
        ("paired remnant-like text is stripped", "00~BACKUP01~", "BACKUP"),
    ]

    def test_cases(self):
        for name, raw, expected in self.CASES:
            with self.subTest(name):
                self.assertEqual(rufus.sanitize_input(raw), expected)


class GetInputTest(unittest.TestCase):
    """Prompt.get_input with a fake console, no terminal involved."""

    def setUp(self):
        self.readline = unittest.mock.Mock()
        self.input = unittest.mock.Mock(return_value="2")
        for patcher in (unittest.mock.patch.dict(sys.modules, {"readline": self.readline}),
                        unittest.mock.patch("builtins.input", self.input)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _console(is_terminal: bool) -> Console:
        return Console(file=io.StringIO(), force_terminal=is_terminal)

    def test_stream_answer_is_sanitized(self):
        answer = rufus.Prompt.get_input(self._console(False), "", False,
                                        stream=io.StringIO("\x1b[200~3\x1b[201~\n"))
        self.assertEqual(answer, "3")

    def test_no_escape_sequence_when_not_a_terminal(self):
        console = self._console(False)
        rufus.Prompt.get_input(console, "", False)
        self.assertNotIn(rufus._BRACKETED_PASTE_OFF, console.file.getvalue())

    def test_bracketed_paste_turned_off_on_a_terminal(self):
        console = self._console(True)
        rufus.Prompt.get_input(console, "", False)
        self.assertIn(rufus._BRACKETED_PASTE_OFF, console.file.getvalue())

    def test_line_editing_reads_with_plain_prompt_and_no_history(self):
        answer = rufus.Prompt.get_input(self._console(False), Text("Pick", style="bold cyan"), False)
        self.assertEqual(answer, "2")
        self.input.assert_called_once_with("Pick")
        self.readline.set_auto_history.assert_called_once_with(False)

    def test_line_editing_prints_leading_lines_itself(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rufus.Prompt.get_input(self._console(False), Text("\nPath to ISO file: "), False)
        self.input.assert_called_once_with("Path to ISO file: ")
        self.assertEqual(stdout.getvalue(), "\n")

    def test_password_passes_through_unchanged(self):
        with unittest.mock.patch("getpass.getpass", return_value=" pa\tss "):
            answer = rufus.Prompt.get_input(self._console(False), "", True)
        self.assertEqual(answer, " pa\tss ")
        self.input.assert_not_called()


class ParsePathInputTest(IsoTreeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.other = self.tmp / "other.iso"
        self.other.touch()

    def assertFound(self, raw, *paths):
        self.assertEqual(rufus.parse_path_input(raw), rufus.PathInput(list(paths), None))

    def assertNotFound(self, raw, guess):
        self.assertEqual(rufus.parse_path_input(raw), rufus.PathInput([], guess))

    def test_ways_of_entering_an_existing_path(self):
        iso = str(self.iso)
        escaped = iso.replace(" ", "\\ ")
        cases = [
            ("typed as-is", iso),
            ("Finder drag-and-drop escapes", escaped),
            ("drag-and-drop trailing space", escaped + " "),
            ("double quoted", f'"{iso}"'),
            ("single quoted", f"'{iso}'"),
            ("bracketed paste", f"\x1b[200~{iso}\x1b[201~"),
            ("file URL", "file://" + iso.replace(" ", "%20")),
            ("file://localhost URL", "file://localhost" + iso.replace(" ", "%20")),
            ("unbalanced quote", f'"{iso}'),
        ]
        for name, raw in cases:
            with self.subTest(name):
                self.assertFound(raw, self.iso)

    def test_real_backslash_in_name_kept(self):
        odd = self.tmp / "odd\\name.iso"
        odd.touch()
        self.assertFound(str(odd), odd)

    def test_file_url_with_literal_hash(self):
        hashed = self.tmp / "Win #11.iso"
        hashed.touch()
        self.assertFound(f"file://{hashed}", hashed)

    def test_name_stored_as_nfd_on_a_non_normalizing_share(self):
        nfd = Path(unicodedata.normalize("NFD", "/share/Español.iso"))
        with unittest.mock.patch.object(Path, "exists", lambda path: str(path) == str(nfd)), \
                unittest.mock.patch.object(Path, "resolve", lambda path: path):
            self.assertFound("/share/Español.iso", nfd)

    def test_several_dropped_files(self):
        escaped = str(self.iso).replace(" ", "\\ ")
        self.assertFound(f"{escaped} {self.other}", self.iso, self.other)

    def test_missing_escaped_path_reports_unescaped_path(self):
        missing = self.tmp / "nope dir" / "x.iso"
        self.assertNotFound(str(missing).replace(" ", "\\ "), missing)

    def test_missing_unquoted_path_with_spaces_reported_whole(self):
        typo = self.tmp / "My ISOs" / "Win 11.isp"
        self.assertNotFound(str(typo), typo)

    def test_unknown_user_home_is_not_found_rather_than_a_crash(self):
        for raw in ["~nosuchuser_rufus/x.iso", "~00~"]:
            with self.subTest(raw):
                self.assertNotFound(raw, Path(raw))

    def test_nothing_to_read(self):
        for name, raw in [("paste markers only", "  \x1b[200~\x1b[201~ "),
                          ("empty single quotes", "''"), ("empty double quotes", '""')]:
            with self.subTest(name):
                self.assertEqual(rufus.parse_path_input(raw), rufus.PathInput([], None))


class AskIsoPathTest(IsoTreeMixin, unittest.TestCase):
    """ask_iso_path's branches, with the prompt's answers scripted."""

    def _ask(self, *answers):
        output = Console(file=io.StringIO(), width=500)
        with unittest.mock.patch.object(rufus.Prompt, "ask", side_effect=answers), \
                unittest.mock.patch.object(rufus, "console", output):
            chosen = rufus.ask_iso_path()
        return chosen, output.file.getvalue()

    def test_valid_path_accepted(self):
        self.assertEqual(self._ask(str(self.iso))[0], self.iso)

    def test_empty_answer_gets_a_hint(self):
        chosen, out = self._ask("", str(self.iso))
        self.assertEqual(chosen, self.iso)
        self.assertIn("drag it here from Finder", out)

    def test_missing_path_reported_with_repr(self):
        chosen, out = self._ask(str(self.tmp / "nope.iso"), str(self.iso))
        self.assertEqual(chosen, self.iso)
        self.assertIn(f"Not found: '{self.tmp / 'nope.iso'}'", out)

    def test_folder_rejected(self):
        chosen, out = self._ask(str(self.tmp / "My ISOs"), str(self.iso))
        self.assertEqual(chosen, self.iso)
        self.assertIn("That's a folder", out)

    def test_iso_preferred_among_dropped_items(self):
        notes = self.tmp / "notes.txt"
        notes.touch()
        escaped = str(self.iso).replace(" ", "\\ ")
        chosen, out = self._ask(f"{notes} {escaped}")
        self.assertEqual(chosen, self.iso)
        self.assertIn("2 items dropped", out)

    def test_non_iso_file_accepted_with_warning(self):
        image = self.tmp / "disk.img"
        image.touch()
        chosen, out = self._ask(str(image))
        self.assertEqual(chosen, image)
        self.assertIn("doesn't end in .iso", out)


@unittest.skipUnless(pty is not None, "needs a pty")
class PromptPtyTest(IsoTreeMixin, unittest.TestCase):
    """Drives the real prompts in a pseudo-terminal with line editing on,
    sending the bytes a terminal sends for a paste, a Finder drop or a key."""

    def _run(self, code: str, *steps: tuple[bytes, str]) -> bytes:
        """Run `code` in a child on a pty; for each (wait_for, payload) step,
        wait until `wait_for` is printed again, then type `payload`."""
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(REPO)
                os.execv(sys.executable, [
                    sys.executable, "-c",
                    f"import sys; sys.path.insert(0, {str(REPO)!r}); import rufus; "
                    f"rufus.enable_line_editing(); {code}; print('DONE')"])
            finally:
                os._exit(127)  # never fall back into the parent's test run
        out, pending, scanned_to, deadline = b"", list(steps), 0, time.monotonic() + 15
        try:
            while time.monotonic() < deadline and b"DONE" not in out:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                if pending and pending[0][0] in out[scanned_to:]:
                    # With line editing on, readline draws the prompt itself,
                    # so seeing it means the terminal is ready for input.
                    os.write(fd, pending.pop(0)[1].encode())
                    scanned_to = len(out)
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)
        return out

    def test_iso_path_prompt(self):
        iso = str(self.iso)
        payloads = [
            iso + "\r",                                     # typed
            iso.replace(" ", "\\ ") + " \r",                # Finder drop
            f"\x1b[200~{iso}\x1b[201~\r",                   # bracketed paste
            # Types "…Win 11.io", moves left one, inserts the missing "s".
            # Without line editing the arrow is dropped and "…Win 11.ios" fails.
            iso[:-2] + iso[-1] + "\x1b[D" + iso[-2] + "\r",
        ]
        code = f"[print('RESULT=' + str(rufus.ask_iso_path())) for _ in range({len(payloads)})]"
        out = self._run(code, *[(b"Path to ISO", payload) for payload in payloads])
        self.assertEqual(out.count(f"RESULT={iso}".encode()), len(payloads), out)
        self.assertIn(rufus._BRACKETED_PASTE_OFF.encode(), out)

    def test_menu_number_and_confirm_paste(self):
        drives = [{"node": f"/dev/disk{n}", "name": "Stick", "size": 8 * 1024 ** 3, "protocol": "USB"}
                  for n in (4, 5)]
        code = (f"print('RESULT=' + rufus.ask_usb({drives!r})['node']); "
                "print('RESULT=' + repr(rufus.Confirm.ask('Proceed?', default=False)))")
        out = self._run(code, (b"Select USB drive number", "\x1b[200~2\x1b[201~\r"),
                        (b"Proceed?", "\x1b[200~y\x1b[201~\r"))
        self.assertIn(b"RESULT=/dev/disk5", out)
        self.assertIn(b"RESULT=True", out)

    def test_up_arrow_cannot_recall_an_earlier_answer(self):
        code = ("rufus.Prompt.ask('First'); "
                "print('RESULT=' + repr(rufus.Confirm.ask('Proceed?', default=False)))")
        out = self._run(code, (b"First", "y\r"), (b"Proceed?", "\x1b[A\r"))
        self.assertIn(b"RESULT=False", out)


if __name__ == "__main__":
    unittest.main()
