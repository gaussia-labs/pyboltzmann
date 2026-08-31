"""Identity primitives: what a hash means, and what bytes it is computed over."""

from boltzmann.identity.digest import BlockId, Digest, MerkleRoot, OciDigest
from boltzmann.identity.hashing import ALGORITHM, DIGEST_SIZE, hash_empty, hash_leaf, hash_node, sha256, sha256_hex
from boltzmann.identity.principal import (
    MAX_ACTOR_ID,
    ActorIdForm,
    actor_id_form,
    is_actor_id,
    parse_actor_id,
)
from boltzmann.identity.serialization import (
    MAX_SAFE_INTEGER,
    SERIALIZATION_ID,
    Serializer,
    canonicalize,
    get_serializer,
    reject_non_deterministic,
)

__all__ = [
    "ALGORITHM",
    "DIGEST_SIZE",
    "MAX_ACTOR_ID",
    "MAX_SAFE_INTEGER",
    "SERIALIZATION_ID",
    "BlockId",
    "Digest",
    "MerkleRoot",
    "OciDigest",
    "ActorIdForm",
    "Serializer",
    "actor_id_form",
    "canonicalize",
    "get_serializer",
    "hash_empty",
    "hash_leaf",
    "hash_node",
    "is_actor_id",
    "parse_actor_id",
    "reject_non_deterministic",
    "sha256",
    "sha256_hex",
]
