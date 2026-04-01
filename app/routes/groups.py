from datetime import datetime
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Group, GroupMembership, GroupRole, Market, User
from app.auth import get_current_user, require_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/groups", response_class=HTMLResponse)
def groups_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    memberships = (
        db.query(GroupMembership)
        .filter_by(user_id=user.id)
        .all()
    )
    groups = [m.group for m in memberships]
    return templates.TemplateResponse(
        "groups.html", {"request": request, "user": user, "groups": groups}
    )


@router.get("/groups/create", response_class=HTMLResponse)
def create_group_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    return templates.TemplateResponse("create_group.html", {"request": request, "user": user})


@router.post("/groups/create")
def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    name = name.strip()
    if len(name) < 2:
        return templates.TemplateResponse(
            "create_group.html",
            {"request": request, "user": user, "error": "Group name must be at least 2 characters."},
        )
    group = Group(
        name=name,
        description=description.strip() or None,
        created_by_id=user.id,
    )
    db.add(group)
    db.flush()
    # Creator automatically becomes admin member
    membership = GroupMembership(group_id=group.id, user_id=user.id, role=GroupRole.admin)
    db.add(membership)
    db.commit()
    db.refresh(group)
    return RedirectResponse(f"/groups/{group.id}", status_code=302)


@router.post("/groups/join")
def join_group(
    request: Request,
    invite_code: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    group = db.query(Group).filter_by(invite_code=invite_code.strip()).first()
    if not group:
        # Re-render groups page with error
        memberships = db.query(GroupMembership).filter_by(user_id=user.id).all()
        groups = [m.group for m in memberships]
        return templates.TemplateResponse(
            "groups.html",
            {"request": request, "user": user, "groups": groups, "join_error": "Invalid invite code."},
        )
    existing = db.query(GroupMembership).filter_by(group_id=group.id, user_id=user.id).first()
    if not existing:
        membership = GroupMembership(group_id=group.id, user_id=user.id, role=GroupRole.member)
        db.add(membership)
        db.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=302)


@router.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    # Must be a member to view
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user.id).first()
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this group")

    markets = (
        db.query(Market)
        .filter_by(group_id=group_id)
        .order_by(Market.created_at.desc())
        .all()
    )
    # Leaderboard: members ranked by balance
    member_ids = [m.user_id for m in group.memberships]
    leaderboard = (
        db.query(User)
        .filter(User.id.in_(member_ids))
        .order_by(User.balance.desc())
        .all()
    )
    return templates.TemplateResponse(
        "group_detail.html",
        {
            "request": request,
            "user": user,
            "group": group,
            "membership": membership,
            "markets": markets,
            "leaderboard": leaderboard,
        },
    )


@router.get("/groups/{group_id}/markets/create", response_class=HTMLResponse)
def create_market_page(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user.id).first()
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this group")
    return templates.TemplateResponse(
        "create_market.html", {"request": request, "user": user, "group": group}
    )


@router.post("/groups/{group_id}/markets/create")
def create_market(
    group_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    closes_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user.id).first()
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this group")

    close_dt = None
    if closes_at:
        try:
            close_dt = datetime.fromisoformat(closes_at)
        except ValueError:
            return templates.TemplateResponse(
                "create_market.html",
                {"request": request, "user": user, "group": group, "error": "Invalid date format."},
            )
    market = Market(
        title=title.strip(),
        description=description.strip() or None,
        created_by_id=user.id,
        group_id=group_id,
        closes_at=close_dt,
    )
    db.add(market)
    db.commit()
    db.refresh(market)
    return RedirectResponse(f"/markets/{market.id}", status_code=302)
