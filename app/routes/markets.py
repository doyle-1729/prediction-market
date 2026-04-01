from datetime import datetime
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Market, MarketStatus, Order, OrderSide, OrderOutcome, OrderStatus, OrderType, Trade, Position
from app.auth import get_current_user, require_user
from app.matching import match_order, reserve_funds, release_funds, resolve_market

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/groups", status_code=302)


@router.get("/markets/{market_id}", response_class=HTMLResponse)
def market_detail(market_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    market = db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    yes_bids, unified_asks, recent_trades, max_depth = _load_orderbook(market_id, db)
    user_position = db.query(Position).filter_by(user_id=user.id, market_id=market_id).first()
    user_open_orders = (
        db.query(Order)
        .filter(
            Order.user_id == user.id,
            Order.market_id == market_id,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .order_by(Order.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        "market_detail.html",
        {
            "request": request,
            "user": user,
            "market": market,
            "yes_bids": yes_bids,
            "unified_asks": unified_asks,
            "recent_trades": recent_trades,
            "max_depth": max_depth,
            "user_position": user_position,
            "user_open_orders": user_open_orders,
        },
    )


@router.get("/markets/{market_id}/orderbook-partial", response_class=HTMLResponse)
def orderbook_partial(market_id: int, request: Request, db: Session = Depends(get_db)):
    """HTMX partial: returns the live order book + recent trades div for polling."""
    user = get_current_user(request, db)
    if not user:
        return HTMLResponse("", status_code=204)
    market = db.get(Market, market_id)
    if not market:
        return HTMLResponse("", status_code=404)

    yes_bids, unified_asks, recent_trades, max_depth = _load_orderbook(market_id, db)

    return templates.TemplateResponse(
        "orderbook_partial.html",
        {
            "request": request,
            "market": market,
            "yes_bids": yes_bids,
            "unified_asks": unified_asks,
            "recent_trades": recent_trades,
            "max_depth": max_depth,
        },
    )


def _load_orderbook(market_id: int, db: Session):
    yes_bids = (
        db.query(Order)
        .filter(
            Order.market_id == market_id,
            Order.outcome == OrderOutcome.yes,
            Order.side == OrderSide.buy,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .order_by(Order.price.desc(), Order.created_at.asc())
        .all()
    )
    yes_asks_raw = (
        db.query(Order)
        .filter(
            Order.market_id == market_id,
            Order.outcome == OrderOutcome.yes,
            Order.side == OrderSide.sell,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .order_by(Order.price.asc(), Order.created_at.asc())
        .all()
    )
    # NO buys show up as implied YES asks at (100 - no_price)
    no_bids_raw = (
        db.query(Order)
        .filter(
            Order.market_id == market_id,
            Order.outcome == OrderOutcome.no,
            Order.side == OrderSide.buy,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .order_by(Order.price.desc(), Order.created_at.asc())
        .all()
    )
    recent_trades = (
        db.query(Trade)
        .filter(Trade.market_id == market_id)
        .order_by(Trade.created_at.desc())
        .limit(20)
        .all()
    )

    # Aggregate bids by price level (price desc)
    bid_levels: dict[int, dict] = {}
    for o in yes_bids:
        rem = o.quantity - o.filled_quantity
        if rem <= 0:
            continue
        lvl = bid_levels.setdefault(o.price, {"price": o.price, "remaining": 0, "order_count": 0})
        lvl["remaining"] += rem
        lvl["order_count"] += 1
    agg_bids = sorted(bid_levels.values(), key=lambda x: -x["price"])

    # Build unified ask side: YES sells + NO buys at complementary prices
    ask_levels: dict[int, dict] = {}
    for o in yes_asks_raw:
        rem = o.quantity - o.filled_quantity
        if rem <= 0:
            continue
        dp = o.price
        lvl = ask_levels.setdefault(dp, {"display_price": dp, "remaining": 0, "order_count": 0, "type": "yes_sell"})
        if lvl["type"] != "yes_sell":
            lvl["type"] = "mixed"
        lvl["remaining"] += rem
        lvl["order_count"] += 1
    for o in no_bids_raw:
        rem = o.quantity - o.filled_quantity
        if rem <= 0:
            continue
        dp = 100 - o.price
        lvl = ask_levels.setdefault(dp, {"display_price": dp, "remaining": 0, "order_count": 0, "type": "no_buy"})
        if lvl["type"] != "no_buy":
            lvl["type"] = "mixed"
        lvl["remaining"] += rem
        lvl["order_count"] += 1
    unified_asks = sorted(ask_levels.values(), key=lambda e: e["display_price"])

    all_remaining = [e["remaining"] for e in unified_asks] + [e["remaining"] for e in agg_bids]
    max_depth = max(all_remaining) if all_remaining else 1
    return agg_bids, unified_asks, recent_trades, max_depth


@router.post("/markets/{market_id}/order")
def place_order(
    market_id: int,
    request: Request,
    outcome: str = Form(...),
    side: str = Form(...),
    price: int = Form(0),
    quantity: int = Form(...),
    order_type: str = Form("limit"),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    market = db.get(Market, market_id)
    if not market or market.status != MarketStatus.open:
        raise HTTPException(status_code=400, detail="Market is not open")
    if quantity < 1:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")

    try:
        outcome_enum = OrderOutcome(outcome)
        side_enum = OrderSide(side)
        order_type_enum = OrderType(order_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid outcome, side, or order type")

    # Market orders sweep the book: buy at 99¢ (matches everything), sell at 1¢
    if order_type_enum == OrderType.market:
        price = 99 if side_enum == OrderSide.buy else 1
    elif price < 1 or price > 99:
        raise HTTPException(status_code=400, detail="Price must be between 1 and 99")

    if side_enum == OrderSide.buy:
        if not reserve_funds(user, price, quantity):
            raise HTTPException(status_code=400, detail="Insufficient balance")
    else:
        pos = db.query(Position).filter_by(user_id=user.id, market_id=market_id).first()
        owned = 0
        if pos:
            owned = pos.yes_shares if outcome_enum == OrderOutcome.yes else pos.no_shares
        reserved_sells = (
            db.query(func.sum(Order.quantity - Order.filled_quantity))
            .filter(
                Order.user_id == user.id,
                Order.market_id == market_id,
                Order.outcome == outcome_enum,
                Order.side == OrderSide.sell,
                Order.status.in_([OrderStatus.open, OrderStatus.partial]),
            )
            .scalar() or 0
        )
        available = owned - reserved_sells
        if available < quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient shares (you have {max(0, available)} available to sell)",
            )

    order = Order(
        user_id=user.id,
        market_id=market_id,
        outcome=outcome_enum,
        side=side_enum,
        price=price,
        quantity=quantity,
        order_type=order_type_enum,
    )
    db.add(order)
    db.flush()

    match_order(order, db)

    # IOC: cancel any unfilled remainder of a market order
    if order_type_enum == OrderType.market:
        unfilled = order.quantity - order.filled_quantity
        if unfilled > 0:
            if side_enum == OrderSide.buy:
                release_funds(user, price, unfilled)
            order.status = OrderStatus.cancelled

    db.commit()

    return RedirectResponse(f"/markets/{market_id}", status_code=302)


@router.post("/markets/{market_id}/cancel/{order_id}")
def cancel_order(
    market_id: int,
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    order = db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status not in (OrderStatus.open, OrderStatus.partial):
        raise HTTPException(status_code=400, detail="Order cannot be cancelled")

    unfilled = order.quantity - order.filled_quantity
    if order.side == OrderSide.buy and unfilled > 0:
        release_funds(user, order.price, unfilled)

    order.status = OrderStatus.cancelled
    db.commit()
    return RedirectResponse(f"/markets/{market_id}", status_code=302)


@router.post("/markets/{market_id}/cancel-all")
def cancel_all_market_orders(
    market_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    open_orders = (
        db.query(Order)
        .filter(
            Order.user_id == user.id,
            Order.market_id == market_id,
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
    return RedirectResponse(f"/markets/{market_id}", status_code=302)


@router.post("/markets/{market_id}/resolve")
def resolve(
    market_id: int,
    request: Request,
    outcome: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    market = db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    if market.created_by_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the market creator can resolve it")
    if market.status == MarketStatus.resolved:
        raise HTTPException(status_code=400, detail="Market already resolved")

    try:
        outcome_enum = OrderOutcome(outcome)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid outcome")

    market.status = MarketStatus.resolved
    market.resolved_outcome = outcome_enum
    resolve_market(market, db)
    db.commit()
    return RedirectResponse(f"/markets/{market_id}", status_code=302)
