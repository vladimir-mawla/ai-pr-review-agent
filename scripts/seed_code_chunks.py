"""M9 demo script: seed ``code_chunks`` from this repo's own Python source.

Owns: PLAN.md's M9 demo command's middle step -- applying
``migrations/scripts/dev-pgvector-init.sql`` against
``Settings.pgvector_url``, then walking ``--repo`` for ``*.py`` files,
chunking each one, embedding every chunk via the configured ``Embedder``
(``backend.memory.embedder.get_embedder`` -- ``DeterministicFixtureEmbedder``
unless ``EMBEDDER_BACKEND=openai`` and a real ``OPENAI_API_KEY`` are both
set), and inserting the result into ``code_chunks`` for
``tests/integration/test_hybrid_retrieval.py``'s demo-command run to query.

Not test code, and not named in M9's literal freeze-boundary file list --
required for that milestone's own demo command (which invokes it by this
exact path, `python scripts/seed_code_chunks.py --repo .`) to run at all,
the same category of disclosed-but-not-freeze-boundary-listed script as
M7's ``scripts/run_fixture_review.py`` and M2's
``scripts/send_signed_webhook.py``.

CHUNKING STRATEGY -- AST-based, on function/class boundaries, NOT fixed-
size line/character splitting. Splitting a Python file into arbitrary
fixed-size windows routinely cuts a function signature away from its body,
or straddles a class boundary, producing chunks that read as out-of-context
fragments and hurt retrieval relevance -- there is no principled reason a
chunk should ever end mid-function. Parsing into the AST and chunking on
each TOP-LEVEL ``FunctionDef``/``AsyncFunctionDef``/``ClassDef`` instead
keeps every chunk a complete, self-contained unit (a whole function, or a
whole class including its methods) -- the natural retrieval granularity for
"find the code that implements X". Leftover top-level code (imports, module
docstring, module-level constants -- anything not inside a top-level
def/class) becomes exactly one additional "module preamble" chunk per file,
rather than being silently dropped. This is intentionally ONE level of
chunking (top-level defs/classes), not recursive into nested functions or
class methods individually -- a coarser granularity than a
production-grade chunker might use, but the right trade-off for this
milestone's small local corpus and the retrieval tests it needs to support
(finding "the function/class that does X"), not a general-purpose code
search product.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from backend.core.settings import get_settings
from backend.memory.context_retriever import HybridRetriever
from backend.memory.embedder import get_embedder
from backend.memory.tiger_client import apply_migrations, connect

# Directories never worth chunking: VCS internals, virtualenvs, caches, and
# this genesis-kit's own bookkeeping -- none of it is "this project's
# source code" in the sense a code-review retrieval system should ever
# surface as a grounding chunk.
_EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".import_linter_cache",
    "node_modules",
    ".genesis",
}

# How many chunks are sent to the embedder in one call. Batching (rather
# than one embedder.embed() call per chunk) matters most for the real
# OpenAIEmbedder path -- fewer, larger HTTP requests instead of one round
# trip per chunk -- and is free for DeterministicFixtureEmbedder either
# way (pure local computation, no I/O to batch).
_EMBED_BATCH_SIZE = 64


def _iter_python_files(repo_root: Path) -> list[Path]:
    """Every ``*.py`` file under ``repo_root``, excluding VCS/venv/cache noise, sorted for determinism."""
    return sorted(
        path
        for path in repo_root.rglob("*.py")
        if not any(part in _EXCLUDED_DIR_NAMES for part in path.parts)
    )


def _chunk_python_file(source: str) -> list[str]:
    """Split ``source`` into top-level function/class chunks plus one leftover chunk.

    See this module's docstring for the full "why AST, not fixed-size
    splitting" reasoning. A file that fails to parse (a syntax error, or
    genuinely not Python despite the ``.py`` extension) falls back to one
    single whole-file chunk rather than being silently skipped -- still
    useful as a keyword/semantic search target even un-chunked, and never
    worse than dropping it entirely.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [source] if source.strip() else []

    lines = source.splitlines(keepends=True)
    line_is_covered = [False] * len(lines)
    chunks: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        segment = ast.get_source_segment(source, node)
        if not segment or not segment.strip():
            continue
        chunks.append(segment)
        start_line = node.lineno - 1
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        for line_index in range(start_line, end_line):
            if 0 <= line_index < len(line_is_covered):
                line_is_covered[line_index] = True

    leftover = "".join(
        line for line, covered in zip(lines, line_is_covered, strict=True) if not covered
    ).strip()
    if leftover:
        chunks.append(leftover)

    # A file with no top-level def/class and no meaningful leftover (e.g.
    # a blank or whitespace-only __init__.py) legitimately produces zero
    # chunks -- that's correct, not a bug to work around.
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=".",
        help="Repo root to walk for .py source files (PLAN.md's demo command passes '.').",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on the number of chunks seeded (for fast local iteration).",
    )
    args = parser.parse_args()

    settings = get_settings()
    apply_migrations(settings.pgvector_url)

    repo_root = Path(args.repo).resolve()
    chunks: list[tuple[str, str]] = []
    for path in _iter_python_files(repo_root):
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        relative_path = str(path.relative_to(repo_root))
        for chunk_content in _chunk_python_file(source):
            chunks.append((relative_path, chunk_content))

    if args.limit is not None:
        chunks = chunks[: args.limit]

    if not chunks:
        print(f"No Python source chunks found under {repo_root}", file=sys.stderr)
        return 1

    # Idempotent re-seed: code_chunks carries no append-only invariant
    # (unlike agent_events -- this is a derived retrieval index over this
    # repo's own source, not an audit trail), and docker-compose.yml's
    # pgvector service has no volume, so TRUNCATE-and-reload on every run
    # keeps repeated invocations (this script re-run, or the demo command,
    # or the test suite's own module fixture) safe and gives a clean,
    # reproducible corpus rather than an ever-growing pile of duplicate
    # rows across runs.
    with connect(settings.pgvector_url) as conn:
        conn.execute("TRUNCATE code_chunks")

    embedder = get_embedder(settings)
    retriever = HybridRetriever(settings.pgvector_url, embedder, settings=settings)

    inserted = 0
    for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
        embeddings = embedder.embed([content for _path, content in batch])
        for (relative_path, content), embedding in zip(batch, embeddings, strict=True):
            retriever.insert_embedded_chunk(relative_path, content, embedding)
            inserted += 1

    print(
        f"Seeded {inserted} code chunks from {repo_root} into code_chunks "
        f"(embedder_backend={settings.embedder_backend})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
