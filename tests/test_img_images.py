"""Regression coverage for raw .img selection, probing and download resumption."""

import json
from pathlib import Path

import pytest

import rufus


def test_img_probe_does_not_mount_and_selects_raw_linux_flow(tmp_path, monkeypatch):
    image = tmp_path / "tails-amd64-7.0.img"
    sector = bytearray(512)
    sector[450] = 0x83  # Linux partition
    sector[454:458] = (1).to_bytes(4, "little")
    sector[458:462] = (1).to_bytes(4, "little")
    sector[510:512] = b"\x55\xaa"
    image.write_bytes(sector + bytes(512))

    def fail_mount(_):
        pytest.fail("raw images must not be mounted for ISO inspection")

    monkeypatch.setattr(rufus, "mount_iso", fail_mount)
    info = rufus.probe_iso(image)

    assert info == {
        "kind": "linux", "hybrid": True, "uefi": False, "raw": True,
        "label": "tails-amd64-7.0", "size": 1024,
    }


@pytest.mark.parametrize("content", [b"", b"not an image", b"\0" * 512,
                                    b"\0" * 510 + b"\x55\xaa",
                                    b"\0" * 446 + b"\x01" + b"\0" * 63 + b"\x55\xaa"])
def test_invalid_raw_img_rejected_before_write(tmp_path, content):
    image = tmp_path / "invalid.img"
    image.write_bytes(content)
    with pytest.raises(ValueError, match="partition table|raw disk image"):
        rufus.probe_iso(image)


@pytest.mark.parametrize("partition_type,start,sectors,file_sectors", [
    (0, 1, 1, 2),       # no partition type
    (0x83, 0, 1, 2),    # partition starts in the MBR
    (0x83, 1, 0, 2),    # empty partition
    (0x83, 1, 2, 2),    # truncated image
    (0xEE, 1, 2, 2),    # truncated GPT image
])
def test_img_with_invalid_partition_rejected(tmp_path, partition_type, start, sectors, file_sectors):
    image = tmp_path / "bad-partition.img"
    data = bytearray(512 * file_sectors)
    data[450] = partition_type
    data[454:458] = start.to_bytes(4, "little")
    data[458:462] = sectors.to_bytes(4, "little")
    data[510:512] = b"\x55\xaa"
    image.write_bytes(data)
    with pytest.raises(ValueError, match="partition table"):
        rufus.probe_iso(image)


def test_gpt_raw_img_with_protective_mbr_is_accepted(tmp_path):
    image = tmp_path / "gpt.img"
    data = bytearray(4096)
    data[450] = 0xEE  # protective MBR for GPT
    data[454:458] = (1).to_bytes(4, "little")
    data[458:462] = (7).to_bytes(4, "little")
    data[510:512] = b"\x55\xaa"
    data[512:520] = b"EFI PART"
    image.write_bytes(data)
    assert rufus.probe_iso(image)["raw"] is True


def test_gpt_protective_mbr_without_gpt_header_is_rejected(tmp_path):
    image = tmp_path / "fake-gpt.img"
    data = bytearray(4096)
    data[450] = 0xEE
    data[454:458] = (1).to_bytes(4, "little")
    data[458:462] = (7).to_bytes(4, "little")
    data[510:512] = b"\x55\xaa"
    image.write_bytes(data)
    with pytest.raises(ValueError, match="partition table"):
        rufus.probe_iso(image)


def test_gpt_protective_mbr_with_clamped_length_is_accepted(tmp_path):
    image = tmp_path / "gpt-large.img"
    with image.open("wb") as f:
        f.truncate((0xffffffff + 1) * 512)
    with image.open("r+b") as f:
        sector = bytearray(512)
        sector[450] = 0xEE
        sector[454:458] = (1).to_bytes(4, "little")
        sector[458:462] = (0xffffffff).to_bytes(4, "little")
        sector[510:512] = b"\x55\xaa"
        f.write(sector)
        f.write(b"EFI PART")
    assert rufus.probe_iso(image)["raw"] is True


def test_img_part_is_discovered_and_resumed_without_losing_extension(tmp_path):
    dest = tmp_path / "Tails_7.0.img"
    part = tmp_path / "Tails_7.0.img.part"
    part.write_bytes(b"first ")
    state = {"entry_name": "Tails", "direct": True, "url": "https://example.test/tails.img", "dest": str(dest)}
    sidecar = tmp_path / "Tails_7.0.img.part.json"
    sidecar.write_text(json.dumps(state))

    items = rufus.find_incomplete_downloads(tmp_path)
    assert len(items) == 1
    assert items[0]["part_path"] == part

    class Response:
        status_code = 206
        headers = {"Content-Range": "bytes 6-10/11"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"image"

    class Session:
        def get(self, url, **kwargs):
            assert url == state["url"]
            assert kwargs["headers"] == {"Range": "bytes=6-"}
            return Response()

    rufus.download_iso_with_progress(Session(), state["url"], dest, resume=True, progress_cb=lambda *_: None)
    assert dest.read_bytes() == b"first image"
    assert not part.exists()
    assert not sidecar.exists()


def test_direct_img_download_keeps_extension(tmp_path, monkeypatch):
    entry = {"name": "Tails", "direct": [{"label": "Latest", "tag": "latest", "extension": ".img", "url": "https://example.test/tails.img"}]}
    dest = tmp_path / "Tails_latest.img"
    part = tmp_path / "Tails_latest.img.part"

    def fake_download(_session, _url, path):
        assert path == dest
        assert part.with_name(part.name + ".json").exists()
        part.write_bytes(b"raw image")
        part.rename(path)

    monkeypatch.setattr(rufus, "_plain_session", lambda: object())
    monkeypatch.setattr(rufus, "download_iso_with_progress", fake_download)
    assert rufus.download_direct_iso(entry, tmp_path) == dest
    assert dest.read_bytes() == b"raw image"


def test_existing_img_download_is_found(tmp_path):
    image = tmp_path / "Tails_7.0.img"
    image.write_bytes(b"disk")
    assert rufus.find_existing_isos({"name": "Tails"}, tmp_path) == [image]
