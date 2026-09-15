"""A strict, stdlib-only EBML reader for the WebM header of a recording.

Artifact tests need to prove that a committed ``.webm`` really carries one
visible video stream.  Shelling out to ``ffprobe`` makes that assertion skip on
any machine without a working install, which is the same as not asserting at
all.  This module reads the container header directly instead, using only
:mod:`struct`.

Every ambiguity fails loudly.  An unknown-size element, a child that overruns
its parent, a malformed variable-length integer, an empty or missing document
type, a missing timestamp scale or duration, a missing or repeated segment, a
file whose tracks are absent, empty or unreachable, and a track omitting its
number, type or codec all raise :class:`ValueError` rather than yielding a
summary that quietly says nothing.
Parsing stops at the first ``Cluster`` so frame data is never walked, which is
also why a ``Cluster`` preceding ``Tracks`` is a refusal and not an empty
track tuple.
"""

from __future__ import annotations

import struct
from typing import Any


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
FLAG_ENABLED = 0xB9
PIXEL_WIDTH = 0xB0
PIXEL_HEIGHT = 0xBA

_MASTERS = (EBML_HEADER, SEGMENT, INFO, TRACKS)
_NANOSECONDS_PER_SECOND = 1_000_000_000


def read_vint(buf: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int]:
    """Read one EBML variable-length integer and return it with the next offset.

    ``keep_marker`` retains the length-marker bit, which is how element IDs are
    spelled; sizes clear it.
    """

    if pos < 0 or pos >= len(buf):
        raise ValueError(
            f"variable-length integer starts past the end of the buffer at {pos}"
        )
    first = buf[pos]
    if first == 0x00:
        raise ValueError(
            f"variable-length integer has a zero leading byte at offset {pos}"
        )
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    end = pos + length
    if end > len(buf):
        raise ValueError(
            f"variable-length integer at offset {pos} is truncated: "
            f"{length} bytes declared, {len(buf) - pos} available"
        )
    value = first if keep_marker else first & (mask - 1)
    for index in range(pos + 1, end):
        value = (value << 8) | buf[index]
    return value, end


