"""An MCP server over a Boltzmann brain.

One tool per protocol operation, each a thin call into the SDK. Nothing here decides what knowledge a
source yields, which index to consult, or what to keep -- those belong to the model on the other end of
the connection and to the retention policy, and a server that decided them would be answering questions
the protocol assigns elsewhere.

**Ingestion is two calls, and that is the point.** :func:`open_task` hands back a processing task and the
JSON Schema its candidates have to satisfy; the client's model writes candidates against that schema;
:func:`submit_candidates` validates and commits them. The shape makes the protocol's central rule
structural rather than documented: *the external model never writes to a Merkle DAG or an index* (paper
Section 7.1). There is no tool that would let it. Validation and commit happen here or not at all.

The one thing this server refuses to do is start misconfigured. Its lifespan validates the environment
and opens the brain before it accepts a single request, so a missing registry is a startup failure with
an explanation rather than a tool call that fails halfway through a push.
"""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from boltzmann_sandbox import wire
from boltzmann_sandbox.brain import open_brain, registry_client
from boltzmann_sandbox.config import ConfigError, Settings, load

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from boltzmann.brain import Brain
    from boltzmann.distribution.oras_client import OrasRegistryClient
    from boltzmann.ingest.task import ProcessingTask

INSTRUCTIONS = """A Boltzmann brain: portable, verifiable, model-agnostic knowledge.

The brain conserves, validates and retrieves knowledge. You process, contextualize and use it.

Retrieval returns data with its provenance, never prose, and every block it returns has been verified by
hash and by membership in the installed version. Cite what you get by block_id.

To add knowledge: call open_task for a registered source, write candidates against the JSON Schema it
returns, then call submit_candidates. Validation is the brain's; a candidate you cannot justify from the
source will be rejected rather than stored."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True, idempotentHint=False)


@dataclass(slots=True)
class Session:
    """
    What every tool needs, built once at startup.

    Attributes:
        settings (Settings): Validated configuration.
        brain (Brain): The brain, with this sandbox's planner and indices.
        client (OrasRegistryClient): The transport, already authenticated.
        lock (threading.RLock): Serializes brain access. FastMCP runs tools in a thread pool, so two
            calls can overlap; a read that overlapped a commit would see a half-written version, and two
            writes would race for the snapshot. One lock over the brain is enough and costs nothing at
            the request rate a sandbox sees.
        tasks (dict[str, ProcessingTask]): Tasks handed out by :func:`open_task` and not yet submitted.
            Held in memory on purpose -- a task is an invitation to propose, not a commitment, and one
            that is never answered should evaporate with the process rather than accumulate on disk.
    """

    settings: Settings
    brain: Brain
    client: OrasRegistryClient
    lock: threading.RLock
    tasks: dict[str, ProcessingTask] = field(default_factory=dict)


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """
    Validate the environment and open the brain, before the first request.

    Args:
        _server (FastMCP): The server being started. Unused: everything this needs comes from the
            environment, which is the point -- configuration is not a runtime argument.

    Yields:
        dict[str, Any]: The session, under ``"session"``.

    Raises:
        SystemExit: If the configuration is incomplete. Refusing to start is the whole point: a brain that
            cannot say which OCI artifact it works against cannot be tested, and discovering that inside a
            tool call turns a configuration mistake into a mysterious failure.
    """
    try:
        settings = load()
    except ConfigError as error:
        print(f"cannot start: {error}", file=sys.stderr)
        print("Run `uv run boltzmann-doctor` for the full picture.", file=sys.stderr)
        raise SystemExit(2) from error

    brain = open_brain(settings)
    client = registry_client(settings)
    print(
        f"boltzmann: {settings.reference} | brain {settings.brain_path} | version {brain.snapshot().digest.short}",
        file=sys.stderr,
    )

    session = Session(settings=settings, brain=brain, client=client, lock=threading.RLock())
    try:
        yield {"session": session}
    finally:
        # Nothing to close: the brain is an OCI layout on disk and every write already landed.
        pass


mcp: FastMCP = FastMCP(name="boltzmann", instructions=INSTRUCTIONS, lifespan=lifespan)


def use(ctx: Context) -> Session:
    """
    The session for this request.

    Args:
        ctx (Context): The request context.

    Returns:
        Session: The brain and its transport.
    """
    session: Session = ctx.lifespan_context["session"]
    return session


def _memory_type(name: str) -> Any:
    """A memory type from its name, with the valid ones listed when it is wrong."""
    from boltzmann.blocks.memory_type import MemoryType

    try:
        return MemoryType(name)
    except ValueError as error:
        valid = ", ".join(kind.value for kind in MemoryType)
        raise ToolError(f"{name!r} is not a memory type. Valid: {valid}") from error


def _block_id(value: str) -> Any:
    """A block id from its string form."""
    from boltzmann.exceptions import DigestFormatError, DigestKindError
    from boltzmann.identity.digest import BlockId

    try:
        return BlockId.parse(value)
    except (DigestFormatError, DigestKindError) as error:
        raise ToolError(f"{value!r} is not a block id: {error}") from error


# --- Reading -------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def brain_state(ctx: Context) -> dict[str, Any]:
    """Report the installed version: its digest, each module's Merkle root and block count, how many
    versions this brain has, and where it tracks."""
    session = use(ctx)
    with session.lock:
        origin = session.brain.origin
        return {
            **wire.snapshot(session.brain.snapshot()),
            "versions": len(session.brain.ancestry()),
            "artifact": session.settings.reference,
            # The layout's head pointer as stored on disk. Empty for a brain with no commits yet, which is
            # a state worth being able to see rather than one to hide behind a default.
            "head": session.brain.state(),
            # Where this brain tracks, like a git tracking branch: the remote it last agreed with, and
            # whether that agreement covered every module or only the ones installed.
            "tracking": {
                "reference": origin.reference,
                "tag": origin.tag,
                "snapshot": str(origin.snapshot),
                "partial": origin.partial,
            }
            if origin is not None
            else None,
        }


@mcp.tool(annotations=READ_ONLY)
def search(
    ctx: Context,
    text: Annotated[str, Field(description="What to look for. May be empty when filters alone narrow it.")] = "",
    memory_types: Annotated[
        list[str] | None,
        Field(description="Restrict to these kinds of memory: canonical, episodic, semantic, procedural, provenance."),
    ] = None,
    subject: Annotated[str | None, Field(description="Restrict to one domain.")] = None,
    limit: Annotated[int, Field(description="Maximum matches.", ge=1, le=100)] = 10,
    mode: Annotated[
        str,
        Field(description="Matching strategy: auto, exact, lexical, semantic, associative. Never an index name."),
    ] = "auto",
    expand_depth: Annotated[
        int, Field(description="How far to follow declared relations outward, for associative retrieval.", ge=0, le=3)
    ] = 0,
    include_superseded: Annotated[
        bool, Field(description="Whether blocks a newer one replaced may be returned.")
    ] = False,
) -> dict[str, Any]:
    """Retrieve knowledge with its provenance. Returns verified blocks and their sources, never a written
    answer: composing one is your job, and citing block_id is how the answer stays checkable."""
    from boltzmann.query.request import Query, QueryFilters, QueryHints, RetrievalMode

    session = use(ctx)
    try:
        retrieval = RetrievalMode(mode)
    except ValueError as error:
        valid = ", ".join(item.value for item in RetrievalMode)
        raise ToolError(f"{mode!r} is not a retrieval mode. Valid: {valid}") from error

    query = Query(
        text=text,
        filters=QueryFilters(
            memory_types=[_memory_type(name) for name in memory_types] if memory_types else None,
            subject=subject,
            include_superseded=include_superseded,
        ),
        hints=QueryHints(mode=retrieval, limit=limit, expand_depth=expand_depth),
    )
    with session.lock:
        return wire.evidence(session.brain.search(query))


@mcp.tool(annotations=READ_ONLY)
def resolve_block(
    ctx: Context,
    block_id: Annotated[str, Field(description="The block to read, as sha256:<hex>.")],
) -> dict[str, Any]:
    """Read one block by identity. The bytes are re-hashed on the way out, so what you get back is what
    the identity names or nothing at all."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    identity = _block_id(block_id)
    with session.lock:
        try:
            block = session.brain.resolve(identity)
        except BoltzmannError as error:
            raise ToolError(str(error)) from error

        for memory_type, module in session.brain.modules().items():
            if identity in module:
                return wire.block(block, memory_type)
        raise ToolError(f"{block_id} resolved but belongs to no installed module")


