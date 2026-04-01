from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Order, Position, Trade, User, OrderStatus, OrderSide
from app.auth import get_current_user, require_user
from app.matching import release_funds

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    open_orders = (
        db.query(Order)
        .filter(
            Order.user_id == user.id,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .order_by(Order.created_at.desc())
        .all()
    )
    positions = (
        db.query(Position)
        .filter(
            Position.user_id == user.id,
            (Position.yes_shares != 0) | (Position.no_shares != 0),
        )
        .all()
    )
    recent_trades = (
        db.query(Trade)
        .filter(
            (Trade.buy_order_id.in_(
                db.query(Order.id).filter(Order.user_id == user.id)
            ))
            | (Trade.sell_order_id.in_(
                db.query(Order.id).filter(Order.user_id == user.id)
            ))
        )
        .order_by(Trade.created_at.desc())
        .limit(50)
        .all()
    )

    return templates.TemplateResponse(
        "portfolio.html",
        {
            "request": request,
            "user": user,
            "open_orders": open_orders,
            "positions": positions,
            "recent_trades": recent_trades,
        },
    )


@router.post("/portfolio/cancel-all")
def cancel_all_orders(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    open_orders = (
        db.query(Order)
        .filter(
            Order.user_id == user.id,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .all()
    )
    for order in open_orders:
        unfilled = order.quantity - order.filled_quantity
        if order.side == OrderSide.buy and unfilled > 0:
            release_funds(user, order.price, unfilled)
        order.status = OrderStatus.cancelled
    db.commit()
    return RedirectResponse("/portfolio", status_code=302)


@router.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    users = db.query(User).order_by(User.balance.desc()).all()
    return templates.TemplateResponse(
        "leaderboard.html", {"request": request, "user": user, "users": users}
    )
