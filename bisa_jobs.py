"""Idempotent BISA maintenance jobs intended for a scheduler invocation.

These functions run once and return. They do not start background threads,
sleep, or pretend that a scheduler exists.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

from bisa_security import as_utc, iso, security_connection


def _limit(value: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(maximum, parsed))


def _setting(con, key: str, default: Any) -> Any:
    row = con.execute("SELECT value_json FROM platform_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return default


def expire_pending_orders(*, now: datetime | None = None, limit: int = 100) -> dict[str, Any]:
    """Expire overdue unconfirmed orders and release reservations atomically."""
    current = as_utc(now)
    stamp = iso(current)
    expired: list[str] = []
    released = 0
    with security_connection(immediate=True) as con:
        rows = con.execute(
            """SELECT id,account_id,merchant_id,status
            FROM orders
            WHERE status='pending_store_confirmation'
              AND COALESCE(NULLIF(expires_at,''),NULLIF(response_due_at,'')) IS NOT NULL
              AND COALESCE(NULLIF(expires_at,''),response_due_at)<=?
            ORDER BY COALESCE(NULLIF(expires_at,''),response_due_at),id
            LIMIT ?""",
            (stamp, _limit(limit, 1000)),
        ).fetchall()
        for row in rows:
            changed = con.execute(
                """UPDATE orders SET status='expired',cancellation_reason='merchant_response_timeout',
                version=version+1,updated_at=? WHERE id=? AND status='pending_store_confirmation'""",
                (stamp, row["id"]),
            ).rowcount
            if not changed:
                continue
            released += int(con.execute(
                "UPDATE inventory_reservations SET status='released' WHERE order_id=? AND status='pending'",
                (row["id"],),
            ).rowcount or 0)
            con.execute(
                """INSERT INTO order_events(
                id,order_id,event_type,from_status,to_status,actor_kind,actor_id,detail_json,created_at)
                VALUES(?,?,'merchant_response_expired','pending_store_confirmation','expired','system','bisa_jobs',?,?)""",
                (
                    f"ordevt_{uuid.uuid4().hex}", row["id"],
                    json.dumps({"reason": "merchant_response_timeout"}, separators=(",", ":")), stamp,
                ),
            )
            con.execute(
                """UPDATE notifications SET acted_at=?
                WHERE target_kind='merchant' AND target_id=? AND dedupe_key=? AND acted_at=''""",
                (stamp, row["merchant_id"], f"order:{row['id']}:confirm"),
            )
            con.execute(
                """INSERT OR IGNORE INTO notifications(
                id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,
                requires_action,dedupe_key,read_at,acted_at,created_at,priority)
                VALUES(?,?,?,?,?,?,?,?,0,?,'','',?,1)""",
                (
                    f"ntf_{uuid.uuid4().hex}", "account", row["account_id"],
                    "انتهت مهلة تأكيد المتجر", "Store confirmation expired",
                    "لم يؤكد المتجر الطلب في الوقت المحدد وأُعيد المخزون المحجوز.",
                    "The store did not confirm in time and reserved stock was released.",
                    f"shopper:order:{row['id']}", f"order:{row['id']}:expired", stamp,
                ),
            )
            expired.append(row["id"])
    return {"ok": True, "expired": len(expired), "releasedReservations": released, "orderIds": expired}


def mark_stale_inventory(*, now: datetime | None = None, limit: int = 500) -> dict[str, Any]:
    """Mark inventory freshness without destructively hiding or deleting products."""
    current = as_utc(now)
    stamp = iso(current)
    marked = 0
    branches: dict[tuple[str, str], int] = {}
    with security_connection(immediate=True) as con:
        try:
            cadence = int(_setting(con, "inventoryCadenceHours", 24))
        except (TypeError, ValueError):
            cadence = 24
        cadence = max(1, min(24 * 30, cadence))
        enforcement = str(_setting(con, "inventoryEnforcement", "mark_stale") or "mark_stale")
        if enforcement not in {"reminder_only", "mark_stale", "hide_stale", "pause_stale"}:
            enforcement = "mark_stale"
        cutoff = current - timedelta(hours=cadence)
        rows = con.execute(
            """SELECT i.product_id,i.branch_id,i.last_stock_verified_at,
            b.merchant_id FROM product_branch_inventory i
            JOIN products p ON p.id=i.product_id
            JOIN store_branches b ON b.id=i.branch_id
            JOIN merchants m ON m.id=b.merchant_id
            WHERE i.active=1 AND p.active=1 AND p.status='approved'
              AND b.active=1 AND b.status='approved'
              AND m.active=1 AND m.status='approved'
              AND (i.last_stock_verified_at='' OR i.last_stock_verified_at<=?)
              AND (i.freshness_status!='stale' OR i.stale_at='')
            ORDER BY COALESCE(NULLIF(i.last_stock_verified_at,''),'0000'),i.branch_id,i.product_id
            LIMIT ?""",
            (iso(cutoff), _limit(limit, 5000)),
        ).fetchall()
        for row in rows:
            changed = con.execute(
                """UPDATE product_branch_inventory
                SET freshness_status='stale',stale_enforcement=?,
                    stale_at=CASE WHEN stale_at='' THEN ? ELSE stale_at END,updated_at=?
                WHERE product_id=? AND branch_id=? AND active=1
                  AND (freshness_status!='stale' OR stale_at='')""",
                (enforcement, stamp, stamp, row["product_id"], row["branch_id"]),
            ).rowcount
            if changed:
                marked += 1
                key = (row["merchant_id"], row["branch_id"])
                branches[key] = branches.get(key, 0) + 1
        for (merchant_id, branch_id), count in branches.items():
            existing_due = con.execute(
                """SELECT id FROM inventory_audits
                WHERE branch_id=? AND status='due' AND confirmed_at='' LIMIT 1""",
                (branch_id,),
            ).fetchone()
            if not existing_due:
                con.execute(
                    """INSERT INTO inventory_audits(
                    id,merchant_id,branch_id,status,due_at,confirmed_at,confirmed_by,summary,created_at)
                    VALUES(?,?,?,'due',?,'','',?,?)""",
                    (
                        f"iaudit_{uuid.uuid4().hex}", merchant_id, branch_id, stamp,
                        json.dumps({"staleCount": count, "cadenceHours": cadence}, separators=(",", ":")), stamp,
                    ),
                )
            dedupe = f"inventory:{branch_id}:stale:{current.date().isoformat()}"
            con.execute(
                """INSERT OR IGNORE INTO notifications(
                id,target_kind,target_id,title_ar,title_en,body_ar,body_en,route,
                requires_action,dedupe_key,read_at,acted_at,created_at,priority)
                VALUES(?,?,?,?,?,?,?,?,1,?,'','',?,5)""",
                (
                    f"ntf_{uuid.uuid4().hex}", "merchant", merchant_id,
                    "تأكيد المخزون مطلوب", "Inventory verification required",
                    f"هناك {count} منتجات تحتاج مراجعة توفرها.",
                    f"{count} products need an availability check.",
                    f"merchant:inventory:{branch_id}", dedupe, stamp,
                ),
            )
    return {
        "ok": True, "markedStale": marked, "branches": len(branches),
        "enforcement": enforcement, "cutoff": iso(cutoff),
        "destructiveVisibilityChange": False,
    }


def run_operations_once(*, now: datetime | None = None) -> dict[str, Any]:
    """Run the safe operational set once; suitable for cron or a platform job."""
    current = as_utc(now)
    return {
        "orders": expire_pending_orders(now=current),
        "inventory": mark_stale_inventory(now=current),
        "ranAt": iso(current),
    }


def main() -> int:
    """Scheduler entry point: one bounded, observable, retry-safe run."""
    try:
        print(json.dumps(run_operations_once(), ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        # Do not serialize request/customer data.  The exception type is enough
        # for platform monitoring while preserving a non-zero scheduler exit.
        print(json.dumps({"ok":False,"error":"operations_job_failed","type":type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
