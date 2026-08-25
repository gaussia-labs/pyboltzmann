"""The network transport, over ORAS.

Requires the ``[oci]`` extra. The paper cites ORAS as the reference for OCI Artifacts, and this is a
thin adapter over it: resolve a manifest, fetch a blob, upload the blobs a registry does not have, then
the manifest.

Because the local brain is already an OCI layout, a push here is a transfer of files that already exist
on disk -- ORAS uploads a path, and that path is the blob in ``blobs/sha256/``. Nothing is serialized at
push time.

Blob-level methods are used rather than ORAS's file-oriented ``push``/``pull``, because the artifact
this SDK publishes has its own manifest shape: one layer per module, each annotated with the module's
internal Merkle root, and a config blob that is the snapshot document.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from boltzmann.distribution.manifest import (
    BrainManifest,
    Descriptor,
    SignatureManifest,
    parse_manifest,
    parse_signature_manifest,
)
from boltzmann.distribution.media_types import (
    EMPTY_CONFIG_BYTES,
    IMAGE_INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    REFERRERS_TAG_PREFIX,
    SIGNATURE_MEDIA_TYPE,
)
from boltzmann.exceptions import DistributionError, ReferenceNotFoundError
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.store.base import BlockStore
from boltzmann.store.oci_layout import OciLayoutStore

HTTP_NOT_FOUND = 404
"""What a registry answers for a tag it does not have."""

OK_STATUSES = (200, 201, 202)
"""What counts as success across the registry API."""

WRITE_AUTH_TIMEOUT = 30
"""Seconds to wait for the challenge that precedes a write."""


def _registry(insecure: bool) -> Any:
    """Import ORAS lazily, so the core stays installable without the extra."""
    try:
        import oras.provider
    except ModuleNotFoundError as error:  # pragma: no cover - depends on install extras
        raise DistributionError("the ORAS transport needs the [oci] extra: pip install 'pyboltzmann[oci]'") from error
    return oras.provider.Registry(insecure=insecure)


class OrasRegistryClient:
    """
    Talks to an OCI-compatible registry over HTTP.

    Attributes:
        insecure (bool): Whether to allow plain HTTP, for local registries.
    """

    def __init__(self, insecure: bool = False, registry: Any | None = None) -> None:
        """
        Build a client.

        Args:
            insecure (bool): Whether to allow plain HTTP.
            registry (Any | None): An already-configured ORAS ``Registry``, for callers that handled
                authentication themselves. Defaults to a fresh one.
        """
        self.insecure = insecure
        self._registry = registry

    @property
    def registry(self) -> Any:
        """The underlying ORAS registry, built on first use."""
        if self._registry is None:
            self._registry = _registry(self.insecure)
        return self._registry

    def login(self, username: str, password: str) -> None:
        """
        Authenticate against the registry.

        Args:
            username (str): Account name.
            password (str): Password or token.
        """
        self.registry.login(username=username, password=password)

    async def resolve(self, reference: str, tag: str) -> BrainManifest:
        """
        Fetch a manifest by tag, without downloading any module.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to resolve.

        Returns:
            BrainManifest: The manifest.

        Raises:
            ReferenceNotFoundError: If the registry reports no manifest under this tag.
            DistributionError: If the tag cannot be resolved for any other reason, or what it resolves to
                is not a Boltzmann brain.
        """
        # ORAS's own ``get_manifest`` reports every failure the same way, and the status code is what says
        # whether the tag is absent or the registry refused us. A caller has to be able to tell: absence
        # before a first push is expected, and a refusal that looks like absence disables the
        # fast-forward check that exists to stop a push from clobbering someone else's version.
        response = self._request_manifest(reference, tag)
        if response.status_code == HTTP_NOT_FOUND:
            raise ReferenceNotFoundError(f"{reference}:{tag} is not published")
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"cannot resolve {reference}:{tag}: {response.status_code} {response.reason} -- {response.text[:200]}"
            )

        if not response.content.lstrip().startswith(b"{"):
            raise DistributionError(
                f"cannot resolve {reference}:{tag}: the registry answered {response.status_code} with "
                f"{response.headers.get('content-type', 'no content type')} rather than a manifest. "
                f"For Docker Hub the endpoint is registry-1.docker.io; docker.io serves the website"
            )

        return parse_manifest(response.content)

    def _authorize_write(self, reference: str) -> None:
        """Obtain a token that can actually write, rather than trusting the registry's challenge.

        A bearer token is scoped to a set of actions, and ORAS asks for exactly the scope the
        ``Www-Authenticate`` challenge names. Against Docker Hub that is not enough: the challenge its
        upload endpoint returns advertises ``pull`` alone, so ORAS receives a read-only token, retries with
        it, and is refused by the same registry -- whose error then names ``pull`` **and** ``push`` as the
        actions required. The credentials were never the problem. The ``docker`` CLI does not hit this
        because it asks for ``pull,push`` when it intends to push, whatever the challenge says.

        So this asks for the write scope up front. The challenge is still read from the registry, for its
        realm and service, and only the scope is replaced -- inventing the realm would be worse than
        trusting it.

        A registry that needs no authentication answers the probe without a challenge, and nothing happens.
        If the token request fails, the token is left alone and the ordinary challenge-response path still
        runs, so this can only improve on the previous behaviour.

        Args:
            reference (str): Repository reference, ``<host>/<namespace>/<repo>``.
        """
        auth = getattr(self.registry, "auth", None)
        if auth is None or not hasattr(auth, "request_token"):
            return

        import oras.auth.utils as auth_utils

        host, _, repository = reference.partition("/")
        if not repository:
            return

        try:
            probe = self.registry.session.get(f"{self.registry.prefix}://{host}/v2/", timeout=WRITE_AUTH_TIMEOUT)
            challenge = probe.headers.get("Www-Authenticate")
            if not challenge:
                return  # No authentication in front of this registry.

            header = auth_utils.parse_auth_header(challenge)
            header.scope = f"repository:{repository}:pull,push"
            token = auth.request_token(header)
        except Exception:
            # A failed optimisation must not fail the push: the ordinary path still runs.
            return

        if token:
            if hasattr(auth, "set_token_auth"):
                auth.set_token_auth(token)
            else:
                auth.token = token

    def _request_manifest(self, reference: str, tag: str) -> Any:
        """The raw manifest response, so that the status code survives."""
        import oras.defaults

        try:
            container = self.registry.get_container(f"{reference}:{tag}")
            url = f"{self.registry.prefix}://{container.manifest_url()}"
            accept = ", ".join(oras.defaults.default_manifest_accepted_media_types)
            return self.registry.do_request(url, "GET", headers={"Accept": accept})
        except Exception as error:
            # A transport failure rather than a rejection: no status code exists to classify.
            raise DistributionError(f"cannot reach {reference}:{tag}: {error}") from error

    async def pull_blob(self, reference: str, digest: OciDigest, store: BlockStore) -> None:
        """
        Download one blob into a local store.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The blob to fetch.
            store (BlockStore): Where to write it. The store recomputes the digest, so a corrupted
                download cannot land under the identity it claims.

        Raises:
            DistributionError: If the download fails.
        """
        try:
            response = self.registry.get_blob(reference, str(digest))
            response.raise_for_status()
        except Exception as error:
            raise DistributionError(f"cannot fetch {digest.short} from {reference}: {error}") from error

        stored = store.put_bytes(response.content)
        if stored != digest:
            raise DistributionError(f"{reference} served bytes for {digest.short} that hash to {stored.short}")

    async def push(
        self,
        reference: str,
        tag: str,
        manifest: BrainManifest,
        store: BlockStore,
    ) -> OciDigest:
        """
        Upload the blobs the registry lacks, then the manifest.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to publish under.
            manifest (BrainManifest): The manifest to publish.
            store (BlockStore): Where the blobs are read from.

        Returns:
            OciDigest: The digest the registry filed the manifest under, which is also the digest the local
            layout holds it under -- one artifact, one name.

        Raises:
            DistributionError: If an upload fails, or the registry stored something other than what was
                sent.
        """
        container = self.registry.get_container(f"{reference}:{tag}")

        for descriptor in [manifest.config, *manifest.layers]:
            layer = descriptor.model_dump(mode="json", by_alias=True, exclude_defaults=False, exclude_none=True)
            if not descriptor.annotations:
                layer.pop("annotations", None)
            if self.registry.blob_exists(layer, container):
                continue
            with self._as_file(descriptor, store) as path:
                # After the existence check, because that is a read and would leave a read-scoped token
                # cached for the write to fail with.
                self._authorize_write(reference)
                response = self.registry.upload_blob(str(path), container, layer)
            if response.status_code not in OK_STATUSES:
                raise DistributionError(
                    f"uploading {descriptor.digest.short} to {reference} failed with {response.status_code}"
                )

        return self._upload_manifest(container, reference, tag, manifest)

    def _upload_manifest(self, container: Any, reference: str, tag: str, manifest: BrainManifest) -> OciDigest:
        """
        Upload the manifest's own bytes, unaltered, and return the digest the registry filed them under.

        Sending the bytes rather than a dictionary is what keeps the artifact's name the same in every
        place it exists. Handing ORAS a dictionary would let ``requests`` serialize it, and a digest is over
        bytes: the registry would file the artifact under a name the publisher could not reproduce, and
        pinning by digest -- the only way to name a version somebody else can move a tag away from -- would
        resolve to nothing.

        The tag is deliberately *not* written into the manifest. It is a pointer to an artifact, not a
        property of one; putting it inside would give the same brain a different digest under every tag it
        is published as. The layout records the tag in ``index.json``, and the registry in the tag itself.

        Args:
            container (Any): The ORAS container for this reference and tag.
            reference (str): Repository reference, for messages.
            tag (str): The tag being published, for messages.
            manifest (BrainManifest): What to publish.

        Returns:
            OciDigest: The digest the registry reported, checked against ours.

        Raises:
            DistributionError: If the upload fails, or the registry's digest disagrees with the bytes sent.
        """
        payload = manifest.to_bytes()
        ours = OciDigest.of(payload)

        self._authorize_write(reference)
        response = self.registry.do_request(
            f"{self.registry.prefix}://{container.manifest_url()}",
            "PUT",
            headers={"Content-Type": MANIFEST_MEDIA_TYPE},
            data=payload,
        )
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"publishing {reference}:{tag} failed with {response.status_code} {response.reason} -- "
                f"{response.text[:200]}"
            )

        stored = response.headers.get("Docker-Content-Digest")
        if stored is None:
            # The OCI spec requires the header on a manifest PUT. Without it there is nothing to check
            # against, and our own digest is the best answer available.
            return ours
        if stored != str(ours):
            raise DistributionError(
                f"{reference}:{tag} was stored as {stored} but the bytes sent hash to {ours}; the registry "
                f"rewrote the manifest, so the artifact has two names and neither side can pin it"
            )
        return ours

    async def push_referrer(self, reference: str, manifest: SignatureManifest, store: BlockStore) -> OciDigest:
        """
        Publish a signature manifest as a referrer of an artifact already pushed.

        The record blob and the empty config are uploaded through the ordinary blob path, then
        the manifest is PUT at its own digest -- never at a tag. A registry that indexed the
        ``subject`` answers with an ``OCI-Subject`` header; one that did not gets the fallback:
        the ``sha256-<hex>`` tag is read, the referrer added, and the index written back, which
        is the scheme the Referrers API specifies for registries that predate it.

        Args:
            reference (str): Repository reference.
            manifest (SignatureManifest): What to publish.
            store (BlockStore): Where the record blob is read from.

        Returns:
            OciDigest: Digest of the published signature manifest.

        Raises:
            DistributionError: If an upload fails.
        """
        container = self.registry.get_container(f"{reference}:latest")
        for descriptor, payload in (
            (manifest.config, EMPTY_CONFIG_BYTES),
            (manifest.record, store.get_bytes(manifest.record.digest)),
        ):
            layer = descriptor.model_dump(mode="json", by_alias=True, exclude_defaults=False, exclude_none=True)
            layer.pop("annotations", None)
            if self.registry.blob_exists(layer, container):
                continue
            self._authorize_write(reference)
            with _StagedFile(payload) as path:
                response = self.registry.upload_blob(str(path), container, layer)
            if response.status_code not in OK_STATUSES:
                raise DistributionError(
                    f"uploading signature blob {descriptor.digest.short} to {reference} failed with "
                    f"{response.status_code}"
                )

        payload = manifest.to_bytes()
        digest = OciDigest.of(payload)
        self._authorize_write(reference)
        response = self.registry.do_request(
            self._manifest_url(reference, str(digest)),
            "PUT",
            headers={"Content-Type": MANIFEST_MEDIA_TYPE},
            data=payload,
        )
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"publishing a signature referrer to {reference} failed with {response.status_code} "
                f"{response.reason} -- {response.text[:200]}"
            )
        if response.headers.get("OCI-Subject") is None:
            # The registry did not index the subject: it predates the Referrers API, so the
            # fallback tag carries the index instead.
            self._append_to_fallback(reference, manifest, digest, len(payload))
        return digest

    async def referrers(self, reference: str, subject: OciDigest, artifact_type: str | None = None) -> list[Descriptor]:
        """
        Discover referrers through the Referrers API, falling back to the tag scheme.

        Args:
            reference (str): Repository reference.
            subject (OciDigest): The referred manifest's digest.
            artifact_type (str | None): Narrow to one artifact type.

        Returns:
            list[Descriptor]: One descriptor per referrer, possibly empty.
        """
        host, _, repository = reference.partition("/")
        url = f"{self.registry.prefix}://{host}/v2/{repository}/referrers/{subject}"
        if artifact_type is not None:
            url += f"?artifactType={artifact_type}"
        try:
            response = self.registry.do_request(url, "GET", headers={"Accept": IMAGE_INDEX_MEDIA_TYPE})
        except Exception as error:
            raise DistributionError(f"cannot query referrers of {subject.short} at {reference}: {error}") from error
        if response.status_code == HTTP_NOT_FOUND:
            return self._fallback_referrers(reference, subject, artifact_type)
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"querying referrers of {subject.short} at {reference} failed with {response.status_code}"
            )
        try:
            listing = response.json()
        except ValueError as error:
            raise DistributionError(
                f"the referrers listing for {subject.short} at {reference} is not JSON: {error}"
            ) from error
        return self._parse_referrer_entries(listing, artifact_type, subject)

    @staticmethod
    def _parse_referrer_entries(listing: object, artifact_type: str | None, subject: OciDigest) -> list[Descriptor]:
        """Read a referrers listing tolerantly: filter first, parse second, skip what fails.

        A listing is shared space -- other tools attach their own referrers, whose descriptors
        legally carry fields this model does not (``platform``, ``urls``, ``data``) -- so an
        entry is only parsed once it claims the artifact type asked for, and one unreadable
        entry never hides the others. The API's own filter is advisory: a registry that ignored
        it sets no annotation, and the client filters regardless, as the spec instructs.
        """
        entries = listing.get("manifests", []) if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            logging.getLogger(__name__).warning(
                "the referrers listing for %s does not carry a manifests array; treating it as empty",
                subject.short,
            )
            return []
        found: list[Descriptor] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if artifact_type is not None and entry.get("artifactType") != artifact_type:
                continue
            try:
                found.append(Descriptor.model_validate(entry))
            except ValidationError:
                logging.getLogger(__name__).warning("skipping an unreadable referrer of %s", subject.short)
        return found

    async def pull_referrer(self, reference: str, digest: OciDigest) -> SignatureManifest:
        """
        Fetch one signature manifest by digest.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The signature manifest's digest.

        Returns:
            SignatureManifest: The parsed manifest.

        Raises:
            DistributionError: If the fetch fails or what arrives is not a signature manifest.
        """
        try:
            response = self.registry.do_request(
                self._manifest_url(reference, str(digest)),
                "GET",
                headers={"Accept": MANIFEST_MEDIA_TYPE},
            )
        except Exception as error:
            raise DistributionError(f"cannot fetch signature manifest {digest.short}: {error}") from error
        if response.status_code not in OK_STATUSES:
            raise DistributionError(f"fetching signature manifest {digest.short} failed with {response.status_code}")
        return parse_signature_manifest(response.content)

    def _manifest_url(self, reference: str, name: str) -> str:
        """The manifests endpoint for a tag or a digest, built without a Container's tag parsing."""
        host, _, repository = reference.partition("/")
        return f"{self.registry.prefix}://{host}/v2/{repository}/manifests/{name}"

    def _fallback_tag(self, subject: OciDigest) -> str:
        return f"{REFERRERS_TAG_PREFIX}{subject.hex}"

    def _fallback_referrers(self, reference: str, subject: OciDigest, artifact_type: str | None) -> list[Descriptor]:
        """Read the ``sha256-<hex>`` index a pre-Referrers registry holds instead."""
        return self._parse_referrer_entries(
            {"manifests": self._fallback_raw_entries(reference, subject)}, artifact_type, subject
        )

    def _fallback_raw_entries(self, reference: str, subject: OciDigest) -> list[dict[str, Any]]:
        """The fallback index's entries as the registry holds them, so a rewrite drops nothing."""
        response = self._request_manifest(reference, self._fallback_tag(subject))
        if response.status_code == HTTP_NOT_FOUND:
            return []
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"reading the referrers fallback tag for {subject.short} failed with {response.status_code}"
            )
        try:
            listing = response.json()
        except ValueError as error:
            raise DistributionError(
                f"the referrers fallback index for {subject.short} at {reference} is not JSON: {error}"
            ) from error
        entries = listing.get("manifests", []) if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _append_to_fallback(self, reference: str, manifest: SignatureManifest, digest: OciDigest, size: int) -> None:
        """Read-modify-write the fallback index so pre-Referrers registries still list signatures.

        The existing entries are carried as raw documents, not re-parsed descriptors: the tag is
        shared space, and rewriting a foreign entry through this client's model would silently
        drop the fields it does not know.
        """
        existing = self._fallback_raw_entries(reference, manifest.subject.digest)
        if any(entry.get("digest") == str(digest) for entry in existing):
            return
        entries = [
            *existing,
            {
                "mediaType": MANIFEST_MEDIA_TYPE,
                "artifactType": SIGNATURE_MEDIA_TYPE,
                "digest": str(digest),
                "size": size,
                "annotations": dict(manifest.annotations),
            },
        ]
        payload = canonicalize(
            {
                "schemaVersion": 2,
                "mediaType": IMAGE_INDEX_MEDIA_TYPE,
                "manifests": entries,
            }
        )
        self._authorize_write(reference)
        response = self.registry.do_request(
            self._manifest_url(reference, self._fallback_tag(manifest.subject.digest)),
            "PUT",
            headers={"Content-Type": IMAGE_INDEX_MEDIA_TYPE},
            data=payload,
        )
        if response.status_code not in OK_STATUSES:
            raise DistributionError(
                f"writing the referrers fallback tag at {reference} failed with {response.status_code}"
            )

    def _as_file(self, descriptor: Descriptor, store: BlockStore) -> Any:
        """ORAS uploads a path. For a layout store that path already exists; otherwise, stage one."""
        if isinstance(store, OciLayoutStore):
            return _ExistingFile(store.blobs_dir / descriptor.digest.hex)
        return _StagedFile(store.get_bytes(descriptor.digest))


class _ExistingFile:
    """A blob that is already a file on disk, so there is nothing to copy."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exception: object) -> None:
        return None


class _StagedFile:
    """A blob from a store with no filesystem, staged for the duration of one upload."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self._handle: Any = None

    def __enter__(self) -> Path:
        self._handle = tempfile.NamedTemporaryFile(delete=False)
        self._handle.write(self.payload)
        self._handle.close()
        return Path(self._handle.name)

    def __exit__(self, *exception: object) -> None:
        if self._handle is not None:
            Path(self._handle.name).unlink(missing_ok=True)
