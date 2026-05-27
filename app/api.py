from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Field, Session, SQLModel, create_engine, select
import os

from db import Person

postgres_url = os.getenv("DATABASE_URL", "postgresql://postgres:admin@db:5432/postgres")
engine = create_engine(postgres_url, echo=True)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI()


@app.post("/persons/")
def create_person(person: Person, session: SessionDep):
    session.add(person)
    session.commit()
    session.refresh(person)

    return person


@app.get("/")
async def read_token(token: Annotated[str, Depends(oauth2_scheme)]):
    return {"token": token}


@app.get("/persons/{persons_id}")
def get_person(person: Person, session: SessionDep):
    session.add(person)
    session.commit()
    session.refresh(person)

    return person
