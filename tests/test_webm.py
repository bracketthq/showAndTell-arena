"""Contracts for the stdlib EBML/WebM header reader used by artifact tests."""

from __future__ import annotations

import struct

import pytest

from tests._webm import read_vint, webm_summary

EBML_HEADER = 0x1A45DFA3
SEGMENT = 0x18538067
INFO = 0x1549A966
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
VIDEO = 0xE0
CLUSTER = 0x1F43B675
DOC_TYPE = 0x4282
TIMESTAMP_SCALE = 0x2AD7B1
DURATION = 0x4489
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
CODEC_ID = 0x86
FLAG_DEFAULT = 0x88
FLAG_ENABLED = 0xB9
PIXEL_WIDTH = 0xB0
PIXEL_HEIGHT = 0xBA


def encode_size(value: int, *, length: int = 0) -> bytes:
    """Encode an element size as a variable-length integer."""

    if length == 0:
        length = 1
        while value >= (1 << (7 * length)) - 1:
            length += 1
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def encode_id(element_id: int) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def element(element_id: int, payload: bytes) -> bytes:
    return encode_id(element_id) + encode_size(len(payload)) + payload


def uint(value: int) -> bytes:
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def video_track(
    *,
    number: int = 1,
    codec: bytes = b"V_VP8",
    width: int = 1440,
    height: int = 900,
    enabled: int | None = None,
    default: int | None = None,
) -> bytes:
    body = (
        element(TRACK_NUMBER, uint(number))
        + element(TRACK_TYPE, uint(1))
        + element(CODEC_ID, codec)
    )
    if enabled is not None:
        body += element(FLAG_ENABLED, uint(enabled))
    if default is not None:
        body += element(FLAG_DEFAULT, uint(default))
    body += element(
        VIDEO,
        element(PIXEL_WIDTH, uint(width)) + element(PIXEL_HEIGHT, uint(height)),
    )
    return element(TRACK_ENTRY, body)


def audio_track(*, number: int = 2, codec: bytes = b"A_OPUS") -> bytes:
    return element(
        TRACK_ENTRY,
        element(TRACK_NUMBER, uint(number))
        + element(TRACK_TYPE, uint(2))
        + element(CODEC_ID, codec),
    )


def webm_file(
    tracks: bytes,
    *,
    doc_type: bytes = b"webm",
    duration: float | None = 2000.0,
    scale: int | None = 1_000_000,
    trailer: bytes = b"",
    segment_size: bytes | None = None,
) -> bytes:
    header = element(EBML_HEADER, element(DOC_TYPE, doc_type))
    info_body = b""
    if scale is not None:
        info_body += element(TIMESTAMP_SCALE, uint(scale))
    if duration is not None:
        info_body += element(DURATION, struct.pack(">d", duration))
    segment_body = element(INFO, info_body) + element(TRACKS, tracks) + trailer
    if segment_size is None:
        segment = element(SEGMENT, segment_body)
    else:
        segment = encode_id(SEGMENT) + segment_size + segment_body
    return header + segment


def info_element(
    *, scale: int | None = 1_000_000, duration: object = 2000.0
) -> bytes:
    """Build a Segment Info block carrying a timestamp scale and duration."""

    body = b""
    if scale is not None:
        body += element(TIMESTAMP_SCALE, uint(scale))
    if duration is not None:
        raw = duration if isinstance(duration, bytes) else struct.pack(">d", duration)
        body += element(DURATION, raw)
    return element(INFO, body)


VIDEO_SUMMARY = {
    "number": 1,
    "type": 1,
    "codec": "V_VP8",
    "enabled": True,
    "width": 1440,
    "height": 900,
}


@pytest.mark.parametrize("length", [1, 2, 3, 4])
@pytest.mark.parametrize("value", [0, 5, 100])
def test_vint_lengths_cover_every_encoded_width(length: int, value: int) -> None:
    encoded = ((1 << (7 * length)) | value).to_bytes(length, "big")

    assert read_vint(encoded, 0, keep_marker=False) == (value, length)
    assert read_vint(encoded, 0, keep_marker=True) == (
        (1 << (7 * length)) | value,
        length,
    )


def test_vint_reads_from_an_offset_inside_a_larger_buffer() -> None:
    buffer = b"\xff\xff" + encode_size(300) + b"\xff"

    assert read_vint(buffer, 2, keep_marker=False) == (300, 4)


