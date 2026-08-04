import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from tortoise import Tortoise
from fastapi.middleware.cors import CORSMiddleware

from settings import *
from api import router
from tasks import NapCatSync


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = os.getenv("DATABASE_URL", "sqlite://data/yasuo.db")
    os.makedirs("data", exist_ok=True)
    await Tortoise.init(
        db_url=db_url,
        modules={"models": ["models"]},
    )
    await Tortoise.generate_schemas()
    task = asyncio.create_task(NapCatSync().run())
    yield
    task.cancel()
    await Tortoise.close_connections()


def create_app() -> FastAPI:
    app = FastAPI(title="Yasuo Message API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=6273,
        reload=False,
    )
