"""Durable outbound delivery after the physical transaction has committed.

GET before POST recovers an accepted command after a connection loss. Exactly-
once document creation additionally requires the 1C receipt register/transaction;
ERP never claims it from an HTTP 200 alone. A job error cannot undo physical stock.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from modules.wms.physical_models import ExchangeJob


async def flush_exchange(session, adapter, *, limit=10, now=None):
    now = now or datetime.now(timezone.utc)
    query = select(ExchangeJob).where(ExchangeJob.status.in_(["pending", "waiting", "error"]),
        or_(ExchangeJob.next_attempt_at.is_(None), ExchangeJob.next_attempt_at <= now))
    jobs = (await session.scalars(query.order_by(ExchangeJob.id).limit(limit)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))).all()
    processed = 0
    for job in jobs:
        dependencies = sorted(set(job.dependencies or []) | ({job.depends_on} if job.depends_on else set()))
        ready = True
        for dependency in dependencies:
            previous = await session.scalar(select(ExchangeJob).where(ExchangeJob.organization_id == job.organization_id,
                ExchangeJob.operation_id == dependency))
            if previous is None or previous.status != "done":
                job.status, job.error = "waiting", "Ожидается подтверждение зависимой операции"
                job.next_attempt_at = now + timedelta(seconds=30)
                ready = False
                break
        if not ready:
            continue
        if adapter is None or not adapter.available:
            job.status, job.error = "waiting", "HTTP-сервис WMS в 1С не настроен"
            job.next_attempt_at = now + timedelta(seconds=60)
            continue
        job.attempts += 1
        try:
            outcome = await adapter.outcome(job.organization_id, job.operation_id)
            if outcome is None:
                outcome = await adapter.submit(job.payload, job.payload_hash)
            outcome = adapter.validate(outcome, job.organization_id, job.operation_id, job.payload_hash)
            status = outcome["status"]
            if status == "done":
                job.status, job.document_ref, job.document_state = "done", outcome["document_ref"], outcome["document_state"]
                job.error, job.next_attempt_at = "", None
            elif status == "failed":
                # Business failure is a review, not permission to repeatedly POST
                # or create a replacement document with another operation ID.
                job.status, job.error = "review", "Операция отклонена 1С; требуется сверка результата"
                job.next_attempt_at = None
            else:
                job.status, job.error = "waiting", "Операция принята 1С, проведение ещё не подтверждено"
                job.next_attempt_at = now + timedelta(seconds=30)
        except Exception as exc:
            job.status = "error"
            # Exception type is safe; server payloads and credentials are not.
            job.error = f"Обмен не подтверждён ({type(exc).__name__}); результат будет запрошен повторно"
            job.next_attempt_at = now + timedelta(seconds=min(900, 5 * 2 ** min(job.attempts, 8)))
        processed += 1
    await session.flush()
    return processed


async def tick_exchange(session, services):
    gateway = getattr(services, "onec", None)
    # Two requests cap an unavailable 1C's effect on the shared background loop.
    await flush_exchange(session, getattr(gateway, "wms_http", None), limit=2)
