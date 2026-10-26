"""DuckDB Local Columnar Storage Manager.

Provides high-performance, embedded analytical persistence for:
- Tracked Baskets and Individual Legs
- Orders, Fills, and Realized PnL
- Sharp de-vigged odds history
- Pod performance metrics
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import duckdb
from src.config import settings
from src.core.models import Basket, BasketLeg, Order, Position, SharpOdds

logger = logging.getLogger(__name__)


class DuckDBManager:
    """Manages DuckDB local database schema and persistence."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DUCKDB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        """Initializes tables for baskets, legs, orders, fills, and odds."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS baskets (
                basket_id VARCHAR PRIMARY KEY,
                created_at TIMESTAMP,
                status VARCHAR,
                total_invested_cents BIGINT,
                total_realized_pnl_cents BIGINT,
                total_unrealized_pnl_cents BIGINT,
                updated_at TIMESTAMP
            );
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS basket_legs (
                basket_id VARCHAR,
                ticker VARCHAR,
                market_title VARCHAR,
                target_entry_price INTEGER,
                actual_entry_price INTEGER,
                contracts INTEGER,
                status VARCHAR,
                entry_order_id VARCHAR,
                tp_order_id VARCHAR,
                tp_price INTEGER,
                current_price INTEGER,
                current_sharp_prob DOUBLE,
                pnl_cents BIGINT,
                guillotine_triggered BOOLEAN,
                exit_price INTEGER,
                PRIMARY KEY (basket_id, ticker)
            );
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR PRIMARY KEY,
                client_order_id VARCHAR,
                ticker VARCHAR,
                action VARCHAR,
                side VARCHAR,
                count INTEGER,
                yes_price INTEGER,
                no_price INTEGER,
                order_type VARCHAR,
                post_only BOOLEAN,
                sell_position_floor INTEGER,
                cancel_on_pause BOOLEAN,
                status VARCHAR,
                filled_count INTEGER,
                maker_fee_cents INTEGER,
                taker_fee_cents INTEGER,
                strategy_pod VARCHAR,
                basket_id VARCHAR,
                created_at TIMESTAMP
            );
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                fill_id VARCHAR PRIMARY KEY,
                order_id VARCHAR,
                ticker VARCHAR,
                action VARCHAR,
                side VARCHAR,
                count INTEGER,
                fill_price_cents INTEGER,
                is_maker BOOLEAN,
                fee_cents INTEGER,
                strategy_pod VARCHAR,
                basket_id VARCHAR,
                timestamp TIMESTAMP
            );
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS devigged_odds (
                event_id VARCHAR,
                sport VARCHAR,
                team_home VARCHAR,
                team_away VARCHAR,
                bookmaker VARCHAR,
                home_devigged_prob DOUBLE,
                away_devigged_prob DOUBLE,
                timestamp TIMESTAMP
            );
        """)
        logger.info("DuckDB schema initialized at %s", self.db_path)

    # -------------------------------------------------------------------------
    # Basket Persistence
    # -------------------------------------------------------------------------
    def save_basket(self, basket: Basket) -> None:
        """Upserts basket and its constituent legs."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO baskets (
                basket_id, created_at, status, total_invested_cents,
                total_realized_pnl_cents, total_unrealized_pnl_cents, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                basket.basket_id,
                basket.created_at,
                basket.status,
                basket.total_invested_cents,
                basket.total_realized_pnl_cents,
                basket.total_unrealized_pnl_cents,
                now,
            ),
        )

        for leg in basket.legs:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO basket_legs (
                    basket_id, ticker, market_title, target_entry_price,
                    actual_entry_price, contracts, status, entry_order_id,
                    tp_order_id, tp_price, current_price, current_sharp_prob,
                    pnl_cents, guillotine_triggered, exit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basket.basket_id,
                    leg.ticker,
                    leg.market_title,
                    leg.target_entry_price,
                    leg.actual_entry_price,
                    leg.contracts,
                    leg.status,
                    leg.entry_order_id,
                    leg.tp_order_id,
                    leg.tp_price,
                    leg.current_price,
                    leg.current_sharp_prob,
                    leg.pnl_cents,
                    leg.guillotine_triggered,
                    leg.exit_price,
                ),
            )

    def get_basket(self, basket_id: str) -> Optional[Basket]:
        row = self.conn.execute(
            "SELECT basket_id, created_at, status, total_invested_cents, total_realized_pnl_cents, total_unrealized_pnl_cents FROM baskets WHERE basket_id = ?",
            [basket_id],
        ).fetchone()

        if not row:
            return None

        legs_rows = self.conn.execute(
            """
            SELECT ticker, market_title, target_entry_price, actual_entry_price,
                   contracts, status, entry_order_id, tp_order_id, tp_price,
                   current_price, current_sharp_prob, pnl_cents, guillotine_triggered, exit_price
            FROM basket_legs WHERE basket_id = ?
            """,
            [basket_id],
        ).fetchall()

        legs = []
        for l in legs_rows:
            legs.append(
                BasketLeg(
                    ticker=l[0],
                    market_title=l[1],
                    target_entry_price=l[2],
                    actual_entry_price=l[3],
                    contracts=l[4],
                    status=l[5],
                    entry_order_id=l[6],
                    tp_order_id=l[7],
                    tp_price=l[8],
                    current_price=l[9],
                    current_sharp_prob=l[10],
                    pnl_cents=l[11],
                    guillotine_triggered=bool(l[12]),
                    exit_price=l[13],
                )
            )

        return Basket(
            basket_id=row[0],
            created_at=row[1],
            status=row[2],
            total_invested_cents=row[3],
            total_realized_pnl_cents=row[4],
            total_unrealized_pnl_cents=row[5],
            legs=legs,
        )

    def get_all_baskets(self) -> list[Basket]:
        rows = self.conn.execute("SELECT basket_id FROM baskets ORDER BY created_at DESC").fetchall()
        baskets = []
        for r in rows:
            b = self.get_basket(r[0])
            if b:
                baskets.append(b)
        return baskets

    def record_order(self, order: Order) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO orders (
                order_id, client_order_id, ticker, action, side, count,
                yes_price, no_price, order_type, post_only, sell_position_floor,
                cancel_on_pause, status, filled_count, maker_fee_cents,
                taker_fee_cents, strategy_pod, basket_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.client_order_id,
                order.ticker,
                order.action.value,
                order.side.value,
                order.count,
                order.yes_price,
                order.no_price,
                order.type.value,
                order.post_only,
                order.sell_position_floor,
                order.cancel_on_pause,
                order.status.value,
                order.filled_count,
                order.maker_fee_paid_cents,
                order.taker_fee_paid_cents,
                order.strategy_pod,
                order.basket_id,
                order.created_at,
            ),
        )

    def record_fill(
        self,
        order: Order,
        fill_price_cents: int,
        is_maker: bool,
        fee_cents: int,
    ) -> None:
        import uuid
        fill_id = f"fill-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """
            INSERT INTO fills (
                fill_id, order_id, ticker, action, side, count,
                fill_price_cents, is_maker, fee_cents, strategy_pod, basket_id, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill_id,
                order.order_id,
                order.ticker,
                order.action.value,
                order.side.value,
                order.count,
                fill_price_cents,
                is_maker,
                fee_cents,
                order.strategy_pod,
                order.basket_id,
                datetime.now(timezone.utc),
            ),
        )

    def record_devigged_odds(self, odds: SharpOdds) -> None:
        self.conn.execute(
            """
            INSERT INTO devigged_odds (
                event_id, sport, team_home, team_away, bookmaker,
                home_devigged_prob, away_devigged_prob, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                odds.event_id,
                odds.sport,
                odds.team_home,
                odds.team_away,
                odds.bookmaker,
                odds.home_devigged_prob,
                odds.away_devigged_prob,
                odds.last_updated,
            ),
        )

    def close(self) -> None:
        self.conn.close()
