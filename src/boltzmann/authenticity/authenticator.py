"""The authenticity check: two verifications, reported separately, never one boolean.

Integrity is recomputed bottom-up from bytes and needs no configuration; authenticity is checked
top-down against the trust root in force at the snapshot's position (paper Section 8.9). This
module runs the second and produces a *report*, not a verdict: every distinguishable failure of
the paper's table appears either as a per-signature outcome or as a report-level finding, and
the summary ``state`` is a derived property no construction can contradict.

The report deliberately has no ``is_ok``. A consumer that collapses "intact" and "authentic"
into one flag cannot express "intact, and signed by an authorized key" as distinct from
"intact, provenance unknown" -- the paper forbids the collapse, and the absence of the property
is its enforcement.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.authenticity.backend import signature_backend_available
from boltzmann.authenticity.chain import (
    Position,
    SnapshotRole,
    descends_from,
    locate,
    observed_revisions,
    walk_first_parents,
)
from boltzmann.authenticity.diff import RequiredScopes, ScopeQuestion, gather_evidence, required_scopes
from boltzmann.authenticity.keys import SshPublicKey
from boltzmann.authenticity.pins import TrustPin, read_pin
from boltzmann.authenticity.policy import VerificationPolicy
from boltzmann.authenticity.record import SignatureRecord, for_snapshot
from boltzmann.authenticity.scopes import PROPOSABLE_SCOPES, Scope
from boltzmann.authenticity.sshsig import SshSignature
from boltzmann.authenticity.sshsig import verify as verify_sshsig
from boltzmann.authenticity.trust_root import SinceVerdict, TrustedKey, TrustRoot, confirm_since
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.exceptions import (
    CompromisedKeyError,
    InsufficientScopeError,
    NamespaceMismatchError,
    QuorumFailureError,
    RetiredKeyError,
    SignatureFormatError,
    SignatureInvalidError,
    TrustRootMismatchError,
    UnauthorizedKeyError,
    UnsignedBrainError,
    UnsupportedKeyTypeError,
    VerificationUnavailableError,
)
from boltzmann.identity.digest import OciDigest
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.base import BlockStore


class AuthorshipState(StrEnum):
    """The three states the paper allows a summary to take -- and the only three."""

    AUTHORIZED = "authorized"
    """A signature over the snapshot verified under a key holding the scopes its change required."""

    UNSIGNED = "unsigned"
    """The brain carries no signature at all: the zero-configuration case, fully verifiable for
    integrity, with no authorship claimed or checkable."""

    UNAUTHORIZED = "unauthorized"
    """A signature was present and failed a condition. Includes "could not be checked": a
    verifier that cannot run the mathematics must not guess in the signer's favor."""


class SignatureOutcome(StrEnum):
    """What became of one signature record. One member per distinguishable fate."""

    VALID = "valid"
    VALID_AS_PROPOSAL = "valid_as_proposal"
    """Valid under a ``propose``-holding key for a content change: attributable, verifiable, and
    explicitly not the published head unless policy says otherwise (paper Section 12.6)."""

    WRONG_NAMESPACE = "wrong_namespace"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    """The record's named fingerprint and its embedded key disagree -- the one rejection that
    needs no cryptography at all."""

    INVALID_SIGNATURE = "invalid_signature"
    UNSUPPORTED_KEY_TYPE = "unsupported_key_type"
    UNAUTHORIZED_KEY = "unauthorized_key"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """The key holds every certainly-required scope but not every possibly-required one, and the
    missing evidence is what keeps the difference open. Distinct from both valid and
    insufficient scope: fail-closed, but honestly labelled."""

    RETIRED_KEY = "retired_key"
    COMPROMISED_KEY = "compromised_key"
    COMPROMISE_UNDECIDABLE = "compromise_undecidable"
    """The compromise position could not be ordered against this snapshot because the chain
    truncated. An undecidable compromise is not a cleared one."""

    UNVERIFIABLE = "unverifiable"
    """The mathematics could not run: the ``[authenticity]`` extra is not installed."""


class FindingKind(StrEnum):
    """Report-level facts. Blocking ones make the state ``unauthorized``; the rest diagnose."""

    UNSIGNED_BRAIN = "unsigned_brain"
    CHAIN_TRUNCATED = "chain_truncated"
    TRUST_ROOT_MISMATCH = "trust_root_mismatch"
    SINCE_REFUTED = "since_refuted"
    QUORUM_FAILURE = "quorum_failure"
    REVISION_CHANGED_CONTENT = "revision_changed_content"
    REVISION_REGRESSED = "revision_regressed"
    SIGNATURES_BELOW_POLICY = "signatures_below_policy"
    PROPOSED_HEAD = "proposed_head"
    COMPROMISED_KEY = "compromised_key"
    GENESIS_BELOW_QUORUM = "genesis_below_quorum"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    UNVERIFIABLE = "unverifiable"


