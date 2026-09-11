from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base metadata shared by all future ORM models."""
