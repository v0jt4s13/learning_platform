from __future__ import annotations

from typing import Any

from flask import Flask
from flask_login import LoginManager
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, relationship, scoped_session, sessionmaker


class BaseModel(DeclarativeBase):
    pass


class SimpleSQLAlchemy:
    Column = Column
    Integer = Integer
    String = String
    Text = Text
    DateTime = DateTime
    Enum = Enum
    ForeignKey = ForeignKey
    relationship = staticmethod(relationship)

    def __init__(self) -> None:
        self.Model = BaseModel
        self.session: scoped_session | None = None
        self._engine = None

    def init_app(self, app: Flask) -> None:
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        connect_args: dict[str, Any] = {}
        if uri.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        engine = create_engine(uri, future=True, connect_args=connect_args)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        self.session = scoped_session(factory)
        self.Model.metadata.bind = engine
        self.Model.query = self.session.query_property()  # type: ignore[attr-defined]

        @app.teardown_appcontext
        def cleanup(exception: Exception | None) -> None:  # noqa: ARG001 - Flask contract
            if self.session is not None:
                self.session.remove()

        if app.config.get("SQLALCHEMY_CREATE_TABLES", True):
            self.Model.metadata.create_all(engine)

    def add(self, instance):  # pragma: no cover - passthrough helper
        self.session.add(instance)  # type: ignore[union-attr]

    def commit(self):  # pragma: no cover
        self.session.commit()  # type: ignore[union-attr]

    def drop_all(self):  # pragma: no cover - only used in tests
        bind = self.Model.metadata.bind
        if bind is not None:
            self.Model.metadata.drop_all(bind=bind)

    def create_all(self):  # pragma: no cover
        bind = self.Model.metadata.bind
        if bind is not None:
            self.Model.metadata.create_all(bind=bind)


login_manager = LoginManager()
db = SimpleSQLAlchemy()