class Finding(BaseModel):
    """
    One report-level fact.

    Attributes:
        kind (FindingKind): What was found.
        detail (str): The specifics, written for the operator who has to act on them.
        key (str | None): The fingerprint involved, when one is.
        blocking (bool): Whether this alone keeps the state from ``authorized``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FindingKind
    detail: str
    key: str | None = None
    blocking: bool = True


class SignatureVerdict(BaseModel):
    """
    What became of one signature record.

    Attributes:
        key (str): The fingerprint the record names.
        embedded_key (str | None): The fingerprint of the key actually inside the signature
            blob, when the armor parses. This one is the authority; the named one is an index.
        claimed_scopes (tuple[Scope, ...]): What the record claims -- diagnosis, never decision.
        held_scopes (tuple[Scope, ...]): What the trust root in force actually grants the key.
        outcome (SignatureOutcome): The fate.
        detail (str | None): The specifics, when the outcome alone does not say enough.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    embedded_key: str | None = None
    claimed_scopes: tuple[Scope, ...] = ()
    held_scopes: tuple[Scope, ...] = ()
    outcome: SignatureOutcome
    detail: str | None = None

    @property
    def is_valid(self) -> bool:
        """Whether this signature fully authorizes on its own."""
        return self.outcome is SignatureOutcome.VALID