@mcp.tool(annotations=READ_ONLY)
def prove_block(
    ctx: Context,
    block_id: Annotated[str, Field(description="The block to prove.")],
    memory_type: Annotated[str, Field(description="Which module should contain it.")],
) -> dict[str, Any]:
    """Prove a block belongs to the installed version, in O(log n), without holding the rest of the
    module. Returns the audit path and whether it verifies against the module's root."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    kind = _memory_type(memory_type)
    with session.lock:
        try:
            proof = session.brain.prove(_block_id(block_id), kind)
            return wire.proof(proof, session.brain.root_of(kind))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=READ_ONLY)
def verify_brain(ctx: Context) -> dict[str, Any]:
    """Recompute every module's Merkle root from its blocks and compare against the installed snapshot.
    Also reports which blocks are resolvable, tombstoned, or missing."""
    session = use(ctx)
    with session.lock:
        return {
            "verified": session.brain.verify(),
            "snapshot": str(session.brain.snapshot().digest),
            **wire.resolvability(session.brain.resolvability()),
        }


@mcp.tool(annotations=READ_ONLY)
def history(ctx: Context) -> dict[str, Any]:
    """List this brain's versions, newest first, with each one's roots. Every retained root still
    verifies: a later version does not invalidate an earlier one."""
    session = use(ctx)
    with session.lock:
        versions = session.brain.history()
        return {
            "count": len(versions),
            "versions": [wire.snapshot(version) for version in versions],
        }


# --- Writing -------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False),
)
def register_source(
    ctx: Context,
    media_type: Annotated[str, Field(description="What the bytes are, e.g. application/pdf, text/plain.")],
    text: Annotated[str | None, Field(description="The source as text. Use this or file_path, not both.")] = None,
    file_path: Annotated[str | None, Field(description="A local file to read the source from.")] = None,
    license: Annotated[str | None, Field(description="Licence or retention policy for this source.")] = None,
) -> dict[str, Any]:
    """Preserve a source verbatim as canonical evidence, addressed by the hash of its bytes. Registering
    the same bytes twice is a no-op that returns the same identity, so this is safe to retry."""
    from boltzmann.blocks.provenance import Actor, ActorKind
    from boltzmann.exceptions import BoltzmannError
    from boltzmann.ingest.register import RegistrationRequest

    session = use(ctx)
    if (text is None) == (file_path is None):
        raise ToolError("give exactly one of text or file_path")

    if file_path is not None:
        path = Path(file_path).expanduser()
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ToolError(f"cannot read {file_path}: {error}") from error
    else:
        data = (text or "").encode()

    request = RegistrationRequest(
        media_type=media_type,
        actor=Actor(id=session.settings.actor, kind=ActorKind.HUMAN),
        license=license,
    )
    with session.lock:
        try:
            result = session.brain.register(data, request)
        except BoltzmannError as error:
            raise ToolError(str(error)) from error

    return {
        **wire.registration(result),
        "size": len(data),
        "next": "call open_task with this block_id to extract knowledge from it",
    }


# Reads the brain but is not idempotent: each call mints a task id, and provenance will cite whichever one
# the candidates were submitted under.
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=False))
def open_task(
    ctx: Context,
    source: Annotated[str, Field(description="The registered canonical block to derive knowledge from.")],
    allowed: Annotated[
        list[str] | None,
        Field(description="Which kinds of memory may be proposed: episodic, semantic, procedural."),
    ] = None,
    instructions: Annotated[str | None, Field(description="Extra guidance to carry with the task.")] = None,
) -> dict[str, Any]:
    """Open a processing task over a registered source, and return the JSON Schema its candidates must
    satisfy.

    This is the boundary: you decide what knowledge the source yields, because that judgment is yours and
    not the protocol's. Write candidates against the schema returned here and pass them to
    submit_candidates. Canonical and provenance memory cannot be proposed -- one is the source itself, the
    other is the brain's own record of what happened."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)

    # The SDK leaves task_id optional, since a caller doing both halves itself has no need to correlate
    # them. Here the two halves are separate requests, so one is minted -- and provenance cites it, which
    # is what later links every committed block back to the request that proposed it.
    task_id = f"task-{uuid.uuid4().hex[:12]}"

    with session.lock:
        try:
            task = session.brain.define_task(
                _block_id(source),
                allowed=[_memory_type(name) for name in allowed] if allowed else None,
                instructions=instructions,
                task_id=task_id,
            )
            schema = session.brain.candidates_schema(task)
        except BoltzmannError as error:
            raise ToolError(str(error)) from error

        session.tasks[task_id] = task

    return {
        "task": task.model_dump(mode="json"),
        "candidates_schema": schema,
        "next": "write candidates against candidates_schema, then call submit_candidates with this task_id",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def submit_candidates(
    ctx: Context,
    task_id: Annotated[str, Field(description="The task_id open_task returned.")],
    candidates: Annotated[
        list[dict[str, Any]],
        Field(description="Candidates matching the candidates_schema from open_task."),
    ],
    producer_id: Annotated[str, Field(description="Which model produced these, e.g. claude-opus-5.")],
    producer_version: Annotated[str, Field(description="That model's version, so a drop can target it later.")],
) -> dict[str, Any]:
    """Validate candidates and commit the ones that pass.

    Validation is the brain's, not yours: a candidate citing evidence it was not derived from, duplicating
    an existing block, or contradicting one, is rejected or held for review rather than stored. A rejection
    comes back with its code, so a second attempt can be better rather than just different."""
    from boltzmann.blocks.provenance import Producer, ProducerKind
    from boltzmann.exceptions import BoltzmannError
    from boltzmann.ingest.proposer import Candidate, CandidateSet

    session = use(ctx)
    task = session.tasks.get(task_id)
    if task is None:
        known = ", ".join(session.tasks) or "none outstanding"
        raise ToolError(f"unknown task_id {task_id!r}. Call open_task first. Outstanding: {known}")

    try:
        proposed = CandidateSet(
            producer=Producer(kind=ProducerKind.MODEL, id=producer_id, version=producer_version),
            candidates=[Candidate.model_validate(candidate) for candidate in candidates],
        )
    except ValueError as error:
        # Malformed against the schema the model was given. That is a bad proposal, not a broken brain.
        raise ToolError(f"candidates do not match the schema from open_task: {error}") from error

    with session.lock:
        try:
            report = session.brain.validate(proposed, task)
            result = session.brain.commit(report)
        except BoltzmannError as error:
            raise ToolError(str(error)) from error

    return {"validation": wire.validation(report), **wire.commit(result)}


# --- Retention -----------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def plan_drop(
    ctx: Context,
    blocks: Annotated[list[str], Field(description="The blocks to remove.")],
    memory_type: Annotated[str, Field(description="Which module they live in.")],
    reason: Annotated[str, Field(description="Why. Recorded in provenance when the drop runs.")],
) -> dict[str, Any]:
    """Show what dropping these blocks would take with it, without doing it.

    Dropping canonical evidence is privileged: everything derived from it goes too, because knowledge whose
    source was removed can no longer be justified. Always plan before you drop."""
    from boltzmann.blocks.provenance import Actor, ActorKind
    from boltzmann.exceptions import BoltzmannError
    from boltzmann.retention.requests import DropRequest

    session = use(ctx)
    request = DropRequest(
        blocks=[_block_id(block) for block in blocks],
        memory_type=_memory_type(memory_type),
        actor=Actor(id=session.settings.actor, kind=ActorKind.HUMAN),
        reason=reason,
    )
    with session.lock:
        try:
            return wire.cascade(session.brain.plan_drop(request))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=DESTRUCTIVE)
