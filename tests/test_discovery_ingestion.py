import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import Company, Job, JobSource
from jobbot.discovery.greenhouse.adapter import parse_greenhouse_board_payload
from jobbot.discovery.ingestion import ingest_discovery_batch


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def load_greenhouse_batch():
    payload = json.loads(
        Path("fixtures/discovery/greenhouse/board_jobs_sample.json").read_text(encoding="utf-8")
    )
    return parse_greenhouse_board_payload(
        company_name="Example Corp",
        board_url="https://boards.greenhouse.io/example",
        payload=payload,
    )


def test_ingest_discovery_batch_inserts_jobs_and_sources():
    session = make_session()
    batch = load_greenhouse_batch()

    counters = ingest_discovery_batch(session, batch)

    assert counters.inserted == 2
    assert counters.updated == 0
    assert counters.duplicate == 0
    assert counters.source_attached == 0
    assert session.query(Job).count() == 2
    assert session.query(JobSource).count() == 2
    assert session.query(Company).count() == 1
    first_job = session.query(Job).filter(Job.external_job_id == "12345").one()
    assert first_job.title == "Senior Backend Engineer"
    assert first_job.title_normalized == "senior backend engineer"
    first_source = session.query(JobSource).order_by(JobSource.id).first()
    assert first_source is not None
    assert first_source.metadata_json["board_url"] == "https://boards.greenhouse.io/example"


def test_ingest_discovery_batch_deduplicates_by_canonical_url():
    session = make_session()
    batch = load_greenhouse_batch()

    first = ingest_discovery_batch(session, batch)
    second = ingest_discovery_batch(session, batch)

    assert first.inserted == 2
    assert second.duplicate == 2
    assert session.query(Job).count() == 2
    assert session.query(JobSource).count() == 2


def test_ingest_discovery_batch_attaches_new_source_via_fingerprint():
    session = make_session()
    batch = load_greenhouse_batch()
    ingest_discovery_batch(session, batch)

    existing_job = session.scalar(select(Job).where(Job.external_job_id == "12345"))
    assert existing_job is not None

    source_batch = load_greenhouse_batch()
    source_batch.jobs[0].source_type = "aggregator_listing"
    source_batch.jobs[0].external_job_id = None
    source_batch.jobs[0].canonical_url = "https://jobs.example.com/postings/senior-backend-engineer"
    source_batch.jobs = [source_batch.jobs[0]]

    counters = ingest_discovery_batch(session, source_batch)

    assert counters.inserted == 0
    assert counters.source_attached == 1
    assert session.query(Job).count() == 2
    assert session.query(JobSource).count() == 3


def test_ingest_discovery_batch_updates_source_metadata_on_repeat():
    session = make_session()
    batch = load_greenhouse_batch()
    ingest_discovery_batch(session, batch)

    batch.jobs[0].metadata["sync_run"] = "second-pass"
    batch.jobs = [batch.jobs[0]]
    ingest_discovery_batch(session, batch)

    source_row = session.query(JobSource).filter(JobSource.source_external_id == "12345").one()
    assert source_row.metadata_json["sync_run"] == "second-pass"


def test_ingest_discovery_batch_preserves_existing_richer_values():
    session = make_session()
    batch = load_greenhouse_batch()
    ingest_discovery_batch(session, batch)

    job = session.query(Job).filter(Job.external_job_id == "12345").one()
    job.salary_text = "$180k-$220k"
    job.seniority = "senior"
    session.commit()

    weaker = load_greenhouse_batch()
    weaker.jobs[0].title = "Senior Backend Engineer II"
    weaker.jobs[0].salary_text = None
    weaker.jobs[0].seniority = None
    weaker.jobs = [weaker.jobs[0]]
    ingest_discovery_batch(session, weaker)

    refreshed = session.query(Job).filter(Job.external_job_id == "12345").one()
    assert refreshed.title == "Senior Backend Engineer II"
    assert refreshed.title_normalized == "senior backend engineer ii"
    assert refreshed.salary_text == "$180k-$220k"
    assert refreshed.seniority == "senior"
