from sqlalchemy.engine import make_url

from quant.config import Settings

HOSTILE = "p@ss:w/rd#?%&+ $ü"


def test_password_with_url_reserved_characters_round_trips() -> None:
    settings = Settings(database_url=None, db_host="db", db_password=HOSTILE)
    url = settings.db_url()
    assert url.password == HOSTILE and url.host == "db" and url.database == "quant"
    # What Alembic and SQLAlchemy see after rendering must parse back to the same parts.
    parsed = make_url(url.render_as_string(hide_password=False))
    assert parsed.password == HOSTILE and parsed.host == "db" and parsed.port == 5432


def test_full_url_still_supported() -> None:
    url = Settings(database_url="postgresql+psycopg://u:p@h:5433/x").db_url()
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "u",
        "p",
        "h",
        5433,
        "x",
    )
