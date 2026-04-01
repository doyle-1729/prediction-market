"""
Order book matching engine.

Shares are priced 0–100 (cents), representing probability %.
- Buying YES at 60 means: pay 60¢, receive $1 if YES wins.
- Buying NO at 40 means: pay 40¢, receive $1 if NO wins.

Two kinds of matches:
1. Regular (peer-to-peer): a buy order fills against a resting sell order.
   The seller already owns shares and receives cash.

2. Complementary (pair minting): a YES buy and a NO buy match each other when
   their prices sum to ≥ 100¢.  Neither side needs to own anything first.
   Fresh YES+NO shares are minted and the combined $1 payment becomes the
   prize pool paid out at resolution.  This solves the bootstrap problem where
   no one holds shares to sell initially.

In both cases the resting order is the maker and gets its stated price.
The taker gets the same or a better price; any over-reservation is refunded.
"""

from decimal import Decimal
from sqlalchemy.orm import Session

from app.models import Order, OrderSide, OrderStatus, OrderOutcome, Trade, Position, User


def match_order(new_order: Order, db: Session) -> list[Trade]:
    """
    Attempt to match new_order against resting orders.
    For buy orders: first tries regular (sell-side) matches, then complementary
    (opposite-outcome buy) matches.
    For sell orders: regular matching only.
    Returns list of trades created.
    """
    trades = []
    remaining = new_order.quantity - new_order.filled_quantity

    if new_order.side == OrderSide.buy:
        # ── Step 1: regular matching against resting sell orders ─────────────
        resting_sells = (
            db.query(Order)
            .filter(
                Order.market_id == new_order.market_id,
                Order.outcome == new_order.outcome,
                Order.side == OrderSide.sell,
                Order.status.in_([OrderStatus.open, OrderStatus.partial]),
                Order.price <= new_order.price,
                Order.id != new_order.id,
                Order.user_id != new_order.user_id,  # no self-matching
            )
            .order_by(Order.price.asc(), Order.created_at.asc())
            .with_for_update()
            .all()
        )

        for resting in resting_sells:
            if remaining <= 0:
                break

            fill_qty = min(remaining, resting.quantity - resting.filled_quantity)
            trade_price = resting.price  # maker's price

            trade = Trade(
                market_id=new_order.market_id,
                buy_order_id=new_order.id,
                sell_order_id=resting.id,
                outcome=new_order.outcome,
                price=trade_price,
                quantity=fill_qty,
            )
            db.add(trade)

            new_order.filled_quantity += fill_qty
            resting.filled_quantity += fill_qty
            remaining -= fill_qty
            _update_order_status(resting)

            cost = Decimal(trade_price) * fill_qty / Decimal(100)
            _transfer(
                buyer=new_order.user,
                seller=resting.user,
                outcome=new_order.outcome,
                market_id=new_order.market_id,
                quantity=fill_qty,
                cost=cost,
                db=db,
            )

            # Refund taker over-reservation (bid > trade price)
            if new_order.price > trade_price:
                overcharge = Decimal(new_order.price - trade_price) * fill_qty / Decimal(100)
                new_order.user.balance += overcharge

            trades.append(trade)

        # ── Step 2: complementary matching against opposite-outcome buy orders ─
        # A YES buy at P can match a NO buy at Q when P + Q >= 100.
        # The maker (resting NO buy) gets its price; the taker pays the complement.
        if remaining > 0:
            comp_outcome = (
                OrderOutcome.no if new_order.outcome == OrderOutcome.yes
                else OrderOutcome.yes
            )
            resting_comp = (
                db.query(Order)
                .filter(
                    Order.market_id == new_order.market_id,
                    Order.outcome == comp_outcome,
                    Order.side == OrderSide.buy,
                    Order.status.in_([OrderStatus.open, OrderStatus.partial]),
                    Order.price >= 100 - new_order.price,
                    Order.id != new_order.id,
                    Order.user_id != new_order.user_id,  # no self-matching
                )
                .order_by(Order.price.desc(), Order.created_at.asc())
                .with_for_update()
                .all()
            )

            for comp_order in resting_comp:
                if remaining <= 0:
                    break

                fill_qty = min(remaining, comp_order.quantity - comp_order.filled_quantity)

                # Resolve which side is YES and which is NO
                if new_order.outcome == OrderOutcome.yes:
                    yes_order, no_order = new_order, comp_order
                    # Maker (comp_order = NO) gets its price; taker pays complement
                    yes_price = 100 - comp_order.price
                    no_price  = comp_order.price
                else:
                    yes_order, no_order = comp_order, new_order
                    # Maker (comp_order = YES) gets its price; taker pays complement
                    yes_price = comp_order.price
                    no_price  = 100 - comp_order.price

                trade = Trade(
                    market_id=new_order.market_id,
                    buy_order_id=yes_order.id,
                    sell_order_id=no_order.id,  # NO side plays "seller" role in record
                    outcome=OrderOutcome.yes,
                    price=yes_price,
                    quantity=fill_qty,
                )
                db.add(trade)

                new_order.filled_quantity += fill_qty
                comp_order.filled_quantity += fill_qty
                remaining -= fill_qty
                _update_order_status(comp_order)

                # Mint fresh shares — no cash changes hands on trade;
                # both buyers already paid into the prize pool at order placement.
                _mint_shares(
                    yes_buyer=yes_order.user,
                    no_buyer=no_order.user,
                    market_id=new_order.market_id,
                    quantity=fill_qty,
                    db=db,
                )

                # Refund over-reservation to taker (new_order)
                taker_price = yes_price if new_order.outcome == OrderOutcome.yes else no_price
                if new_order.price > taker_price:
                    overcharge = Decimal(new_order.price - taker_price) * fill_qty / Decimal(100)
                    new_order.user.balance += overcharge

                trades.append(trade)

    else:
        # ── Sell order: regular matching only ────────────────────────────────
        # Sell against resting buy orders (same outcome) at price >= sell price.
        resting_buys = (
            db.query(Order)
            .filter(
                Order.market_id == new_order.market_id,
                Order.outcome == new_order.outcome,
                Order.side == OrderSide.buy,
                Order.status.in_([OrderStatus.open, OrderStatus.partial]),
                Order.price >= new_order.price,
                Order.id != new_order.id,
                Order.user_id != new_order.user_id,  # no self-matching
            )
            .order_by(Order.price.desc(), Order.created_at.asc())
            .with_for_update()
            .all()
        )

        for resting in resting_buys:
            if remaining <= 0:
                break

            fill_qty = min(remaining, resting.quantity - resting.filled_quantity)
            trade_price = resting.price  # maker's price

            trade = Trade(
                market_id=new_order.market_id,
                buy_order_id=resting.id,
                sell_order_id=new_order.id,
                outcome=new_order.outcome,
                price=trade_price,
                quantity=fill_qty,
            )
            db.add(trade)

            new_order.filled_quantity += fill_qty
            resting.filled_quantity += fill_qty
            remaining -= fill_qty
            _update_order_status(resting)

            cost = Decimal(trade_price) * fill_qty / Decimal(100)
            _transfer(
                buyer=resting.user,
                seller=new_order.user,
                outcome=new_order.outcome,
                market_id=new_order.market_id,
                quantity=fill_qty,
                cost=cost,
                db=db,
            )

            trades.append(trade)

    _update_order_status(new_order)
    db.flush()
    return trades