def test_vint_rejects_a_zero_leading_byte() -> None:
    with pytest.raises(ValueError, match="zero leading byte"):
        read_vint(b"\x00\x01\x02", 0, keep_marker=False)


def test_vint_rejects_a_truncated_encoding() -> None:
    with pytest.raises(ValueError, match="truncated"):
        read_vint(b"\x40", 0, keep_marker=False)


def test_vint_rejects_a_start_past_the_buffer() -> None:
    with pytest.raises(ValueError, match="past the end"):
        read_vint(b"\x81", 1, keep_marker=False)


def test_summary_reads_a_synthetic_single_video_track() -> None:
    payload = webm_file(video_track())

    summary = webm_summary(payload)

    assert summary == {
        "doc_type": "webm",
        "declared_size": len(payload),
        "duration_seconds": 2.0,
        "tracks": (VIDEO_SUMMARY,),
    }


def test_summary_reports_both_tracks_of_a_synthetic_video_and_audio_file() -> None:
    """A second track must be reported, so a one-track assertion means something."""

    payload = webm_file(video_track() + audio_track())

    summary = webm_summary(payload)

    assert tuple(track["type"] for track in summary["tracks"]) == (1, 2)
    assert summary["tracks"][1] == {
        "number": 2,
        "type": 2,
        "codec": "A_OPUS",
        "enabled": True,
        "width": None,
        "height": None,
    }


def test_summary_honours_an_explicit_disabled_flag() -> None:
    payload = webm_file(video_track(enabled=0))

    assert webm_summary(payload)["tracks"][0]["enabled"] is False


def test_summary_treats_an_absent_enabled_flag_as_visible() -> None:
    payload = webm_file(video_track())

    assert webm_summary(payload)["tracks"][0]["enabled"] is True


def test_summary_ignores_the_default_flag_when_reporting_visibility() -> None:
    """Visibility is ``FlagEnabled`` plus track type, never ``FlagDefault``.

    Real browser recordings ship ``FlagDefault`` = 0 on their only video
    track, so a reader keying on that flag would report an invisible stream.
    """

    payload = webm_file(video_track(default=0))

    summary = webm_summary(payload)

    assert summary["tracks"] == (VIDEO_SUMMARY,)


def test_summary_refuses_an_unknown_size_segment() -> None:
    payload = webm_file(video_track(), segment_size=b"\xff")

    with pytest.raises(ValueError, match="unknown size"):
        webm_summary(payload)


def test_summary_refuses_a_child_overrunning_its_parent() -> None:
    tracks = video_track()
    overrun = encode_id(TRACKS) + encode_size(len(tracks) + 64) + tracks
    header = element(EBML_HEADER, element(DOC_TYPE, b"webm"))
    payload = header + element(SEGMENT, info_element() + overrun)

    with pytest.raises(ValueError, match="overruns"):
        webm_summary(payload)


def test_summary_stops_at_the_first_cluster() -> None:
    """Frame data is never walked, so a malformed cluster body cannot matter."""

    poison = element(CLUSTER, b"\x00" * 16) + b"\x00" * 32
    payload = webm_file(video_track(), trailer=poison)

    assert webm_summary(payload)["tracks"] == (VIDEO_SUMMARY,)


def test_summary_refuses_a_payload_that_is_not_ebml() -> None:
    with pytest.raises(ValueError):
        webm_summary(b"\x00" * 4096)


def test_summary_refuses_a_file_without_a_duration() -> None:
    payload = webm_file(video_track(), duration=None)

    with pytest.raises(ValueError, match="duration"):
        webm_summary(payload)


def test_summary_refuses_a_file_without_a_timestamp_scale() -> None:
    payload = webm_file(video_track(), scale=None)

    with pytest.raises(ValueError, match="timestamp scale"):
        webm_summary(payload)


def test_summary_refuses_a_file_without_a_doc_type() -> None:
    payload = element(EBML_HEADER, b"") + element(
        SEGMENT, info_element() + element(TRACKS, video_track())
    )

    with pytest.raises(ValueError, match="no doc type"):
        webm_summary(payload)


