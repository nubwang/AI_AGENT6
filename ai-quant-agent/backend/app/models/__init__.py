"""数据库连接管理（SQLAlchemy 2.0+）"""
from collections.abc import Generator

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.debug,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类"""

    def __repr__(self) -> str:
        cols = [c.name for c in self.__table__.columns]  # type: ignore
        vals = {c: getattr(self, c) for c in cols}
        return f"<{self.__class__.__name__}({vals})>"


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