# ── Helpers ───────────────────────────────────────────────────────────────────

def _update_order_status(order: Order):
    if order.filled_quantity >= order.quantity:
        order.status = OrderStatus.filled
    elif order.filled_quantity > 0:
        order.status = OrderStatus.partial


def _transfer(
    buyer: User,
    seller: User,
    outcome: OrderOutcome,
    market_id: int,
    quantity: int,
    cost: Decimal,
    db: Session,
):
    """
    Peer-to-peer share transfer: buyer pays cost (already reserved), seller
    receives cash and loses shares, buyer gains shares.
    """
    seller.balance += cost

    buyer_pos  = _get_or_create_position(buyer.id,  market_id, db)
    seller_pos = _get_or_create_position(seller.id, market_id, db)

    if outcome == OrderOutcome.yes:
        buyer_pos.yes_shares  += quantity
        seller_pos.yes_shares -= quantity
    else:
        buyer_pos.no_shares  += quantity
        seller_pos.no_shares -= quantity


def _mint_shares(
    yes_buyer: User,
    no_buyer: User,
    market_id: int,
    quantity: int,
    db: Session,
):
    """
    Complementary pair creation: mint fresh YES shares for yes_buyer and NO
    shares for no_buyer.  No cash moves on trade — both buyers' reserved funds
    collectively form the $1-per-pair prize pool paid out at resolution.
    """
    yes_pos = _get_or_create_position(yes_buyer.id, market_id, db)
    no_pos  = _get_or_create_position(no_buyer.id,  market_id, db)
    yes_pos.yes_shares += quantity
    no_pos.no_shares   += quantity


def _get_or_create_position(user_id: int, market_id: int, db: Session) -> Position:
    pos = db.query(Position).filter_by(user_id=user_id, market_id=market_id).first()
    if not pos:
        pos = Position(user_id=user_id, market_id=market_id)
        db.add(pos)
        db.flush()
    return pos


def reserve_funds(user: User, price: int, quantity: int) -> bool:
    """Deduct funds from user balance when placing a buy order. Returns False if insufficient."""
    cost = Decimal(price) * quantity / Decimal(100)
    if user.balance < cost:
        return False
    user.balance -= cost
    return True


def release_funds(user: User, price: int, quantity: int):
    """Refund reserved funds when a buy order is cancelled."""
    cost = Decimal(price) * quantity / Decimal(100)
    user.balance += cost


def resolve_market(market, db: Session):
    """
    Pay out winning positions: each winning share pays $1.
    Cancel all open/partial orders and refund reserved funds.

    Money accounting:
    - Peer-to-peer trades: seller already received cash at trade time.
      Winner's $1 payout comes from the buyer's original reservation.
    - Complementary (minted) pairs: both buyers' reservations collectively
      equal $1 per pair, so paying $1 per winning share is always funded.
    """
    outcome = market.resolved_outcome

    open_orders = (
        db.query(Order)
        .filter(
            Order.market_id == market.id,
            Order.status.in_([OrderStatus.open, OrderStatus.partial]),
        )
        .all()
    )
    for order in open_orders:
        unfilled = order.quantity - order.filled_quantity
        if order.side == OrderSide.buy and unfilled > 0:
            release_funds(order.user, order.price, unfilled)
        order.status = OrderStatus.cancelled

    positions = db.query(Position).filter_by(market_id=market.id).all()
    for pos in positions:
        winning_shares = pos.yes_shares if outcome == OrderOutcome.yes else pos.no_shares
        if winning_shares > 0:
            pos.user.balance += Decimal(winning_shares)

    db.flush()
