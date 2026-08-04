"""Properties of identity: what must hold for any input, not just the ones we thought of."""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from boltzmann.exceptions import DigestFormatError, DigestKindError, NonDeterministicValueError, SerializationError
from boltzmann.identity.digest import BlockId, Digest, MerkleRoot, OciDigest
from boltzmann.identity.serialization import MAX_SAFE_INTEGER, canonicalize, get_serializer
from boltzmann.identity.time import parse_timestamp, utc_timestamp

# JSON-shaped values that the protocol accepts inside a payload: no floats, no unsafe integers.
json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER),
    st.text(),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(st.lists(children, max_size=4), st.dictionaries(st.text(), children, max_size=4)),
    max_leaves=12,
)


class TestSerialization:
    """Canonical serialization must be a function of value, not of construction."""

    @given(json_values)
    def test_is_deterministic(self, value: object) -> None:
        assert canonicalize(value) == canonicalize(value)

    @given(st.dictionaries(st.text(), json_scalars, min_size=2, max_size=6))
    def test_is_key_order_independent(self, mapping: dict) -> None:
        reversed_mapping = dict(reversed(list(mapping.items())))
        assert canonicalize(mapping) == canonicalize(reversed_mapping)

    @given(json_values)
    def test_produces_valid_utf8(self, value: object) -> None:
        canonicalize(value).decode("utf-8")

    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_floats_are_always_refused(self, number: float) -> None:
        with pytest.raises(NonDeterministicValueError):
            canonicalize({"value": number})

    @given(st.integers(min_value=MAX_SAFE_INTEGER + 1))
    def test_unsafe_integers_are_always_refused(self, number: int) -> None:
        with pytest.raises(NonDeterministicValueError):
            canonicalize({"value": number})

    def test_nested_float_is_located_in_the_error(self) -> None:
        with pytest.raises(NonDeterministicValueError, match=r"\$\.outer\[1\]\.inner"):
            canonicalize({"outer": [0, {"inner": 1.5}]})

    def test_non_string_key_is_refused(self) -> None:
        with pytest.raises(NonDeterministicValueError, match="non-string object key"):
            canonicalize({1: "one"})

    def test_value_outside_the_json_model_is_refused(self) -> None:
        with pytest.raises(SerializationError, match="outside the JSON data model"):
            canonicalize({"when": object()})

    def test_unknown_serialization_is_refused(self) -> None:
        with pytest.raises(SerializationError, match="unknown serialization"):
            get_serializer("dag-cbor/1")


class TestDigestLevels:
    """The three levels of hashes must never be confused for one another."""

    @given(st.binary(max_size=64))
    def test_same_bytes_different_levels_are_not_equal(self, data: bytes) -> None:
        assert BlockId.of(data).hex == MerkleRoot.of(data).hex
        assert BlockId.of(data) != MerkleRoot.of(data)
        assert MerkleRoot.of(data) != OciDigest.of(data)

    @given(st.binary(max_size=64))
    def test_round_trips_through_its_string_form(self, data: bytes) -> None:
        block_id = BlockId.of(data)
        assert BlockId.parse(str(block_id)) == block_id

    @given(st.binary(max_size=64))
    def test_raw_bytes_round_trip(self, data: bytes) -> None:
        block_id = BlockId.of(data)
        assert BlockId.from_raw(block_id.raw) == block_id

    @pytest.mark.parametrize(
        ("source", "target"),
        [(MerkleRoot, BlockId), (BlockId, MerkleRoot), (OciDigest, BlockId), (BlockId, OciDigest)],
    )
    def test_cross_level_parsing_is_refused(self, source: type[Digest], target: type[Digest]) -> None:
        with pytest.raises(DigestKindError):
            target.parse(source.of(b"payload"))

    @pytest.mark.parametrize(
        "value",
        [
            "sha256:tooshort",
            "sha256:" + "F" * 64,
            "md5:" + "a" * 32,
            "a" * 64,
            "",
        ],
    )
    def test_malformed_digests_are_refused(self, value: str) -> None:
        with pytest.raises(DigestFormatError):
            BlockId.parse(value)

    def test_non_string_is_refused(self) -> None:
        with pytest.raises(DigestFormatError):
            BlockId.parse(42)

    def test_unsupported_algorithm_is_refused(self) -> None:
        with pytest.raises(DigestFormatError, match="unsupported hash algorithm"):
            BlockId(algorithm="sha512", hex="a" * 64)

    def test_short_form_is_abbreviated(self) -> None:
        block_id = BlockId.of(b"payload")
        assert block_id.short.startswith("sha256:")
        assert len(block_id.short) == len("sha256:") + 12


class TestTimestamps:
    """A timestamp inside a payload must have exactly one textual form."""

    @given(st.datetimes())
    def test_formats_canonically(self, moment: object) -> None:
        formatted = utc_timestamp(moment)  # type: ignore[arg-type]
        assert formatted.endswith("Z")
        assert len(formatted) == len("2026-07-24T09:30:00Z")

    @given(st.datetimes())
    def test_round_trips(self, moment: object) -> None:
        formatted = utc_timestamp(moment)  # type: ignore[arg-type]
        assert utc_timestamp(parse_timestamp(formatted)) == formatted

    @pytest.mark.parametrize(
        ("year", "expected"),
        [(1, "0001"), (99, "0099"), (999, "0999"), (1000, "1000"), (2026, "2026")],
    )
    def test_pads_a_year_below_1000(self, year: int, expected: str) -> None:
        # Not a hypothetical: strftime's `%Y` delegates to the platform C library, which
        # writes year 999 as `999` under glibc and `0999` under BSD. The same instant would
        # hash to two different block_id values depending on the host, so the padding is
        # pinned here rather than left to libc.
        assert utc_timestamp(datetime(year, 1, 1, tzinfo=UTC)).startswith(f"{expected}-01-01T")

    @pytest.mark.parametrize("value", ["2026-07-24T09:30:00+00:00", "2026-07-24 09:30:00Z", "2026-07-24"])
    def test_non_canonical_forms_are_refused(self, value: str) -> None:
        with pytest.raises(ValueError, match="not a canonical UTC timestamp"):
            parse_timestamp(value)
