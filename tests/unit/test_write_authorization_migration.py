from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings
from app.models.workbench_review_write_authorization import WorkbenchReviewWriteAuthorization


def test_write_authorization_upgrade_downgrade_and_metadata_contract(tmp_path: Path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'authorization-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "202607170027")
        assert WorkbenchReviewWriteAuthorization.__tablename__ not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        inspector = inspect(engine)
        name = WorkbenchReviewWriteAuthorization.__tablename__
        assert {c["name"] for c in inspector.get_columns(name)} == set(
            WorkbenchReviewWriteAuthorization.__table__.columns.keys()
        )
        assert {c["name"] for c in inspector.get_check_constraints(name)} == {
            c.name
            for c in WorkbenchReviewWriteAuthorization.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert {i["name"] for i in inspector.get_indexes(name)} == {
            i.name for i in WorkbenchReviewWriteAuthorization.__table__.indexes
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "202607170028"
        command.downgrade(config, "202607170027")
        assert name not in inspect(engine).get_table_names()
        assert "workbench_review_decisions" in inspect(engine).get_table_names()
        assert "execution_source_invoice_evidence" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        get_settings.cache_clear()