def drop(
    ctx: Context,
    blocks: Annotated[list[str], Field(description="The blocks to remove.")],
    memory_type: Annotated[str, Field(description="Which module they live in.")],
    reason: Annotated[str, Field(description="Why. Recorded in provenance, always.")],
    confirm: Annotated[bool, Field(description="Must be true. Call plan_drop first to see the cascade.")] = False,
) -> dict[str, Any]:
    """Remove blocks and rebuild the affected Merkle roots, cascading to whatever cited them.

    The removal itself is recorded in provenance -- what was removed, by whom, why -- because forgetting
    that you forgot is not forgetting, it is losing track. Older retained roots keep verifying unchanged."""
    from boltzmann.blocks.provenance import Actor, ActorKind
    from boltzmann.exceptions import BoltzmannError
    from boltzmann.retention.requests import DropRequest

    session = use(ctx)
    if not confirm:
        raise ToolError("refusing to drop without confirm=true. Call plan_drop first to see what goes with it")

    request = DropRequest(
        blocks=[_block_id(block) for block in blocks],
        memory_type=_memory_type(memory_type),
        actor=Actor(id=session.settings.actor, kind=ActorKind.HUMAN),
        reason=reason,
    )
    with session.lock:
        try:
            return wire.dropped(session.brain.drop(request))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def supersede_block(
    ctx: Context,
    block: Annotated[str, Field(description="The newer block that replaces the other.")],
    superseded: Annotated[str, Field(description="The block being replaced.")],
    memory_type: Annotated[str, Field(description="Which module both live in.")],
    reason: Annotated[str | None, Field(description="Why the replacement happened.")] = None,
) -> dict[str, Any]:
    """Record that one block replaces another.

    Nothing is removed. The superseded block stays in the composition and keeps proving into the root; what
    changes is accessibility -- retrieval stops returning it unless asked. That is how a correction keeps
    the history verifiable instead of rewriting it."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            return wire.supersession(
                session.brain.supersede(
                    _block_id(block), _block_id(superseded), _memory_type(memory_type), reason=reason
                )
            )
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def demote_block(
    ctx: Context,
    block: Annotated[str, Field(description="The block to stop surfacing.")],
    memory_type: Annotated[str, Field(description="Which module it lives in.")],
    reason: Annotated[str | None, Field(description="Why it should stop surfacing.")] = None,
) -> dict[str, Any]:
    """Stop a block from surfacing in retrieval without removing it or naming a replacement. The default
    for episodic memory, which is append-only: an episode cannot be dropped, only demoted."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            return wire.supersession(session.brain.demote(_block_id(block), _memory_type(memory_type), reason=reason))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
