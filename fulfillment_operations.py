import sqlite3
from datetime import datetime


# ==================================================
# HELPER: VALIDATE ORDER
# ==================================================

def _validate_order(cursor, order_id):
    """
    Validate that an order exists.
    """

    order = cursor.execute("""
        SELECT
            order_id,
            order_status
        FROM orders
        WHERE order_id = ?
    """, (order_id,)).fetchone()

    if order is None:
        raise ValueError(
            f"Order {order_id} does not exist."
        )

    return order


def _validate_physical_pick_completion(cursor, order_id):
    """Validate that every order-item quantity has been physically picked.

    Read-only validation used before changing an order to Picked. The actual
    inventory movement must already exist as PICK transactions in
    inventory_transactions. Duplicate SKU lines are aggregated.
    """

    item_rows = cursor.execute(
        """
        SELECT
            sku,
            SUM(quantity) AS required_quantity
        FROM order_items
        WHERE order_id = ?
        GROUP BY sku
        """,
        (order_id,),
    ).fetchall()

    if not item_rows:
        raise ValueError(
            f"Order {order_id} has no order items and cannot be marked as Picked."
        )

    picked_rows = cursor.execute(
        """
        SELECT
            sku,
            COALESCE(SUM(CASE
                WHEN transaction_type = 'PICK' THEN -quantity
                ELSE 0
            END), 0) AS picked_quantity
        FROM inventory_transactions
        WHERE reference_id = ?
          AND transaction_type = 'PICK'
        GROUP BY sku
        """,
        (order_id,),
    ).fetchall()

    picked_by_sku = {
        str(sku): max(int(picked_quantity or 0), 0)
        for sku, picked_quantity in picked_rows
    }

    incomplete = []

    for sku, required_quantity in item_rows:
        required_quantity = int(required_quantity or 0)
        picked_quantity = picked_by_sku.get(str(sku), 0)

        if picked_quantity < required_quantity:
            incomplete.append(
                f"{sku}: required {required_quantity}, picked {picked_quantity}"
            )

    if incomplete:
        raise ValueError(
            "Order cannot be marked as Picked because physical picking is "
            "incomplete. "
            + "; ".join(incomplete)
        )


# ==================================================
# RECORD FULFILLMENT EVENT
# ==================================================

