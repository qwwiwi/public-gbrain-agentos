"""Ingest worker -- polls embedding_jobs queue and processes documents."""
import asyncio
import hashlib
import logging
import os
import signal
import sys

import asyncpg

# Worker does not use MCP, but Config requires MCP_PORT.
os.environ.setdefault("MCP_PORT", "0")

from services.shared.config import Config  # noqa: E402
from services.shared.db import close_pool, get_pool  # noqa: E402

from .chunker import chunk_text  # noqa: E402
from .embedder import Embedder  # noqa: E402

logger = logging.getLogger(__name__)

POLL_BATCH_SIZE = 10
POLL_SLEEP_SEC = 5

SQL_FETCH_JOBS = """
    SELECT ej.id, ej.doc_id, d.body, d.path
    FROM embedding_jobs ej
    JOIN documents d ON ej.doc_id = d.id
    WHERE ej.status = 'pending'
    ORDER BY ej.created_at
    LIMIT $1
    FOR UPDATE SKIP LOCKED
"""

SQL_UPDATE_STATUS = """
    UPDATE embedding_jobs
    SET status = $2, updated_at = now() AT TIME ZONE 'utc'
    WHERE id = $1
"""

# Remove stale sibling jobs for the same doc_id before transitioning status.
# Without this, the unique (doc_id, status) constraint can collide with
# leftover rows from earlier runs (e.g. a prior completed/failed entry),
# making SQL_UPDATE_STATUS raise UniqueViolationError and crash the worker.
SQL_CLEANUP_STALE_JOBS = """
    DELETE FROM embedding_jobs
    WHERE doc_id = $1 AND id <> $2
"""

SQL_DELETE_CHUNKS = """
    DELETE FROM chunks WHERE doc_id = $1
"""

SQL_INSERT_CHUNK = """
    INSERT INTO chunks (doc_id, position, content, chunk_hash, embedding, embedded_at)
    VALUES ($1, $2, $3, $4, $5::vector, now())
"""


def _vec_to_str(embedding: list[float]) -> str:
    """Convert embedding list to pgvector string format '[0.1,0.2,...]'."""
    return "[" + ",".join(str(v) for v in embedding) + "]"


class _ShutdownFlag:
    """Simple flag for graceful shutdown."""

    def __init__(self) -> None:
        self.requested: bool = False

    def set(self) -> None:
        self.requested = True


_shutdown = _ShutdownFlag()


def _handle_signal(sig: int, _frame: object) -> None:
    """Signal handler for SIGTERM/SIGINT."""
    logger.info("Received signal %d, requesting shutdown", sig)
    _shutdown.set()


async def _process_job(
    conn: asyncpg.Connection,
    embedder: Embedder,
    job_id: int,
    doc_id: int,
    body: str,
    path: str,
) -> None:
    """Process a single embedding job inside a transaction.

    Args:
        conn: Active asyncpg connection (within transaction).
        embedder: Embedder instance.
        job_id: Embedding job ID.
        doc_id: Document ID.
        body: Document body text.
        path: Document vault path (for logging).
    """
    logger.info("Processing job %d (doc %d, path=%s)", job_id, doc_id, path)

    await conn.execute(SQL_UPDATE_STATUS, job_id, "processing")

    chunks = chunk_text(body)
    if not chunks:
        logger.warning("Job %d: empty body, marking completed", job_id)
        await conn.execute(SQL_UPDATE_STATUS, job_id, "completed")
        return

    embeddings = embedder.embed(chunks)

    await conn.execute(SQL_DELETE_CHUNKS, doc_id)

    for position, (chunk_text_str, embedding) in enumerate(
        zip(chunks, embeddings)
    ):
        chunk_hash = hashlib.sha256(chunk_text_str.encode("utf-8")).hexdigest()
        await conn.execute(
            SQL_INSERT_CHUNK,
            doc_id,
            position,
            chunk_text_str,
            chunk_hash,
            _vec_to_str(embedding),
        )

    await conn.execute(SQL_UPDATE_STATUS, job_id, "completed")
    logger.info(
        "Job %d completed: %d chunks embedded for doc %d",
        job_id,
        len(chunks),
        doc_id,
    )


async def run_worker() -> None:
    """Main worker loop -- poll queue, process jobs, handle shutdown."""
    config = Config()
    pool = await get_pool(config)
    embedder = Embedder(model_name=config.fastembed_model)

    logger.info("Ingest worker started, polling for jobs")

    try:
        while not _shutdown.requested:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(
                        SQL_FETCH_JOBS, POLL_BATCH_SIZE
                    )

                    if not rows:
                        pass  # sleep below
                    else:
                        for row in rows:
                            if _shutdown.requested:
                                logger.info(
                                    "Shutdown requested, stopping mid-batch"
                                )
                                break
                            # Run cleanup outside the savepoint so it
                            # persists even if processing rolls back and we
                            # update the row to 'failed'.
                            await conn.execute(
                                SQL_CLEANUP_STALE_JOBS,
                                row["doc_id"],
                                row["id"],
                            )
                            sp = conn.transaction()
                            await sp.start()
                            try:
                                await _process_job(
                                    conn,
                                    embedder,
                                    row["id"],
                                    row["doc_id"],
                                    row["body"],
                                    row["path"],
                                )
                                await sp.commit()
                            except Exception:
                                logger.exception(
                                    "Job %d failed", row["id"]
                                )
                                await sp.rollback()
                                await conn.execute(
                                    SQL_UPDATE_STATUS,
                                    row["id"],
                                    "failed",
                                )

            if not rows and not _shutdown.requested:
                await asyncio.sleep(POLL_SLEEP_SEC)

    finally:
        logger.info("Ingest worker shutting down")
        await close_pool()


def main() -> None:
    """Entry point for the ingest worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
