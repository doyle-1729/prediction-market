from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import engine
from app.models import Base
from app.routes import auth, markets, portfolio
from app.routes import groups

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Prediction Market")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(groups.router)
app.include_router(markets.router)
app.include_router(portfolio.router)