def prune(
    ctx: Context,
    dry_run: Annotated[bool, Field(description="Report what would be reclaimed without deleting it.")] = True,
) -> dict[str, Any]:
    """Reclaim storage no retained root needs, by marking what every retained version names and sweeping
    the rest. Dry run by default: a block reachable from any retained root is never swept, but seeing the
    list first is cheap."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            return wire.prune(session.brain.prune(dry_run=dry_run))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


# --- Distribution --------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def pack_local(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Tag to record locally. Defaults to the configured one.")] = None,
) -> dict[str, Any]:
    """Materialize the OCI artifact on disk, with no network at all. The brain directory becomes a real
    OCI artifact any tool can copy -- publishing is a copy, not a conversion."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            return wire.manifest(session.brain.pack(tag=tag or session.settings.tag))
        except BoltzmannError as error:
            raise ToolError(str(error)) from error


@mcp.tool(annotations=READ_ONLY)
async def plan_pull(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Which tag to inspect. Defaults to the configured one.")] = None,
    memory_types: Annotated[
        list[str] | None, Field(description="Only consider these modules, for a selective install.")
    ] = None,
) -> dict[str, Any]:
    """Report what installing a remote version would cost, for one manifest request. Layers already held
    locally are reused by digest and are not listed as needed."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            plan = await session.brain.plan_pull(
                session.client,
                session.settings.registry,
                tag or session.settings.tag,
                modules=[_memory_type(name) for name in memory_types] if memory_types else None,
            )
        except BoltzmannError as error:
            raise ToolError(str(error)) from error
    return plan.model_dump(mode="json")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def pull_brain(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Which tag to install. Defaults to the configured one.")] = None,
    memory_types: Annotated[
        list[str] | None, Field(description="Install only these modules. Omit for all of them.")
    ] = None,
) -> dict[str, Any]:
    """Install a version from the registry. A selective install is a first-class outcome, not a partial
    failure: taking the semantic module without the canonical one is a legitimate way to consume a brain,
    and the manifest records what was left out."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    with session.lock:
        try:
            installed = await session.brain.pull(
                session.client,
                session.settings.registry,
                tag or session.settings.tag,
                modules=[_memory_type(name) for name in memory_types] if memory_types else None,
            )
        except BoltzmannError as error:
            raise ToolError(str(error)) from error
    return wire.snapshot(installed)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def push_brain(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Tag to publish under. Defaults to the configured one.")] = None,
    memory_types: Annotated[
        list[str] | None, Field(description="Publish only these modules. Omit for all of them.")
    ] = None,
    force: Annotated[bool, Field(description="Publish even if the remote version is not in this history.")] = False,
) -> dict[str, Any]:
    """Publish this version to the registry, uploading only the blobs it does not already have.

    A push that would overwrite a remote version absent from this brain's history is refused: the protocol
    defines no merge for divergent brains, so the safe move is to report where the two parted rather than
    pick a winner."""
    from boltzmann.exceptions import BoltzmannError

    session = use(ctx)
    if not session.settings.authenticated:
        raise ToolError(
            "no credentials configured, so this push would be rejected by the registry. "
            "Set DOCKER_USERNAME and DOCKER_TOKEN"
        )

    with session.lock:
        try:
            digest = await session.brain.push(
                session.client,
                session.settings.registry,
                tag or session.settings.tag,
                force=force,
                modules=[_memory_type(name) for name in memory_types] if memory_types else None,
            )
        except BoltzmannError as error:
            raise ToolError(str(error)) from error

    return {
        "manifest": str(digest),
        "reference": f"{session.settings.registry}:{tag or session.settings.tag}",
        "snapshot": str(session.brain.snapshot().digest),
    }


def main() -> int:
    """
    Run the server.

    Returns:
        int: ``0`` on a clean shutdown.
    """
    parser = argparse.ArgumentParser(prog="boltzmann-mcp", description="An MCP server over a Boltzmann brain.")
    parser.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind when serving over HTTP")
    parser.add_argument("--port", type=int, default=8000, help="port to bind when serving over HTTP")
    arguments = parser.parse_args()

    if arguments.http:
        mcp.run(transport="http", host=arguments.host, port=arguments.port)
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
