from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from backend.config import DATABASE_URL

# The engine is the low-level connection to PostgreSQL.
# pool_pre_ping=True checks the connection is alive before using it (handles dropped connections).
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, echo=False)

# Session factory — call this to get a database session for a request.
# expire_on_commit=False means objects stay usable after a commit (important in async context).
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Base class all ORM models will inherit from.
class Base(DeclarativeBase):
    pass

# Dependency for FastAPI routes — yields a DB session, closes it after the request.
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
