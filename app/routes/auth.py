import os
from decimal import Decimal
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import hash_password, verify_password, create_session, get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STARTING_BALANCE = Decimal(os.getenv("STARTING_BALANCE", "1000"))


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    if len(username) < 3:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Username must be at least 3 characters."}
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Password must be at least 6 characters."}
        )
    existing = db.query(User).filter_by(username=username).first()
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Username already taken."}
        )
    user = User(
        username=username,
        password_hash=hash_password(password),
        balance=STARTING_BALANCE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    response = RedirectResponse("/", status_code=302)
    create_session(response, user.id)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    user = db.query(User).filter_by(username=username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "next": next, "error": "Invalid username or password."}
        )
    response = RedirectResponse(next or "/", status_code=302)
    create_session(response, user.id)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("pm_session")
    return response
