from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.db.base import Base
from jobbot.db import models  # noqa: F401
from jobbot.db.models import CandidateFact
from jobbot.profiles.schemas import CandidateFactInput, CandidateProfileImport
from jobbot.profiles.service import import_candidate_profile


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_import_candidate_profile_creates_slug_and_facts():
    session = make_session()
    payload = CandidateProfileImport(
        name="Jane Doe",
        personal_details={"email": "jane@example.com"},
        facts=[
            CandidateFactInput(category="employment", content="Built internal dashboards in React"),
            CandidateFactInput(category="skills", content="Python and SQL"),
        ],
    )

    profile = import_candidate_profile(session, payload)
    facts = session.query(CandidateFact).filter(CandidateFact.candidate_profile_id == profile.id).all()

    assert profile.slug == "jane-doe"
    assert len(facts) == 2
    assert facts[0].fact_key == "employment-001"
    assert facts[1].fact_key == "skills-002"


def test_import_candidate_profile_replace_existing_rewrites_facts():
    session = make_session()
    original = CandidateProfileImport(
        name="Jane Doe",
        slug="jane",
        facts=[CandidateFactInput(category="employment", content="Original fact")],
    )
    import_candidate_profile(session, original)

    replacement = CandidateProfileImport(
        name="Jane Doe Updated",
        slug="jane",
        facts=[CandidateFactInput(category="skills", content="Replacement fact")],
    )
    profile = import_candidate_profile(session, replacement, replace_existing=True)
    facts = session.query(CandidateFact).filter(CandidateFact.candidate_profile_id == profile.id).all()

    assert profile.name == "Jane Doe Updated"
    assert len(facts) == 1
    assert facts[0].content == "Replacement fact"