def webm_summary(payload: bytes) -> dict[str, Any]:
    """Summarise the header of a WebM byte string.

    Returns ``doc_type``, ``declared_size`` (the file size the header and the
    segment declare between them), ``duration_seconds`` and a ``tracks`` tuple.
    Each track carries ``number``, ``type``, ``codec``, ``enabled``, ``width``
    and ``height``.  ``enabled`` is the spec's ``FlagEnabled``, defaulting to
    ``True`` when absent; it is never derived from ``FlagDefault``, which is
    routinely ``0`` on a browser recording's only video track.
    """

    return _Reader(payload).summary()


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._doc_type: str | None = None
        self._declared_size: int | None = None
        self._timestamp_scale: int | None = None
        self._duration: float | None = None
        self._tracks: list[dict[str, Any]] = []
        self._stopped = False

    def summary(self) -> dict[str, Any]:
        self._walk(0, len(self._payload), None)
        if self._doc_type is None:
            raise ValueError("EBML header declares no doc type")
        if not self._doc_type:
            raise ValueError("EBML header declares an empty doc type")
        if self._declared_size is None:
            raise ValueError("file declares no segment")
        if self._timestamp_scale is None:
            raise ValueError("segment info declares no timestamp scale")
        if self._duration is None:
            raise ValueError("segment info declares no duration")
        if not self._tracks:
            # Reached by an absent or empty Tracks element and by a Cluster
            # that precedes it, since the walk stops at the first Cluster.
            raise ValueError("file declares no tracks")
        for position, track in enumerate(self._tracks, start=1):
            # A TrackEntry is initialised with these three fields unset, so a
            # malformed file that omits them would otherwise be summarised as a
            # track whose number, type and codec are all ``None`` -- exactly
            # the summary that quietly says nothing.  ``width`` and ``height``
            # are left alone: a non-video track legitimately has neither.
            for field in ("number", "type", "codec"):
                if track[field] is None:
                    raise ValueError(f"track {position} declares no {field}")
            if not track["codec"]:
                # A zero-length CodecID decodes to '', which names no codec,
                # for the same reason a padded-empty DocType is not a doc type.
                raise ValueError(f"track {position} declares an empty codec")
        seconds = (
            self._duration * self._timestamp_scale / _NANOSECONDS_PER_SECOND
        )
        return {
            "doc_type": self._doc_type,
            "declared_size": self._declared_size,
            "duration_seconds": seconds,
            "tracks": tuple(self._tracks),
        }

    def _walk(self, start: int, end: int, parent: int | None) -> None:
        pos = start
        while pos < end and not self._stopped:
            element_id, pos = read_vint(self._payload, pos, keep_marker=True)
            if element_id == CLUSTER:
                self._stopped = True
                return
            size, pos = self._read_size(pos)
            child_end = pos + size
            if child_end > end:
                raise ValueError(
                    f"element 0x{element_id:X} at offset {pos} overruns its "
                    f"parent by {child_end - end} bytes"
                )
            self._visit(element_id, pos, child_end, parent)
            pos = child_end

    def _read_size(self, pos: int) -> tuple[int, int]:
        size, end = read_vint(self._payload, pos, keep_marker=False)
        if size == (1 << (7 * (end - pos))) - 1:
            raise ValueError(f"element at offset {pos} declares an unknown size")
        return size, end

    def _visit(
        self, element_id: int, start: int, end: int, parent: int | None
    ) -> None:
        if element_id in _MASTERS:
            if element_id == SEGMENT:
                if self._declared_size is not None:
                    raise ValueError("file declares more than one segment")
                self._declared_size = end
            self._walk(start, end, element_id)
            return
        if element_id == TRACK_ENTRY and parent == TRACKS:
            self._tracks.append(
                {
                    "number": None,
                    "type": None,
                    "codec": None,
                    "enabled": True,
                    "width": None,
                    "height": None,
                }
            )
            self._walk(start, end, TRACK_ENTRY)
            return
        if element_id == VIDEO and parent == TRACK_ENTRY:
            self._walk(start, end, VIDEO)
            return
        if parent == EBML_HEADER and element_id == DOC_TYPE:
            self._doc_type = self._string(start, end)
        elif parent == INFO and element_id == TIMESTAMP_SCALE:
            self._timestamp_scale = self._uint(start, end)
        elif parent == INFO and element_id == DURATION:
            self._duration = self._float(start, end)
        elif parent == TRACK_ENTRY and self._tracks:
            track = self._tracks[-1]
            if element_id == TRACK_NUMBER:
                track["number"] = self._uint(start, end)
            elif element_id == TRACK_TYPE:
                track["type"] = self._uint(start, end)
            elif element_id == CODEC_ID:
                track["codec"] = self._string(start, end)
            elif element_id == FLAG_ENABLED:
                track["enabled"] = self._uint(start, end) != 0
        elif parent == VIDEO and self._tracks:
            track = self._tracks[-1]
            if element_id == PIXEL_WIDTH:
                track["width"] = self._uint(start, end)
            elif element_id == PIXEL_HEIGHT:
                track["height"] = self._uint(start, end)

    def _uint(self, start: int, end: int) -> int:
        width = end - start
        if not 1 <= width <= 8:
            raise ValueError(
                f"unsigned integer at offset {start} has an unsupported "
                f"width of {width} bytes"
            )
        return int.from_bytes(self._payload[start:end], "big")

    def _float(self, start: int, end: int) -> float:
        raw = self._payload[start:end]
        if len(raw) == 4:
            return float(struct.unpack(">f", raw)[0])
        if len(raw) == 8:
            return float(struct.unpack(">d", raw)[0])
        raise ValueError(
            f"float at offset {start} has an unsupported width of "
            f"{len(raw)} bytes"
        )

    def _string(self, start: int, end: int) -> str:
        raw = self._payload[start:end].rstrip(b"\x00")
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"string at offset {start} is not ASCII"
            ) from exc