class Authorship(BaseModel):
    """
    The authorship line of an Evidence Bundle (paper Section 9.3).

    The projection of an :class:`AuthenticationReport` a query result carries, reported
    separately from ``verified`` -- which covers hashes and membership -- because a bundle that
    collapses the two cannot express "intact, and signed by an authorized key" as distinct from
    "intact, provenance unknown", and a caller has no way to recover the difference afterwards.

    Attributes:
        state (AuthorshipState): Authorized, unsigned, or unauthorized.
        snapshot (OciDigest): The snapshot the evidence was served from.
        key (str | None): A fingerprint that authorized it, when one did.
        trust_root (OciDigest | None): The trust root in force at that position.
        pinned (bool): Whether that trust root is anchored by this consumer's pin.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: AuthorshipState
    snapshot: OciDigest
    key: str | None = None
    trust_root: OciDigest | None = None
    pinned: bool = False


class AuthenticationReport(BaseModel):
    """
    The authenticity half of verification, in full.

    Attributes:
        snapshot (OciDigest): What was authenticated.
        role (SnapshotRole): Genesis, ordinary, or trust-root revision.
        trust_root (OciDigest | None): Digest of the trust root in force at this position.
        trust_root_revision (int | None): Its revision number.
        pinned (bool): Whether the trust root in force is anchored by this consumer's pin --
            directly, or through revisions that each satisfied the quorum rule.
        pin (OciDigest | None): The pin consulted, if one exists.
        required_scopes (tuple[Scope, ...]): What the snapshot's change certainly required.
        undetermined (tuple[ScopeQuestion, ...]): What the evidence could not decide.
        signatures (tuple[SignatureVerdict, ...]): One verdict per record.
        withdrawn (tuple[SignatureVerdict, ...]): Compromised-key verdicts, reported separately
            as the paper requires: the only construct that invalidates a previously valid
            signature must never look like an ordinary authorization failure.
        quorum_required (int | None): For a revision, the previous root's quorum; for a genesis,
            its own declared one.
        quorum_met (int | None): Distinct qualifying keys that signed.
        findings (tuple[Finding, ...]): Report-level facts, blocking and diagnostic.
        signatures_required (int): The policy's bar, recorded so the report is self-describing.
        integrity (bool | None): The *other* verification, carried alongside and never combined.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: OciDigest
    role: SnapshotRole
    trust_root: OciDigest | None = None
    trust_root_revision: int | None = None
    pinned: bool = False
    pin: OciDigest | None = None
    required_scopes: tuple[Scope, ...] = ()
    undetermined: tuple[ScopeQuestion, ...] = ()
    signatures: tuple[SignatureVerdict, ...] = ()
    withdrawn: tuple[SignatureVerdict, ...] = ()
    quorum_required: int | None = None
    quorum_met: int | None = None
    findings: tuple[Finding, ...] = ()
    signatures_required: int = Field(default=1, ge=1)
    integrity: bool | None = None

    @property
    def state(self) -> AuthorshipState:
        """
        The three-state summary, derived and underivable otherwise.

        No construction can claim ``authorized`` while carrying a blocking finding or lacking a
        valid signature -- the property recomputes, which is the code-level reading of "an
        implementation MUST NOT present an unverified brain as verified".
        """
        if not self.signatures and not self.withdrawn:
            return AuthorshipState.UNSIGNED
        if any(finding.blocking for finding in self.findings):
            return AuthorshipState.UNAUTHORIZED
        accepted = {
            verdict.embedded_key
            for verdict in self.signatures
            if verdict.embedded_key is not None
            and verdict.outcome in (SignatureOutcome.VALID, SignatureOutcome.VALID_AS_PROPOSAL)
        }
        if len(accepted) >= self.signatures_required:
            return AuthorshipState.AUTHORIZED
        return AuthorshipState.UNAUTHORIZED

    @property
    def is_proposal(self) -> bool:
        """Whether the only acceptance this snapshot has is as somebody's proposal."""
        outcomes = {verdict.outcome for verdict in self.signatures}
        return SignatureOutcome.VALID_AS_PROPOSAL in outcomes and SignatureOutcome.VALID not in outcomes

    def has(self, kind: FindingKind) -> bool:
        """
        Whether a finding of this kind was reported.

        Args:
            kind (FindingKind): The kind to look for.

        Returns:
            bool: Whether it appears.
        """
        return any(finding.kind is kind for finding in self.findings)

    def outcomes(self) -> dict[str, SignatureOutcome]:
        """
        The fate of every signature, keyed by the fingerprint the record named.

        Returns:
            dict[str, SignatureOutcome]: Outcomes for signatures and withdrawn alike.
        """
        return {verdict.key: verdict.outcome for verdict in (*self.signatures, *self.withdrawn)}

    def require_authorized(self) -> None:
        """
        Fail unless the state is ``authorized``, with the most specific failure as the type.

        Raises:
            UnsignedBrainError: No signature is present.
            TrustRootMismatchError: The trust root does not match the pin.
            QuorumFailureError: A revision lacks its quorum, changed content, or regressed.
            CompromisedKeyError: A signature was withdrawn by a compromise record.
            RetiredKeyError: The only failures are retirements.
            UnauthorizedKeyError: A key is absent from the trust root in force, a ``since``
                claim was refuted, or the chain truncated under the question.
            InsufficientScopeError: A key is authorized but not for this change, or the head is
                only a proposal.
            VerificationUnavailableError: The mathematics could not run.
            SignatureInvalidError: What remains: the signature itself did not hold.
        """
        state = self.state
        if state is AuthorshipState.AUTHORIZED:
            return
        if state is AuthorshipState.UNSIGNED:
            raise UnsignedBrainError(f"snapshot {self.snapshot} carries no signature")
        if self.has(FindingKind.TRUST_ROOT_MISMATCH):
            raise TrustRootMismatchError(self.detail(FindingKind.TRUST_ROOT_MISMATCH))
        if self.has(FindingKind.SINCE_REFUTED) or self.has(FindingKind.CHAIN_TRUNCATED):
            raise UnauthorizedKeyError(
                self.detail(FindingKind.SINCE_REFUTED) or self.detail(FindingKind.CHAIN_TRUNCATED)
            )
        for kind in (FindingKind.QUORUM_FAILURE, FindingKind.REVISION_CHANGED_CONTENT, FindingKind.REVISION_REGRESSED):
            if self.has(kind):
                raise QuorumFailureError(self.detail(kind))
        if self.withdrawn:
            raise CompromisedKeyError(
                f"signatures by {', '.join(verdict.key for verdict in self.withdrawn)} over "
                f"{self.snapshot} are withdrawn by a compromise record"
            )
        outcomes = {verdict.outcome for verdict in self.signatures}
        if SignatureOutcome.RETIRED_KEY in outcomes:
            raise RetiredKeyError(
                f"the signing keys of {self.snapshot} are retired at this position; their earlier signatures stand"
            )
        if SignatureOutcome.UNAUTHORIZED_KEY in outcomes:
            raise UnauthorizedKeyError(f"no signing key of {self.snapshot} is in the trust root in force")
        if (
            self.has(FindingKind.PROPOSED_HEAD)
            or {SignatureOutcome.INSUFFICIENT_SCOPE, SignatureOutcome.INSUFFICIENT_EVIDENCE} & outcomes
        ):
            raise InsufficientScopeError(
                f"no signing key of {self.snapshot} holds every scope its change required "
                f"({', '.join(scope.value for scope in self.required_scopes) or 'none determined'})"
            )
        if SignatureOutcome.UNVERIFIABLE in outcomes:
            raise VerificationUnavailableError(
                "the signatures could not be checked: pip install 'pyboltzmann[authenticity]'"
            )
        raise SignatureInvalidError(f"no signature over {self.snapshot} verified")

    def authorship(self) -> Authorship:
        """
        The projection an Evidence Bundle carries.

        Returns:
            Authorship: The summary, with the first fully-valid key named when one exists.
        """
        valid = next(
            (verdict.embedded_key for verdict in self.signatures if verdict.outcome is SignatureOutcome.VALID),
            None,
        )
        return Authorship(
            state=self.state,
            snapshot=self.snapshot,
            key=valid,
            trust_root=self.trust_root,
            pinned=self.pinned,
        )

    def detail(self, kind: FindingKind) -> str:
        """
        The first finding's detail of a kind, or an empty string.

        Args:
            kind (FindingKind): What to look for.

        Returns:
            str: The prose a refusal can carry verbatim.
        """
        for finding in self.findings:
            if finding.kind is kind:
                return finding.detail
        return ""