@pytest.mark.parametrize("raw", [b"", b"\x00", b"\x00\x00"])
def test_summary_refuses_an_empty_doc_type(raw: bytes) -> None:
    """A zero-length doc type is a missing doc type, not the empty string.

    The reader strips trailing NULs, so a padded-empty DocType decodes to
    ``''`` and would otherwise be reported as a plausible summary field.
    """

    payload = webm_file(video_track(), doc_type=raw)

    with pytest.raises(ValueError, match="empty doc type"):
        webm_summary(payload)


def test_summary_refuses_a_file_without_a_segment() -> None:
    with pytest.raises(ValueError, match="no segment"):
        webm_summary(element(EBML_HEADER, element(DOC_TYPE, b"webm")))


def test_summary_refuses_a_second_segment() -> None:
    """Two segments would merge their tracks and overwrite the declared size."""

    header = element(EBML_HEADER, element(DOC_TYPE, b"webm"))
    body = info_element() + element(TRACKS, video_track())
    payload = header + element(SEGMENT, body) + element(SEGMENT, body)

    with pytest.raises(ValueError, match="more than one segment"):
        webm_summary(payload)


def test_summary_refuses_a_file_without_a_tracks_element() -> None:
    """A header with no Tracks must fail, not report an empty track tuple."""

    payload = element(EBML_HEADER, element(DOC_TYPE, b"webm")) + element(
        SEGMENT, info_element()
    )

    with pytest.raises(ValueError, match="no tracks"):
        webm_summary(payload)


def test_summary_refuses_an_empty_tracks_element() -> None:
    payload = webm_file(b"")

    with pytest.raises(ValueError, match="no tracks"):
        webm_summary(payload)


def test_summary_refuses_a_cluster_placed_before_the_tracks_element() -> None:
    """Parsing stops at the first Cluster, so later tracks are never seen.

    A file laid out that way must refuse rather than report ``tracks: ()``,
    which reads as a genuine answer about a genuinely track-less file.
    """

    header = element(EBML_HEADER, element(DOC_TYPE, b"webm"))
    body = (
        info_element()
        + element(CLUSTER, b"\x00" * 16)
        + element(TRACKS, video_track())
    )
    payload = header + element(SEGMENT, body)

    with pytest.raises(ValueError, match="no tracks"):
        webm_summary(payload)


def partial_track(*, omit: str = "", codec: bytes = b"V_VP8") -> bytes:
    """Build a TrackEntry missing exactly one of its identifying elements."""

    parts = {
        "number": element(TRACK_NUMBER, uint(1)),
        "type": element(TRACK_TYPE, uint(1)),
        "codec": element(CODEC_ID, codec),
    }
    body = b"".join(value for name, value in parts.items() if name != omit)
    return element(
        TRACK_ENTRY,
        body
        + element(
            VIDEO,
            element(PIXEL_WIDTH, uint(1440)) + element(PIXEL_HEIGHT, uint(900)),
        ),
    )


@pytest.mark.parametrize("field", ["number", "type", "codec"])
def test_summary_refuses_a_track_that_omits_an_identifying_field(field: str) -> None:
    """A track summary carrying ``None`` is the quiet nothing this reader bans."""

    payload = webm_file(partial_track(omit=field))

    with pytest.raises(ValueError, match=f"declares no {field}"):
        webm_summary(payload)


def test_summary_refuses_an_empty_codec_id() -> None:
    """A zero-length CodecID decodes to ``''``, which names no codec at all."""

    payload = webm_file(partial_track(codec=b""))

    with pytest.raises(ValueError, match="empty codec"):
        webm_summary(payload)


def test_summary_refuses_a_later_track_that_omits_an_identifying_field() -> None:
    """Every track is checked, not only the first one the walk appended."""

    incomplete = element(
        TRACK_ENTRY, element(TRACK_NUMBER, uint(2)) + element(TRACK_TYPE, uint(2))
    )
    payload = webm_file(video_track() + incomplete)

    with pytest.raises(ValueError, match="track 2 declares no codec"):
        webm_summary(payload)


def test_summary_refuses_a_float_of_an_unsupported_width() -> None:
    payload = element(EBML_HEADER, element(DOC_TYPE, b"webm")) + element(
        SEGMENT, info_element(duration=b"\x01\x02\x03") + element(TRACKS, video_track())
    )

    with pytest.raises(ValueError, match="float"):
        webm_summary(payload)