def record_fulfillment_event(
    order_id,
    event_type,
    event_status=None,
    notes=None
):
    """
    Record an operational event for an order.

    Examples:

    PICKED
    PACKED
    STAGED
    SHIPPED

    This function does not change inventory.
    It records the operational history of the order.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        _validate_order(cursor, order_id)

        event_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO fulfillment_events (
                order_id,
                event_type,
                event_status,
                notes,
                event_time
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id,
            event_type,
            event_status,
            notes,
            event_time
        ))

        event_id = cursor.lastrowid

        connection.commit()

        return {
            "success": True,
            "event_id": event_id,
            "order_id": order_id,
            "event_type": event_type,
            "event_status": event_status,
            "event_time": event_time
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


VALID_STATUS_TRANSITIONS = {
    "Pending": {"Confirmed", "Ready to Pick"},
    "Confirmed": {"Ready to Pick", "Picking", "Reserved"},
    "Ready to Pick": {"Picking", "Reserved"},
    "Picking": {"Reserved", "Picked"},
    "Reserved": {"Picked"},
    "Picked": {"Packed"},
    "Packed": {"Staged"},
    "Staged": {"Shipped"},
    "Shipped": {"Delivered", "Completed"},
    "Delivered": {"Completed"},
    "Completed": set(),
}


# ==================================================
# UPDATE ORDER STATUS
# ==================================================

def update_order_status(
    order_id,
    new_status
):
    """
    Update the current operational status of an order.

    This changes only the order status.
    It does not change inventory.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        order = _validate_order(cursor, order_id)
        current_status = str(order[1])
        new_status = str(new_status)

        if new_status not in VALID_STATUS_TRANSITIONS.get(current_status, set()):
            raise ValueError(
                f"Order {order_id} cannot move from status "
                f"'{current_status}' to '{new_status}'."
            )

        cursor.execute("""
            UPDATE orders
            SET order_status = ?
            WHERE order_id = ?
        """, (
            new_status,
            order_id
        ))

        connection.commit()

        return {
            "success": True,
            "order_id": order_id,
            "new_status": new_status
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# MARK ORDER AS PICKED
# ==================================================

def mark_order_picked(
    order_id,
    notes=None
):
    """
    Mark an order as picked.

    The actual inventory reduction should already have
    been performed using pick_stock() from inventory.py.

    This function records the operational event and
    updates the order status.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        order = _validate_order(
            cursor,
            order_id
        )

        current_status = order[1]

        # --------------------------------------------------
        # Prevent invalid progression
        # --------------------------------------------------

        if current_status not in (
            "Pending",
            "Confirmed",
            "Ready to Pick",
            "Picking",
            "Reserved"
        ):
            raise ValueError(
                f"Order {order_id} cannot be marked as picked "
                f"from status '{current_status}'."
            )

        # The UI confirmation is not sufficient protection. The backend must
        # verify that the physical inventory pick was actually recorded.
        _validate_physical_pick_completion(
            cursor,
            order_id,
        )

        event_time = datetime.now().isoformat(
            timespec="seconds"
        )

        # --------------------------------------------------
        # Record event
        # --------------------------------------------------

        cursor.execute("""
            INSERT INTO fulfillment_events (
                order_id,
                event_type,
                event_status,
                notes,
                event_time
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id,
            "PICKED",
            "COMPLETED",
            notes,
            event_time
        ))

        event_id = cursor.lastrowid

        # --------------------------------------------------
        # Update order status
        # --------------------------------------------------

        cursor.execute("""
            UPDATE orders
            SET order_status = ?
            WHERE order_id = ?
        """, (
            "Picked",
            order_id
        ))

        connection.commit()

        return {
            "success": True,
            "order_id": order_id,
            "event_id": event_id,
            "status": "Picked"
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# MARK ORDER AS PACKED
# ==================================================

def mark_order_packed(
    order_id,
    notes=None
):
    """
    Mark an order as packed.

    Packing should happen only after the order has
    been picked.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        order = _validate_order(
            cursor,
            order_id
        )

        current_status = order[1]

        if current_status != "Picked":

            raise ValueError(
                f"Order {order_id} cannot be packed. "
                f"Current status is '{current_status}'. "
                f"Order must be Picked first."
            )

        event_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO fulfillment_events (
                order_id,
                event_type,
                event_status,
                notes,
                event_time
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id,
            "PACKED",
            "COMPLETED",
            notes,
            event_time
        ))

        event_id = cursor.lastrowid

        cursor.execute("""
            UPDATE orders
            SET order_status = ?
            WHERE order_id = ?
        """, (
            "Packed",
            order_id
        ))

        connection.commit()

        return {
            "success": True,
            "order_id": order_id,
            "event_id": event_id,
            "status": "Packed"
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# MARK ORDER AS STAGED
# ==================================================

def mark_order_staged(
    order_id,
    notes=None
):
    """
    Mark a packed order as staged and ready for
    courier pickup.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        order = _validate_order(
            cursor,
            order_id
        )

        current_status = order[1]

        if current_status != "Packed":

            raise ValueError(
                f"Order {order_id} cannot be staged. "
                f"Current status is '{current_status}'. "
                f"Order must be Packed first."
            )

        event_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO fulfillment_events (
                order_id,
                event_type,
                event_status,
                notes,
                event_time
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id,
            "STAGED",
            "READY_FOR_PICKUP",
            notes,
            event_time
        ))

        event_id = cursor.lastrowid

        cursor.execute("""
            UPDATE orders
            SET order_status = ?
            WHERE order_id = ?
        """, (
            "Staged",
            order_id
        ))

        connection.commit()

        return {
            "success": True,
            "order_id": order_id,
            "event_id": event_id,
            "status": "Staged"
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# MARK ORDER AS SHIPPED
# ==================================================

def mark_order_shipped(
    order_id,
    notes=None
):
    """
    Mark a staged order as shipped.

    This represents the handover of the package to
    the courier.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        order = _validate_order(
            cursor,
            order_id
        )

        current_status = order[1]

        if current_status != "Staged":

            raise ValueError(
                f"Order {order_id} cannot be shipped. "
                f"Current status is '{current_status}'. "
                f"Order must be Staged first."
            )

        event_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO fulfillment_events (
                order_id,
                event_type,
                event_status,
                notes,
                event_time
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id,
            "SHIPPED",
            "HANDED_TO_COURIER",
            notes,
            event_time
        ))

        event_id = cursor.lastrowid

        cursor.execute("""
            UPDATE orders
            SET order_status = ?
            WHERE order_id = ?
        """, (
            "Shipped",
            order_id
        ))

        connection.commit()

        return {
            "success": True,
            "order_id": order_id,
            "event_id": event_id,
            "status": "Shipped"
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# GET ORDER FULFILLMENT HISTORY
# ==================================================

def get_order_fulfillment_history(
    order_id
):
    """
    Retrieve the complete fulfillment event history
    for an order.
    """

    connection = sqlite3.connect("fulfillment.db")
    cursor = connection.cursor()

    try:

        _validate_order(
            cursor,
            order_id
        )

        events = cursor.execute("""
            SELECT
                event_id,
                event_type,
                event_status,
                notes,
                event_time
            FROM fulfillment_events
            WHERE order_id = ?
            ORDER BY event_time ASC,
                     event_id ASC
        """, (order_id,)).fetchall()

        results = []

        for event in events:

            results.append({
                "event_id": event[0],
                "event_type": event[1],
                "event_status": event[2],
                "notes": event[3],
                "event_time": event[4]
            })

        return {
            "success": True,
            "order_id": order_id,
            "events": results
        }

    finally:

        connection.close()