class Authenticator:
    """
    Runs the top-down check against one store.

    Attributes:
        store (BlockStore): Where snapshots, records, compositions, and the pin live.
        policy (VerificationPolicy): The deployment's tolerances. Affects what blocks; never
            affects what is reported.
    """

    def __init__(self, store: BlockStore, policy: VerificationPolicy | None = None) -> None:
        """
        Open an authenticator over a store.

        Args:
            store (BlockStore): The store to read from.
            policy (VerificationPolicy | None): Tolerances. Defaults to the paper's defaults.
        """
        self.store = store
        self.policy = policy if policy is not None else VerificationPolicy()

    # --- The flow ---------------------------------------------------------------

    def authenticate(
        self,
        snapshot: Snapshot,
        records: Sequence[SignatureRecord] | None = None,
        current: TrustRoot | None = None,
    ) -> AuthenticationReport:
        """
        Check every signature over a snapshot against the trust root in force at its position.

        Args:
            snapshot (Snapshot): The document to authenticate.
            records (Sequence[SignatureRecord] | None): The signatures to judge. Defaults to
                what the store holds for this snapshot. Records covering another snapshot are
                skipped, not failed: they are simply not about this question.
            current (TrustRoot | None): The newest trust root the caller knows -- ordinarily the
                brain's head's. Compromise markers are read from here, because a compromise is
                discovered after the fact and recorded by a *later* revision than the positions
                it withdraws (paper Section 8.6); authorization, scopes, and retirement stay
                judged at the snapshot's own position. Defaults to the snapshot's own trust
                root, which is exact when authenticating the head.

        Returns:
            AuthenticationReport: Everything, separated. Never raises for a protocol failure --
            a report is the answer, and :meth:`AuthenticationReport.require_authorized` is the
            caller's way to turn it into a typed refusal.
        """
        digest = snapshot.digest
        position = locate(self.store, snapshot)
        newest = current if current is not None else snapshot.trust_root
        held = [
            record
            for record in (records if records is not None else for_snapshot(self.store, digest))
            if record.snapshot == digest
        ]

        findings: list[Finding] = []
        if position.truncated:
            findings.append(
                Finding(
                    kind=FindingKind.CHAIN_TRUNCATED,
                    detail=(
                        f"the first parent {snapshot.first_parent} of {digest.short} is not held, so "
                        f"the trust root in force here is unknowable"
                    ),
                )
            )

        in_force = position.in_force
        pin = read_pin(self.store)
        pinned, pin_findings = self._judge_pin(pin, snapshot, position)
        findings.extend(pin_findings)

        if in_force is not None and not position.truncated:
            observed = observed_revisions(self.store, snapshot)
            for entry in in_force.keys:
                if confirm_since(observed, entry) is SinceVerdict.REFUTED:
                    findings.append(
                        Finding(
                            kind=FindingKind.SINCE_REFUTED,
                            detail=(
                                f"key {entry.fingerprint} claims authorization since revision "
                                f"{entry.since}, but an observed revision at or after it does not "
                                f"list the key; the whole trust root carrying the claim is untrustworthy"
                            ),
                            key=entry.fingerprint,
                        )
                    )

        self._judge_ancestry(snapshot, position, findings, pinned=pinned)

        required = required_scopes(gather_evidence(self.store, snapshot, position.parent))
        if not required.is_complete:
            findings.append(
                Finding(
                    kind=FindingKind.EVIDENCE_INCOMPLETE,
                    detail=(
                        f"the difference against the first parent could not be fully read "
                        f"({', '.join(question.value for question in sorted(required.undetermined))}); "
                        f"keys are judged against every scope still possible"
                    ),
                    blocking=False,
                )
            )

        if not held:
            findings.append(
                Finding(kind=FindingKind.UNSIGNED_BRAIN, detail=f"snapshot {digest.short} carries no signature")
            )
            return self._report(snapshot, position, pinned, pin, required, (), (), None, None, findings)

        if not signature_backend_available():
            findings.append(
                Finding(
                    kind=FindingKind.UNVERIFIABLE,
                    detail=(
                        "signatures are present and the Ed25519 primitive is not: "
                        "pip install 'pyboltzmann[authenticity]'. Every structural check still ran."
                    ),
                    blocking=False,
                )
            )

        verdicts: list[SignatureVerdict] = []
        withdrawn: list[SignatureVerdict] = []
        accepted_keys: dict[str, set[bytes]] = {"valid": set(), "proposal": set()}
        for record in held:
            verdict, blob, is_withdrawn = self._judge(record, snapshot, position, required, newest)
            if is_withdrawn:
                withdrawn.append(verdict)
                findings.append(
                    Finding(
                        kind=FindingKind.COMPROMISED_KEY,
                        detail=(
                            f"the signature by {verdict.key} is withdrawn: the key is recorded as "
                            f"compromised from a position this snapshot descends from"
                        ),
                        key=verdict.key,
                        blocking=False,
                    )
                )
                continue
            verdicts.append(verdict)
            if blob is not None and verdict.outcome is SignatureOutcome.VALID:
                accepted_keys["valid"].add(blob)
            if blob is not None and verdict.outcome is SignatureOutcome.VALID_AS_PROPOSAL:
                accepted_keys["proposal"].add(blob)

        quorum_required, quorum_met = self._judge_governance(snapshot, position, held, findings)
        self._judge_policy(accepted_keys["valid"], accepted_keys["proposal"], findings)

        return self._report(
            snapshot, position, pinned, pin, required, verdicts, withdrawn, quorum_required, quorum_met, findings
        )

    # --- One record --------------------------------------------------------------

    def _judge(
        self,
        record: SignatureRecord,
        snapshot: Snapshot,
        position: Position,
        required: RequiredScopes,
        current: TrustRoot | None,
    ) -> tuple[SignatureVerdict, bytes | None, bool]:
        """One record's fate: (verdict, embedded key blob for distinctness, withdrawn-by-compromise)."""

        def verdict(
            outcome: SignatureOutcome,
            detail: str | None = None,
            embedded: str | None = None,
            held: tuple[Scope, ...] = (),
        ) -> SignatureVerdict:
            return SignatureVerdict(
                key=record.key,
                embedded_key=embedded,
                claimed_scopes=record.scopes,
                held_scopes=held,
                outcome=outcome,
                detail=detail,
            )

        if record.namespace != SNAPSHOT_NAMESPACE:
            return (
                verdict(
                    SignatureOutcome.WRONG_NAMESPACE,
                    f"the record names namespace {record.namespace!r}, not {SNAPSHOT_NAMESPACE!r}",
                ),
                None,
                False,
            )
        try:
            parsed: SshSignature = record.parsed
        except SignatureFormatError as error:
            return verdict(SignatureOutcome.INVALID_SIGNATURE, str(error)), None, False

        embedded = parsed.public_key
        if embedded.fingerprint != record.key:
            return (
                verdict(
                    SignatureOutcome.FINGERPRINT_MISMATCH,
                    f"the record names {record.key} but the signature embeds {embedded.fingerprint}; "
                    f"a record whose two identities disagree is rejected outright",
                    embedded=embedded.fingerprint,
                ),
                None,
                False,
            )

        try:
            verify_sshsig(parsed, snapshot.canonical_bytes())
        except NamespaceMismatchError as error:
            return verdict(SignatureOutcome.WRONG_NAMESPACE, str(error), embedded=embedded.fingerprint), None, False
        except UnsupportedKeyTypeError as error:
            return (
                verdict(SignatureOutcome.UNSUPPORTED_KEY_TYPE, str(error), embedded=embedded.fingerprint),
                None,
                False,
            )
        except VerificationUnavailableError:
            return (
                verdict(
                    SignatureOutcome.UNVERIFIABLE,
                    "the [authenticity] extra is not installed, so the mathematics could not run",
                    embedded=embedded.fingerprint,
                ),
                None,
                False,
            )
        except (SignatureFormatError, SignatureInvalidError) as error:
            return verdict(SignatureOutcome.INVALID_SIGNATURE, str(error), embedded=embedded.fingerprint), None, False

        in_force = position.in_force
        entry = in_force.entry_for(embedded) if in_force is not None else None
        if in_force is None or entry is None:
            return (
                verdict(
                    SignatureOutcome.UNAUTHORIZED_KEY,
                    (
                        "no trust root is in force at this position"
                        if in_force is None
                        else f"{embedded.fingerprint} is not in the trust root in force (revision {in_force.revision})"
                    ),
                    embedded=embedded.fingerprint,
                ),
                embedded.blob,
                False,
            )
        held_scopes = entry.scopes
        if in_force.is_retired(entry):
            return (
                verdict(
                    SignatureOutcome.RETIRED_KEY,
                    f"{embedded.fingerprint} is retired from revision {entry.retired_from}; its earlier "
                    f"signatures stand",
                    embedded=embedded.fingerprint,
                    held=held_scopes,
                ),
                embedded.blob,
                False,
            )
        compromise = self._compromise_position(embedded, entry, current)
        if compromise is not None:
            reached = descends_from(self.store, snapshot, compromise)
            if reached is True:
                return (
                    verdict(
                        SignatureOutcome.COMPROMISED_KEY,
                        f"{embedded.fingerprint} is compromised from {compromise}; every "
                        f"signature at and after that position is withdrawn",
                        embedded=embedded.fingerprint,
                        held=held_scopes,
                    ),
                    embedded.blob,
                    True,
                )
            if reached is None:
                return (
                    verdict(
                        SignatureOutcome.COMPROMISE_UNDECIDABLE,
                        f"{embedded.fingerprint} carries a compromise record at {compromise} "
                        f"and the chain truncates before it can be ordered against this snapshot",
                        embedded=embedded.fingerprint,
                        held=held_scopes,
                    ),
                    embedded.blob,
                    False,
                )

        granted = frozenset(held_scopes)
        if required.possible <= granted:
            outcome = SignatureOutcome.VALID
        elif required.scopes <= granted:
            outcome = SignatureOutcome.INSUFFICIENT_EVIDENCE
        elif Scope.PROPOSE in granted and required.possible <= PROPOSABLE_SCOPES:
            outcome = SignatureOutcome.VALID_AS_PROPOSAL
        else:
            missing = ", ".join(scope.value for scope in sorted(required.possible - granted))
            return (
                verdict(
                    SignatureOutcome.INSUFFICIENT_SCOPE,
                    f"{embedded.fingerprint} is authorized but does not hold: {missing}",
                    embedded=embedded.fingerprint,
                    held=held_scopes,
                ),
                embedded.blob,
                False,
            )
        return verdict(outcome, embedded=embedded.fingerprint, held=held_scopes), embedded.blob, False

    @staticmethod
    def _compromise_position(key: SshPublicKey, entry: TrustedKey, current: TrustRoot | None) -> OciDigest | None:
        """The compromise marker to honour: the newest known revision's, falling back to the local one.

        A compromise is discovered after the fact and recorded by a revision *later* than the
        positions it withdraws, so the marker for a key is read from the newest trust root the
        caller knows. The entry at the judged position carries none precisely when the judged
        position predates the discovery -- which is the case the construct exists for.
        """
        if current is not None:
            newest = current.entry_for(key)
            if newest is not None and newest.compromised_from is not None:
                return newest.compromised_from
        return entry.compromised_from

    # --- Governance ---------------------------------------------------------------

    def quorum_count(self, position: Position, records: Sequence[SignatureRecord]) -> int:
        """
        Distinct authorized ``govern`` holders of the previous revision that validly signed.

        Public because governance operations gate on it *before* advancing anything: ``rotate``
        refuses to move the head until this meets the quorum the previous revision declares.

        The paper's quorum rule, exactly: valid signatures, distinct keys, each holding
        ``govern`` in the trust root as it stood *before* the change -- which is
        ``position.in_force``. Retirement is judged at the parent's revision, so a key retired
        *by* the revision under review still counts, which is the half that is easy to lose.
        Distinctness is on the raw key blob, never on record count: one key can produce two
        different valid records over one snapshot by switching hash algorithm.
        """
        in_force = position.in_force
        if in_force is None:
            return 0
        message = position.snapshot.canonical_bytes()
        governors: set[bytes] = set()
        for record in records:
            if record.snapshot != position.digest or record.namespace != SNAPSHOT_NAMESPACE:
                continue
            try:
                parsed = record.parsed
                if parsed.public_key.fingerprint != record.key:
                    continue
                verify_sshsig(parsed, message)
            except (SignatureFormatError, SignatureInvalidError, NamespaceMismatchError, UnsupportedKeyTypeError):
                continue
            except VerificationUnavailableError:
                return 0
            entry = in_force.entry_for(parsed.public_key)
            if entry is None or not entry.holds(Scope.GOVERN) or in_force.is_retired(entry):
                continue
            if entry.compromised_from is not None and (
                descends_from(self.store, position.snapshot, entry.compromised_from) is not False
            ):
                continue
            governors.add(parsed.public_key.blob)
        return len(governors)

    def _judge_ancestry(
        self, snapshot: Snapshot, position: Position, findings: list[Finding], pinned: bool = False
    ) -> None:
        """Validate every trust-root transition in the ancestry, not only the head's.

        Rejection propagates forward: a snapshot draws its trust root from its first parent, so a
        descendant of a rejected revision is standing on authority that was never granted, and
        the whole subtree falls with it (paper Section 8.9, Case 2). This is why the
        self-admission attack fails **with no pin at all** -- the verifier is checking an
        internal transition rule, and the chain the attacker must ship contains the evidence
        that their own change of authority was never approved.

        A walk that ends at an unresolvable non-genesis position is the same attack one step
        removed: whoever withheld the parent also withheld the proof that the oldest reachable
        authority was ever granted. That truncation blocks -- unless a pin matched, because a pin
        can only ever match within the resolvable walk, so a match *is* an explicit anchor at or
        above the gap, and history behind an anchor decides nothing.
        """
        if position.truncated or position.parent is None:
            return
        for ancestor in walk_first_parents(self.store, position.parent):
            if ancestor.truncated:
                findings.append(
                    Finding(
                        kind=FindingKind.CHAIN_TRUNCATED,
                        detail=(
                            f"ancestor {ancestor.digest.short} names first parent "
                            f"{ancestor.snapshot.first_parent} which is not held, so the origin of "
                            f"the authority in force at the head is unverifiable"
                            + ("; the pin anchors the authority above the gap" if pinned else "")
                        ),
                        blocking=not pinned,
                    )
                )
                return
            if ancestor.role is not SnapshotRole.REVISION:
                continue
            previous = ancestor.in_force
            if previous is None:
                findings.append(
                    Finding(
                        kind=FindingKind.QUORUM_FAILURE,
                        detail=(
                            f"ancestor {ancestor.digest.short} introduced a trust root onto a chain "
                            f"that had none, so no quorum could ever have approved it; every "
                            f"authority downstream of it is asserted, not granted"
                        ),
                    )
                )
                return
            met = self.quorum_count(ancestor, for_snapshot(self.store, ancestor.digest))
            if met < previous.govern_quorum:
                findings.append(
                    Finding(
                        kind=FindingKind.QUORUM_FAILURE,
                        detail=(
                            f"the trust root in force here was introduced by ancestor "
                            f"{ancestor.digest.short} carrying {met} of the {previous.govern_quorum} "
                            f"govern signatures revision {previous.revision} requires; a rejected "
                            f"revision takes its whole subtree with it"
                        ),
                    )
                )
                return

    def _judge_governance(
        self,
        snapshot: Snapshot,
        position: Position,
        records: Sequence[SignatureRecord],
        findings: list[Finding],
    ) -> tuple[int | None, int | None]:
        """The revision and genesis rules, yielding the quorum columns of the report."""
        if position.role is SnapshotRole.REVISION:
            parent = position.parent
            if parent is not None and snapshot.modules != parent.modules:
                findings.append(
                    Finding(
                        kind=FindingKind.REVISION_CHANGED_CONTENT,
                        detail=(
                            "a trust-root revision must change nothing else, and this one's module "
                            "roots differ from its first parent's; governance is a separate, "
                            "individually auditable event, never folded into a commit"
                        ),
                    )
                )
            previous = position.in_force
            if previous is None:
                findings.append(
                    Finding(
                        kind=FindingKind.QUORUM_FAILURE,
                        detail=(
                            "this snapshot introduces a trust root onto a chain that had none, so "
                            "there is no previous revision to draw a quorum from; authority is "
                            "anchored at a genesis or accepted by an explicit pin, never asserted "
                            "mid-chain"
                        ),
                    )
                )
                return None, None
            introduced = snapshot.trust_root
            if introduced is not None and introduced.revision <= previous.revision:
                findings.append(
                    Finding(
                        kind=FindingKind.REVISION_REGRESSED,
                        detail=(
                            f"the introduced trust root carries revision {introduced.revision}, which "
                            f"does not follow the revision {previous.revision} in force"
                        ),
                    )
                )
            met = self.quorum_count(position, records)
            if met < previous.govern_quorum:
                findings.append(
                    Finding(
                        kind=FindingKind.QUORUM_FAILURE,
                        detail=(
                            f"a trust-root revision requires {previous.govern_quorum} valid signatures "
                            f"from distinct keys holding govern in revision {previous.revision}; this "
                            f"one carries {met}"
                        ),
                    )
                )
            return previous.govern_quorum, met
        if position.role is SnapshotRole.GENESIS and snapshot.trust_root is not None:
            declared = snapshot.trust_root.govern_quorum
            met = self.quorum_count(
                Position(
                    snapshot=position.snapshot,
                    digest=position.digest,
                    parent=None,
                    role=position.role,
                    in_force=snapshot.trust_root,
                    truncated=False,
                ),
                records,
            )
            if met < declared:
                findings.append(
                    Finding(
                        kind=FindingKind.GENESIS_BELOW_QUORUM,
                        detail=(
                            f"the genesis declares a govern quorum of {declared} and carries {met} "
                            f"qualifying signatures; this proves nothing against an attacker and is "
                            f"reported for coherence -- a brain stating a rule its founding act "
                            f"departed from"
                        ),
                        blocking=False,
                    )
                )
            return declared, met
        return None, None

    def _judge_policy(self, valid: set[bytes], proposals: set[bytes], findings: list[Finding]) -> None:
        """The policy's bar: enough distinct fully-valid keys, or an explicitly allowed proposal."""
        needed = self.policy.required_signatures
        if len(valid) >= needed:
            return
        if proposals and not valid and not self.policy.allow_propose_head:
            findings.append(
                Finding(
                    kind=FindingKind.PROPOSED_HEAD,
                    detail=(
                        "every acceptable signature here holds only propose: the snapshot is "
                        "attributable and verifiable and explicitly not the published head, and the "
                        "policy does not permit treating it as the current state"
                    ),
                )
            )
            return
        if proposals and self.policy.allow_propose_head and len(valid | proposals) >= needed:
            return
        findings.append(
            Finding(
                kind=FindingKind.SIGNATURES_BELOW_POLICY,
                detail=(
                    f"the policy requires {needed} valid signature(s) from distinct keys and this "
                    f"snapshot carries {len(valid)}"
                ),
            )
        )

    # --- The pin --------------------------------------------------------------------

    def _judge_pin(self, pin: TrustPin | None, snapshot: Snapshot, position: Position) -> tuple[bool, list[Finding]]:
        """Whether the trust root in force is anchored by the pin.

        A pin matches directly, or through custody: the pinned root is an ancestor revision and
        every revision between it and here satisfied the quorum rule -- the paper's "warn loudly,
        and by default refuse, on any later change that does not follow the quorum rule". A
        change that followed it is not a mismatch; it is the mechanism working.
        """
        if pin is None:
            return False, []
        anchored = pin.trust_root
        in_force = position.in_force
        if in_force is not None and in_force.digest == anchored:
            return True, []
        if not position.truncated:
            # The pinned root being an ancestor revision is enough here: every trust-root
            # transition in the ancestry is separately validated by _judge_ancestry, so a match
            # below means the root in force descends from the pin through approved revisions --
            # or the report already carries the quorum failure that says otherwise.
            for ancestor in walk_first_parents(self.store, snapshot):
                authority = ancestor.snapshot.trust_root
                if authority is not None and authority.digest == anchored:
                    return True, []
        return False, [
            Finding(
                kind=FindingKind.TRUST_ROOT_MISMATCH,
                detail=(
                    f"the pinned trust root {anchored.short} is neither in force here nor an ancestor "
                    f"of what is; an artifact-supplied key list never overrides a pin"
                ),
            )
        ]

    # --- Assembly ---------------------------------------------------------------------

    def _report(
        self,
        snapshot: Snapshot,
        position: Position,
        pinned: bool,
        pin: TrustPin | None,
        required: RequiredScopes,
        verdicts: Sequence[SignatureVerdict],
        withdrawn: Sequence[SignatureVerdict],
        quorum_required: int | None,
        quorum_met: int | None,
        findings: Sequence[Finding],
    ) -> AuthenticationReport:
        in_force: TrustRoot | None = position.in_force
        return AuthenticationReport(
            snapshot=position.digest,
            role=position.role,
            trust_root=in_force.digest if in_force is not None else None,
            trust_root_revision=in_force.revision if in_force is not None else None,
            pinned=pinned,
            pin=pin.trust_root if pin is not None else None,
            required_scopes=tuple(sorted(required.scopes)),
            undetermined=tuple(sorted(required.undetermined)),
            signatures=tuple(verdicts),
            withdrawn=tuple(withdrawn),
            quorum_required=quorum_required,
            quorum_met=quorum_met,
            findings=tuple(findings),
            signatures_required=self.policy.required_signatures,
        )
