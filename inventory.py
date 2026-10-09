import sqlite3
from datetime import datetime

from order_fulfillment import INVENTORY_DEMAND_ACTIVE_STATUSES


def _available_quantity(system_quantity, reserved_quantity):
    """Return the authoritative available quantity from physical balances."""
    return max(int(system_quantity or 0) - int(reserved_quantity or 0), 0)


def _inventory_status(system_quantity, reserved_quantity):
    """Return the warehouse stock status from system and reserved balances."""
    available = _available_quantity(system_quantity, reserved_quantity)
    if available > 0:
        return "Available"
    if int(reserved_quantity or 0) > 0:
        return "Reserved"
    return "Shortage"

def _ensure_inventory_id(cursor, sku, warehouse_id, inventory_id=None):
    """Return a non-null inventory_id for a SKU/warehouse row.

    Correct schemas generate inventory_id automatically. Some databases created
    by older CSV-import code contain inventory rows whose inventory_id is NULL.
    Repair that row transactionally before writing a ledger transaction. The
    repair uses the row's SQLite rowid when available and a unique positive ID
    otherwise; it never inserts a transaction with a NULL inventory_id.
    """
    if inventory_id is not None:
        return inventory_id

    row = cursor.execute(
        "SELECT rowid, inventory_id FROM inventory WHERE sku = ? AND warehouse_id = ?",
        (sku, warehouse_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Inventory row for SKU {sku} in {warehouse_id} no longer exists.")

    rowid, existing_id = row
    if existing_id is not None:
        return existing_id

    candidate = int(rowid)
    collision = cursor.execute(
        "SELECT 1 FROM inventory WHERE inventory_id = ? AND rowid <> ? LIMIT 1",
        (candidate, rowid),
    ).fetchone()
    if collision:
        candidate = int(cursor.execute(
            "SELECT COALESCE(MAX(inventory_id), 0) + 1 FROM inventory"
        ).fetchone()[0])

    cursor.execute(
        "UPDATE inventory SET inventory_id = ? WHERE rowid = ? AND inventory_id IS NULL",
        (candidate, rowid),
    )
    refreshed = cursor.execute(
        "SELECT inventory_id FROM inventory WHERE rowid = ?",
        (rowid,),
    ).fetchone()
    if refreshed is None or refreshed[0] is None:
        raise RuntimeError(
            f"Could not repair missing inventory_id for SKU {sku} in {warehouse_id}. "
            "Check the inventory table schema and migration state."
        )
    return refreshed[0]



# ==================================================
# RECEIVE STOCK
# ==================================================

def receive_stock(
    sku,
    warehouse_id,
    quantity,
    reference_id=None,
    notes=None
):
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:

        if quantity <= 0:
            raise ValueError(
                "Quantity must be greater than zero."
            )

        product = cursor.execute("""
            SELECT sku
            FROM products
            WHERE sku = ?
        """, (sku,)).fetchone()

        if product is None:
            raise ValueError(
                f"SKU {sku} does not exist."
            )

        warehouse = cursor.execute("""
            SELECT warehouse_id
            FROM warehouses
            WHERE warehouse_id = ?
              AND active = 1
        """, (warehouse_id,)).fetchone()

        if warehouse is None:
            raise ValueError(
                f"Warehouse {warehouse_id} does not exist "
                f"or is inactive."
            )

        inventory = cursor.execute("""
            SELECT
                inventory_id,
                system_quantity,
                reserved_quantity
            FROM inventory
            WHERE sku = ?
              AND warehouse_id = ?
        """, (
            sku,
            warehouse_id
        )).fetchone()

        if inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} "
                f"in warehouse {warehouse_id}."
            )

        inventory_id = _ensure_inventory_id(cursor, sku, warehouse_id, inventory[0])
        system_quantity = int(inventory[1])
        reserved_quantity = int(inventory[2])

        new_system_quantity = system_quantity + quantity
        new_available_quantity = _available_quantity(
            new_system_quantity, reserved_quantity
        )
        new_inventory_status = _inventory_status(
            new_system_quantity, reserved_quantity
        )

        cursor.execute("""
            UPDATE inventory
            SET
                system_quantity = ?,
                available_quantity = ?,
                inventory_status = ?
            WHERE inventory_id = ?
        """, (
            new_system_quantity,
            new_available_quantity,
            new_inventory_status,
            inventory_id
        ))

        transaction_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO inventory_transactions (
                inventory_id,
                sku,
                warehouse_id,
                transaction_type,
                quantity,
                reference_id,
                notes,
                transaction_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            inventory_id,
            sku,
            warehouse_id,
            "RECEIVE",
            quantity,
            reference_id,
            notes,
            transaction_time
        ))

        connection.commit()

        return {
            "success": True,
            "sku": sku,
            "warehouse_id": warehouse_id,
            "quantity_received": quantity,
            "new_system_quantity": new_system_quantity,
            "new_available_quantity": new_available_quantity
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# STOCK COUNT
# ==================================================

def stock_count(
    sku,
    warehouse_id,
    physical_quantity,
    reference_id=None,
    notes=None
):
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:

        if physical_quantity < 0:
            raise ValueError(
                "Physical quantity cannot be negative."
            )

        product = cursor.execute("""
            SELECT sku
            FROM products
            WHERE sku = ?
        """, (sku,)).fetchone()

        if product is None:
            raise ValueError(
                f"SKU {sku} does not exist."
            )

        warehouse = cursor.execute("""
            SELECT warehouse_id
            FROM warehouses
            WHERE warehouse_id = ?
              AND active = 1
        """, (warehouse_id,)).fetchone()

        if warehouse is None:
            raise ValueError(
                f"Warehouse {warehouse_id} does not exist "
                f"or is inactive."
            )

        inventory = cursor.execute("""
            SELECT
                inventory_id,
                system_quantity,
                reserved_quantity,
                available_quantity
            FROM inventory
            WHERE sku = ?
              AND warehouse_id = ?
        """, (
            sku,
            warehouse_id
        )).fetchone()

        if inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} "
                f"in warehouse {warehouse_id}."
            )

        inventory_id = _ensure_inventory_id(
            cursor, sku, warehouse_id, inventory[0]
        )
        system_quantity = inventory[1]
        reserved_quantity = inventory[2]

        difference = (
            physical_quantity - system_quantity
        )

        if physical_quantity < reserved_quantity:
            raise ValueError(
                f"Physical quantity ({physical_quantity}) "
                f"cannot be less than reserved quantity "
                f"({reserved_quantity})."
            )

        new_system_quantity = physical_quantity

        new_available_quantity = (
            physical_quantity - reserved_quantity
        )

        verification_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            UPDATE inventory
            SET
                system_quantity = ?,
                available_quantity = ?,
                last_verified_at = ?,
                inventory_status = ?
            WHERE inventory_id = ?
        """, (
            new_system_quantity,
            new_available_quantity,
            verification_time,
            _inventory_status(new_system_quantity, reserved_quantity),
            inventory_id
        ))

        cursor.execute("""
            INSERT INTO inventory_transactions (
                inventory_id,
                sku,
                warehouse_id,
                transaction_type,
                quantity,
                reference_id,
                notes,
                transaction_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            inventory_id,
            sku,
            warehouse_id,
            "ADJUSTMENT",
            difference,
            reference_id,
            notes,
            verification_time
        ))

        connection.commit()

        return {
            "success": True,
            "sku": sku,
            "warehouse_id": warehouse_id,
            "previous_system_quantity": system_quantity,
            "physical_quantity": physical_quantity,
            "difference": difference,
            "reserved_quantity": reserved_quantity,
            "new_available_quantity": new_available_quantity
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# TRANSFER STOCK
# ==================================================

def transfer_stock(
    sku,
    from_warehouse_id,
    to_warehouse_id,
    quantity,
    reference_order_id=None,
    notes=None
):
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:

        if quantity <= 0:
            raise ValueError(
                "Transfer quantity must be greater than zero."
            )

        if from_warehouse_id == to_warehouse_id:
            raise ValueError(
                "Source and destination warehouses "
                "must be different."
            )

        product = cursor.execute("""
            SELECT sku
            FROM products
            WHERE sku = ?
        """, (sku,)).fetchone()

        if product is None:
            raise ValueError(
                f"SKU {sku} does not exist."
            )

        source_warehouse = cursor.execute("""
            SELECT warehouse_id
            FROM warehouses
            WHERE warehouse_id = ?
              AND active = 1
        """, (from_warehouse_id,)).fetchone()

        if source_warehouse is None:
            raise ValueError(
                f"Source warehouse {from_warehouse_id} "
                f"does not exist or is inactive."
            )

        destination_warehouse = cursor.execute("""
            SELECT warehouse_id
            FROM warehouses
            WHERE warehouse_id = ?
              AND active = 1
        """, (to_warehouse_id,)).fetchone()

        if destination_warehouse is None:
            raise ValueError(
                f"Destination warehouse {to_warehouse_id} "
                f"does not exist or is inactive."
            )

        source_inventory = cursor.execute("""
            SELECT
                inventory_id,
                system_quantity,
                reserved_quantity
            FROM inventory
            WHERE sku = ?
              AND warehouse_id = ?
        """, (
            sku,
            from_warehouse_id
        )).fetchone()

        if source_inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} "
                f"in source warehouse {from_warehouse_id}."
            )

        source_inventory_id = _ensure_inventory_id(
            cursor, sku, from_warehouse_id, source_inventory[0]
        )
        source_system_quantity = int(source_inventory[1])
        source_reserved_quantity = int(source_inventory[2])
        source_available_quantity = _available_quantity(
            source_system_quantity, source_reserved_quantity
        )

        if quantity > source_available_quantity:
            raise ValueError(
                f"Cannot transfer {quantity} units. "
                f"Only {source_available_quantity} units "
                f"are available in {from_warehouse_id}."
            )

        destination_inventory = cursor.execute("""
            SELECT
                inventory_id,
                system_quantity,
                reserved_quantity
            FROM inventory
            WHERE sku = ?
              AND warehouse_id = ?
        """, (
            sku,
            to_warehouse_id
        )).fetchone()

        if destination_inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} "
                f"in destination warehouse "
                f"{to_warehouse_id}."
            )

        destination_inventory_id = _ensure_inventory_id(
            cursor, sku, to_warehouse_id, destination_inventory[0]
        )

        destination_system_quantity = int(destination_inventory[1])
        destination_reserved_quantity = int(destination_inventory[2])
        destination_available_quantity = _available_quantity(
            destination_system_quantity, destination_reserved_quantity
        )

        new_source_system_quantity = (
            source_system_quantity - quantity
        )

        new_source_available_quantity = _available_quantity(
            new_source_system_quantity, source_reserved_quantity
        )

        new_destination_system_quantity = destination_system_quantity + quantity
        new_destination_available_quantity = _available_quantity(
            new_destination_system_quantity, destination_reserved_quantity
        )
        new_source_status = _inventory_status(
            new_source_system_quantity, source_reserved_quantity
        )
        new_destination_status = _inventory_status(
            new_destination_system_quantity, destination_reserved_quantity
        )

        cursor.execute("""
            UPDATE inventory
            SET
                system_quantity = ?,
                available_quantity = ?,
                inventory_status = ?
            WHERE inventory_id = ?
        """, (
            new_source_system_quantity,
            new_source_available_quantity,
            new_source_status,
            source_inventory_id
        ))

        cursor.execute("""
            UPDATE inventory
            SET
                system_quantity = ?,
                available_quantity = ?,
                inventory_status = ?
            WHERE inventory_id = ?
        """, (
            new_destination_system_quantity,
            new_destination_available_quantity,
            new_destination_status,
            destination_inventory_id
        ))

        transfer_time = datetime.now().isoformat(
            timespec="seconds"
        )

        cursor.execute("""
            INSERT INTO stock_transfers (
                sku,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                transfer_status,
                reference_order_id,
                requested_at,
                completed_at,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sku,
            from_warehouse_id,
            to_warehouse_id,
            quantity,
            "COMPLETED",
            reference_order_id,
            transfer_time,
            transfer_time,
            notes
        ))

        transfer_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO inventory_transactions (
                inventory_id,
                sku,
                warehouse_id,
                transaction_type,
                quantity,
                reference_id,
                notes,
                transaction_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source_inventory_id,
            sku,
            from_warehouse_id,
            "TRANSFER_OUT",
            -quantity,
            f"TRANSFER-{transfer_id}",
            notes,
            transfer_time
        ))

        cursor.execute("""
            INSERT INTO inventory_transactions (
                inventory_id,
                sku,
                warehouse_id,
                transaction_type,
                quantity,
                reference_id,
                notes,
                transaction_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            destination_inventory_id,
            sku,
            to_warehouse_id,
            "TRANSFER_IN",
            quantity,
            f"TRANSFER-{transfer_id}",
            notes,
            transfer_time
        ))

        connection.commit()

        return {
            "success": True,
            "transfer_id": transfer_id,
            "sku": sku,
            "from_warehouse": from_warehouse_id,
            "to_warehouse": to_warehouse_id,
            "quantity": quantity,
            "status": "COMPLETED"
        }

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()


# ==================================================
# STOCK TRANSFER REQUEST / COMPLETION
# ==================================================

def _validate_transfer_request_inputs(cursor, sku, from_warehouse_id, to_warehouse_id, quantity):
    """Validate common transfer-request inputs. Read-only helper."""
    if int(quantity) <= 0:
        raise ValueError("Transfer quantity must be greater than zero.")

    if from_warehouse_id == to_warehouse_id:
        raise ValueError("Source and destination warehouses must be different.")

    product = cursor.execute(
        "SELECT sku FROM products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if product is None:
        raise ValueError(f"SKU {sku} does not exist.")

    for warehouse_id, label in (
        (from_warehouse_id, "Source"),
        (to_warehouse_id, "Destination"),
    ):
        warehouse = cursor.execute(
            """
            SELECT warehouse_id
            FROM warehouses
            WHERE warehouse_id = ?
              AND active = 1
            """,
            (warehouse_id,),
        ).fetchone()
        if warehouse is None:
            raise ValueError(
                f"{label} warehouse {warehouse_id} does not exist or is inactive."
            )


def request_stock_transfer(
    sku,
    from_warehouse_id,
    to_warehouse_id,
    quantity,
    reference_order_id=None,
    notes=None,
):
    """Create a pending physical transfer request without moving inventory.

    This represents the operational handoff to a warehouse employee. The
    inventory quantities change only when complete_stock_transfer() is called.
    """
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:
        quantity = int(quantity)
        _validate_transfer_request_inputs(
            cursor,
            sku,
            from_warehouse_id,
            to_warehouse_id,
            quantity,
        )

        source_inventory = cursor.execute(
            """
            SELECT inventory_id, system_quantity, reserved_quantity
            FROM inventory
            WHERE sku = ? AND warehouse_id = ?
            """,
            (sku, from_warehouse_id),
        ).fetchone()
        if source_inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} in source warehouse {from_warehouse_id}."
            )

        source_available = _available_quantity(
            source_inventory[1],
            source_inventory[2],
        )

        active_requested = cursor.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM stock_transfers
            WHERE sku = ?
              AND from_warehouse_id = ?
              AND transfer_status IN ('REQUESTED', 'IN_TRANSIT')
            """,
            (sku, from_warehouse_id),
        ).fetchone()[0] or 0
        remaining_source_available = max(
            source_available - int(active_requested),
            0,
        )

        if quantity > remaining_source_available:
            raise ValueError(
                f"Cannot request {quantity} units. {from_warehouse_id} has only "
                f"{remaining_source_available} unit(s) available for new transfer "
                "requests after existing pending transfers."
            )

        requested_at = datetime.now().isoformat(timespec="seconds")

        cursor.execute(
            """
            INSERT INTO stock_transfers (
                sku,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                transfer_status,
                reference_order_id,
                requested_at,
                completed_at,
                notes
            )
            VALUES (?, ?, ?, ?, 'REQUESTED', ?, ?, NULL, ?)
            """,
            (
                sku,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                reference_order_id,
                requested_at,
                notes,
            ),
        )
        transfer_id = cursor.lastrowid
        connection.commit()

        return {
            "success": True,
            "transfer_id": transfer_id,
            "sku": sku,
            "from_warehouse": from_warehouse_id,
            "to_warehouse": to_warehouse_id,
            "quantity": quantity,
            "status": "REQUESTED",
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def request_order_transfers(order_id, transfers, notes=None):
    """Create all transfer requests for one order atomically.

    ``transfers`` is a list of dictionaries containing:
      sku, from_warehouse_id, to_warehouse_id, quantity

    No inventory quantities are changed. Either every request is created or
    the entire request batch is rolled back.
    """
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:
        order_id = str(order_id)
        if cursor.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone() is None:
            raise ValueError(f"Order {order_id} does not exist.")

        aggregated = []
        for item in transfers:
            sku = str(item["sku"])
            from_warehouse_id = str(item["from_warehouse_id"])
            to_warehouse_id = str(item["to_warehouse_id"])
            quantity = int(item["quantity"])
            if quantity <= 0:
                continue
            aggregated.append((sku, from_warehouse_id, to_warehouse_id, quantity))

        if not aggregated:
            raise ValueError(f"No positive transfer quantities were supplied for order {order_id}.")

        requested_at = datetime.now().isoformat(timespec="seconds")
        created = []

        for sku, from_warehouse_id, to_warehouse_id, quantity in aggregated:
            _validate_transfer_request_inputs(
                cursor,
                sku,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
            )

            source_inventory = cursor.execute(
                """
                SELECT inventory_id, system_quantity, reserved_quantity
                FROM inventory
                WHERE sku = ? AND warehouse_id = ?
                """,
                (sku, from_warehouse_id),
            ).fetchone()
            if source_inventory is None:
                raise ValueError(
                    f"No inventory record found for SKU {sku} in source warehouse {from_warehouse_id}."
                )

            source_available = _available_quantity(
                source_inventory[1], source_inventory[2]
            )
            active_requested = cursor.execute(
                """
                SELECT COALESCE(SUM(quantity), 0)
                FROM stock_transfers
                WHERE sku = ?
                  AND from_warehouse_id = ?
                  AND transfer_status IN ('REQUESTED', 'IN_TRANSIT')
                """,
                (sku, from_warehouse_id),
            ).fetchone()[0] or 0

            # Also include requests already created in this same atomic batch.
            batch_requested = sum(
                q for s, f, _, q in aggregated
                if s == sku and f == from_warehouse_id
            )
            # The current item is included in batch_requested; compare the
            # cumulative amount against source availability once below.
            if int(active_requested) + int(batch_requested) > int(source_available):
                raise ValueError(
                    f"Cannot request the transfer batch for {sku} from {from_warehouse_id}. "
                    f"Only {max(int(source_available) - int(active_requested), 0)} unit(s) are available "
                    "after existing pending transfers."
                )

            cursor.execute(
                """
                INSERT INTO stock_transfers (
                    sku, from_warehouse_id, to_warehouse_id, quantity,
                    transfer_status, reference_order_id, requested_at,
                    completed_at, notes
                )
                VALUES (?, ?, ?, ?, 'REQUESTED', ?, ?, NULL, ?)
                """,
                (
                    sku,
                    from_warehouse_id,
                    to_warehouse_id,
                    quantity,
                    order_id,
                    requested_at,
                    notes or f"Transfer requested from Order Flow for {order_id}",
                ),
            )
            created.append({
                "success": True,
                "transfer_id": cursor.lastrowid,
                "sku": sku,
                "from_warehouse": from_warehouse_id,
                "to_warehouse": to_warehouse_id,
                "quantity": quantity,
                "reference_order_id": order_id,
                "status": "REQUESTED",
            })

        connection.commit()
        return {
            "success": True,
            "order_id": order_id,
            "transfers": created,
            "quantity_requested": sum(item["quantity"] for item in created),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_stock_transfer(transfer_id, notes=None):
    """Complete a requested physical transfer and move inventory atomically.

    This is the warehouse confirmation step. Inventory is not moved when a
    transfer is merely requested; it moves only here after the warehouse
    employee confirms the physical movement.
    """
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:
        transfer = cursor.execute(
            """
            SELECT
                transfer_id,
                sku,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                transfer_status,
                reference_order_id,
                notes
            FROM stock_transfers
            WHERE transfer_id = ?
            """,
            (int(transfer_id),),
        ).fetchone()

        if transfer is None:
            raise ValueError(f"Transfer #{transfer_id} does not exist.")

        (
            transfer_id,
            sku,
            from_warehouse_id,
            to_warehouse_id,
            quantity,
            transfer_status,
            reference_order_id,
            existing_notes,
        ) = transfer

        if transfer_status not in ("REQUESTED", "IN_TRANSIT"):
            raise ValueError(
                f"Transfer #{transfer_id} cannot be completed from status '{transfer_status}'."
            )

        source_inventory = cursor.execute(
            """
            SELECT inventory_id, system_quantity, reserved_quantity
            FROM inventory
            WHERE sku = ? AND warehouse_id = ?
            """,
            (sku, from_warehouse_id),
        ).fetchone()
        destination_inventory = cursor.execute(
            """
            SELECT inventory_id, system_quantity, reserved_quantity
            FROM inventory
            WHERE sku = ? AND warehouse_id = ?
            """,
            (sku, to_warehouse_id),
        ).fetchone()

        if source_inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} in source warehouse {from_warehouse_id}."
            )
        if destination_inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} in destination warehouse {to_warehouse_id}."
            )

        source_inventory_id, source_system, source_reserved = source_inventory
        destination_inventory_id, destination_system, destination_reserved = destination_inventory
        source_inventory_id = _ensure_inventory_id(
            cursor, sku, from_warehouse_id, source_inventory_id
        )
        destination_inventory_id = _ensure_inventory_id(
            cursor, sku, to_warehouse_id, destination_inventory_id
        )

        source_available = _available_quantity(source_system, source_reserved)

        # Other pending transfers have already been promised to warehouse
        # operations, even though they have not moved physically yet. Do not
        # allow this completion to consume stock that is committed to those
        # other transfer requests.
        other_pending = cursor.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM stock_transfers
            WHERE sku = ?
              AND from_warehouse_id = ?
              AND transfer_status IN ('REQUESTED', 'IN_TRANSIT')
              AND transfer_id <> ?
            """,
            (sku, from_warehouse_id, transfer_id),
        ).fetchone()[0] or 0

        source_available_for_completion = max(
            source_available - int(other_pending),
            0,
        )

        if int(quantity) > source_available_for_completion:
            raise ValueError(
                f"Cannot complete transfer #{transfer_id}. Only "
                f"{source_available_for_completion} unit(s) are currently available "
                f"for this transfer in {from_warehouse_id} after other pending transfers."
            )

        new_source_system = int(source_system) - int(quantity)
        new_destination_system = int(destination_system) + int(quantity)
        new_source_available = _available_quantity(new_source_system, source_reserved)
        new_destination_available = _available_quantity(
            new_destination_system,
            destination_reserved,
        )

        cursor.execute(
            """
            UPDATE inventory
            SET system_quantity = ?,
                available_quantity = ?,
                inventory_status = ?
            WHERE inventory_id = ?
            """,
            (
                new_source_system,
                new_source_available,
                _inventory_status(new_source_system, source_reserved),
                source_inventory_id,
            ),
        )

        cursor.execute(
            """
            UPDATE inventory
            SET system_quantity = ?,
                available_quantity = ?,
                inventory_status = ?
            WHERE inventory_id = ?
            """,
            (
                new_destination_system,
                new_destination_available,
                _inventory_status(new_destination_system, destination_reserved),
                destination_inventory_id,
            ),
        )

        completed_at = datetime.now().isoformat(timespec="seconds")
        final_notes = notes if notes else existing_notes

        cursor.execute(
            """
            UPDATE stock_transfers
            SET transfer_status = 'COMPLETED',
                completed_at = ?,
                notes = ?
            WHERE transfer_id = ?
            """,
            (completed_at, final_notes, transfer_id),
        )

        reference_id = f"TRANSFER-{transfer_id}"
        cursor.execute(
            """
            INSERT INTO inventory_transactions (
                inventory_id, sku, warehouse_id, transaction_type,
                quantity, reference_id, notes, transaction_time
            )
            VALUES (?, ?, ?, 'TRANSFER_OUT', ?, ?, ?, ?)
            """,
            (
                source_inventory_id,
                sku,
                from_warehouse_id,
                -int(quantity),
                reference_id,
                final_notes,
                completed_at,
            ),
        )
        cursor.execute(
            """
            INSERT INTO inventory_transactions (
                inventory_id, sku, warehouse_id, transaction_type,
                quantity, reference_id, notes, transaction_time
            )
            VALUES (?, ?, ?, 'TRANSFER_IN', ?, ?, ?, ?)
            """,
            (
                destination_inventory_id,
                sku,
                to_warehouse_id,
                int(quantity),
                reference_id,
                final_notes,
                completed_at,
            ),
        )

        connection.commit()

        return {
            "success": True,
            "transfer_id": transfer_id,
            "sku": sku,
            "from_warehouse": from_warehouse_id,
            "to_warehouse": to_warehouse_id,
            "quantity": int(quantity),
            "reference_order_id": reference_order_id,
            "status": "COMPLETED",
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ==================================================
# FULFILLMENT VALIDATION HELPERS
# ==================================================

INVENTORY_DEMAND_ACTIVE_STATUSES = (
    "Pending",
    "Confirmed",
    "Ready to Pick",
    "Picking",
    "Reserved",
)


def _validate_fulfillment_warehouse(warehouse_id):
    """Reserve/Pick are customer-fulfillment actions and therefore use WH01."""
    if str(warehouse_id) != "WH01":
        raise ValueError(
            "Customer fulfillment Reserve Stock and Pick Stock must be performed "
            "from WH01 (Main Fulfillment Warehouse)."
        )


def _validate_order_for_inventory_action(cursor, order_id):
    order = cursor.execute(
        "SELECT order_id, order_status FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError(f"Order {order_id} does not exist.")
    if order[1] not in INVENTORY_DEMAND_ACTIVE_STATUSES:
        raise ValueError(
            f"Order {order_id} cannot be changed by this inventory action "
            f"from status '{order[1]}'."
        )
    return order


def _get_order_sku_transaction_balance(cursor, order_id, sku, warehouse_id):
    """Return the transaction-backed reservation and picked balance.

    Transaction quantities are signed:
      RESERVE = positive
      PICK    = negative
      RELEASE = negative

    Therefore the reservation balance is simply the signed sum of RESERVE,
    PICK and RELEASE transactions. Picked quantity is derived separately from
    negative PICK quantities.
    """
    row = cursor.execute(
        """
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN transaction_type = 'RESERVE' THEN quantity
                    WHEN transaction_type IN ('PICK', 'RELEASE') THEN quantity
                    ELSE 0
                END
            ), 0),
            COALESCE(SUM(
                CASE
                    WHEN transaction_type = 'PICK' THEN -quantity
                    ELSE 0
                END
            ), 0)
        FROM inventory_transactions
        WHERE reference_id = ?
          AND sku = ?
          AND warehouse_id = ?
        """,
        (order_id, sku, warehouse_id),
    ).fetchone()
    return max(int(row[0] or 0), 0), max(int(row[1] or 0), 0)


def _get_order_sku_requirement(cursor, order_id, sku):
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM order_items
        WHERE order_id = ? AND sku = ?
        """,
        (order_id, sku),
    ).fetchone()
    return int(row[0] or 0)


def _reserve_stock_on_cursor(cursor, sku, warehouse_id, quantity, order_id, notes=None):
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Reservation quantity must be greater than zero.")

    _validate_fulfillment_warehouse(warehouse_id)
    _validate_order_for_inventory_action(cursor, order_id)

    if cursor.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
        raise ValueError(f"SKU {sku} does not exist.")

    required_quantity = _get_order_sku_requirement(cursor, order_id, sku)
    if required_quantity <= 0:
        raise ValueError(f"SKU {sku} is not part of order {order_id}.")

    current_reserved_for_order, picked_for_order = _get_order_sku_transaction_balance(
        cursor, order_id, sku, warehouse_id
    )
    remaining_to_reserve = max(
        required_quantity - picked_for_order - current_reserved_for_order,
        0,
    )
    if quantity > remaining_to_reserve:
        raise ValueError(
            f"Cannot reserve {quantity} units for order {order_id}. "
            f"Only {remaining_to_reserve} additional unit(s) are required for SKU {sku}."
        )

    inventory = cursor.execute(
        """
        SELECT inventory_id, system_quantity, reserved_quantity
        FROM inventory
        WHERE sku = ? AND warehouse_id = ?
        """,
        (sku, warehouse_id),
    ).fetchone()
    if inventory is None:
        raise ValueError(
            f"No inventory record found for SKU {sku} in warehouse {warehouse_id}."
        )

    inventory_id, system_quantity, reserved_quantity = inventory
    inventory_id = _ensure_inventory_id(cursor, sku, warehouse_id, inventory_id)
    system_quantity = int(system_quantity)
    reserved_quantity = int(reserved_quantity)
    available_quantity = _available_quantity(system_quantity, reserved_quantity)
    if quantity > available_quantity:
        raise ValueError(
            f"Cannot reserve {quantity} units. Only {available_quantity} units are currently available."
        )

    new_reserved_quantity = reserved_quantity + quantity
    new_available_quantity = _available_quantity(system_quantity, new_reserved_quantity)
    new_inventory_status = _inventory_status(system_quantity, new_reserved_quantity)

    cursor.execute(
        """
        UPDATE inventory
        SET reserved_quantity = ?, available_quantity = ?, inventory_status = ?
        WHERE inventory_id = ?
        """,
        (new_reserved_quantity, new_available_quantity, new_inventory_status, inventory_id),
    )

    transaction_time = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO inventory_transactions (
            inventory_id, sku, warehouse_id, transaction_type, quantity,
            reference_id, notes, transaction_time
        ) VALUES (?, ?, ?, 'RESERVE', ?, ?, ?, ?)
        """,
        (inventory_id, sku, warehouse_id, quantity, order_id, notes, transaction_time),
    )

    return {
        "success": True,
        "sku": sku,
        "warehouse_id": warehouse_id,
        "order_id": order_id,
        "quantity_reserved": quantity,
        "new_reserved_quantity": new_reserved_quantity,
        "new_available_quantity": new_available_quantity,
    }


def _pick_stock_on_cursor(cursor, sku, warehouse_id, quantity, order_id, notes=None):
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Pick quantity must be greater than zero.")

    _validate_fulfillment_warehouse(warehouse_id)
    _validate_order_for_inventory_action(cursor, order_id)

    if cursor.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
        raise ValueError(f"SKU {sku} does not exist.")

    required_quantity = _get_order_sku_requirement(cursor, order_id, sku)
    if required_quantity <= 0:
        raise ValueError(f"SKU {sku} is not part of order {order_id}.")

    order_reserved_quantity, picked_for_order = _get_order_sku_transaction_balance(
        cursor, order_id, sku, warehouse_id
    )
    remaining_to_pick = max(required_quantity - picked_for_order, 0)

    if quantity > remaining_to_pick:
        raise ValueError(
            f"Cannot pick {quantity} units for order {order_id}. "
            f"Only {remaining_to_pick} unit(s) remain to be picked for SKU {sku}."
        )
    if quantity > order_reserved_quantity:
        raise ValueError(
            f"Cannot pick {quantity} units for order {order_id}. "
            f"Only {order_reserved_quantity} unit(s) are currently reserved for this order."
        )

    inventory = cursor.execute(
        """
        SELECT inventory_id, system_quantity, reserved_quantity
        FROM inventory
        WHERE sku = ? AND warehouse_id = ?
        """,
        (sku, warehouse_id),
    ).fetchone()
    if inventory is None:
        raise ValueError(
            f"No inventory record found for SKU {sku} in warehouse {warehouse_id}."
        )

    inventory_id, system_quantity, reserved_quantity = inventory
    inventory_id = _ensure_inventory_id(cursor, sku, warehouse_id, inventory_id)
    system_quantity = int(system_quantity)
    reserved_quantity = int(reserved_quantity)
    if quantity > reserved_quantity:
        raise ValueError(
            f"Cannot pick {quantity} units. Only {reserved_quantity} units are currently reserved in {warehouse_id}."
        )

    new_system_quantity = system_quantity - quantity
    new_reserved_quantity = reserved_quantity - quantity
    new_available_quantity = _available_quantity(new_system_quantity, new_reserved_quantity)
    new_inventory_status = _inventory_status(new_system_quantity, new_reserved_quantity)

    if min(new_system_quantity, new_reserved_quantity, new_available_quantity) < 0:
        raise ValueError("Pick operation would create an invalid inventory balance.")

    cursor.execute(
        """
        UPDATE inventory
        SET system_quantity = ?, reserved_quantity = ?, available_quantity = ?, inventory_status = ?
        WHERE inventory_id = ?
        """,
        (new_system_quantity, new_reserved_quantity, new_available_quantity, new_inventory_status, inventory_id),
    )

    transaction_time = datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO inventory_transactions (
            inventory_id, sku, warehouse_id, transaction_type, quantity,
            reference_id, notes, transaction_time
        ) VALUES (?, ?, ?, 'PICK', ?, ?, ?, ?)
        """,
        (inventory_id, sku, warehouse_id, -quantity, order_id, notes, transaction_time),
    )

    return {
        "success": True,
        "sku": sku,
        "warehouse_id": warehouse_id,
        "order_id": order_id,
        "quantity_picked": quantity,
        "new_system_quantity": new_system_quantity,
        "new_reserved_quantity": new_reserved_quantity,
        "new_available_quantity": new_available_quantity,
    }


# ==================================================
# RESERVE STOCK
# ==================================================

def reserve_stock(sku, warehouse_id, quantity, order_id, notes=None):
    """Reserve live WH01 stock for one order/SKU."""
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()
    try:
        result = _reserve_stock_on_cursor(
            cursor, sku, warehouse_id, quantity, order_id, notes
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reserve_order_stock(order_id, items, warehouse_id="WH01", notes=None):
    """Reserve all requested order/SKU quantities atomically.

    Either every reservation succeeds or none of them are committed. This is
    the operation used by the Fulfillment page's "Reserve All" action.
    """
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()
    try:
        _validate_order_for_inventory_action(cursor, order_id)
        aggregated = {}
        for item in items:
            sku = str(item["sku"] if isinstance(item, dict) else item[0])
            quantity = int(item["quantity"] if isinstance(item, dict) else item[1])
            if quantity > 0:
                aggregated[sku] = aggregated.get(sku, 0) + quantity

        if not aggregated:
            raise ValueError(f"No positive reservation quantities were supplied for order {order_id}.")

        results = []
        for sku, quantity in aggregated.items():
            results.append(
                _reserve_stock_on_cursor(
                    cursor,
                    sku,
                    warehouse_id,
                    quantity,
                    order_id,
                    notes or f"Fulfillment reservation for order {order_id}",
                )
            )

        connection.commit()
        return {
            "success": True,
            "order_id": str(order_id),
            "results": results,
            "quantity_reserved": sum(int(r["quantity_reserved"]) for r in results),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ==================================================
# PICK STOCK
# ==================================================

def pick_stock(sku, warehouse_id, quantity, order_id, notes=None):
    """Pick physically reserved stock from WH01 for one order/SKU."""
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()
    try:
        result = _pick_stock_on_cursor(
            cursor, sku, warehouse_id, quantity, order_id, notes
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def pick_order_stock(order_id, items, warehouse_id="WH01", notes=None):
    """Pick all requested order/SKU quantities atomically.

    Either every physical pick succeeds or no inventory changes are committed.
    """
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()
    try:
        _validate_order_for_inventory_action(cursor, order_id)
        aggregated = {}
        for item in items:
            sku = str(item["sku"] if isinstance(item, dict) else item[0])
            quantity = int(item["quantity"] if isinstance(item, dict) else item[1])
            if quantity > 0:
                aggregated[sku] = aggregated.get(sku, 0) + quantity

        if not aggregated:
            raise ValueError(f"No positive pick quantities were supplied for order {order_id}.")

        results = []
        for sku, quantity in aggregated.items():
            results.append(
                _pick_stock_on_cursor(
                    cursor,
                    sku,
                    warehouse_id,
                    quantity,
                    order_id,
                    notes or f"Physical pick for order {order_id}",
                )
            )

        connection.commit()
        return {
            "success": True,
            "order_id": str(order_id),
            "results": results,
            "quantity_picked": sum(int(r["quantity_picked"]) for r in results),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def receive_stock_for_supply_request(
    request_id,
    sku,
    warehouse_id,
    quantity,
    reference_id=None,
    notes=None,
):
    """Receive physical stock and update its Supply Request atomically."""
    connection = sqlite3.connect("fulfillment.db")
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    try:
        request_id = int(request_id)
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")

        if cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supply_requests'"
        ).fetchone() is None:
            raise ValueError("Supply Request table does not exist.")

        request = cursor.execute(
            """
            SELECT sku, warehouse_id, quantity_ordered, quantity_received, status
            FROM supply_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if request is None:
            raise ValueError(f"Supply Request #{request_id} was not found.")

        request_sku, request_warehouse, ordered, received, status = request
        if str(request_sku) != str(sku) or str(request_warehouse) != str(warehouse_id):
            raise ValueError("Selected Supply Request does not match the receiving SKU and warehouse.")
        if status not in ("ORDERED", "PARTIALLY RECEIVED"):
            raise ValueError(
                f"Supply Request #{request_id} cannot receive stock from status '{status}'."
            )

        remaining = max(int(ordered) - int(received), 0)
        if quantity > remaining:
            raise ValueError(
                f"Receipt quantity cannot exceed the remaining confirmed incoming quantity ({remaining})."
            )

        inventory = cursor.execute(
            """
            SELECT inventory_id, system_quantity, reserved_quantity
            FROM inventory
            WHERE sku = ? AND warehouse_id = ?
            """,
            (sku, warehouse_id),
        ).fetchone()
        if inventory is None:
            raise ValueError(
                f"No inventory record found for SKU {sku} in warehouse {warehouse_id}."
            )

        inventory_id = _ensure_inventory_id(cursor, sku, warehouse_id, inventory[0])
        system_quantity = int(inventory[1])
        reserved_quantity = int(inventory[2])
        new_system_quantity = system_quantity + quantity
        new_available_quantity = _available_quantity(
            new_system_quantity, reserved_quantity
        )
        new_inventory_status = _inventory_status(
            new_system_quantity, reserved_quantity
        )
        transaction_time = datetime.now().isoformat(timespec="seconds")

        cursor.execute(
            """
            UPDATE inventory
            SET system_quantity = ?, available_quantity = ?, inventory_status = ?
            WHERE inventory_id = ?
            """,
            (new_system_quantity, new_available_quantity, new_inventory_status, inventory_id),
        )

        cursor.execute(
            """
            INSERT INTO inventory_transactions (
                inventory_id, sku, warehouse_id, transaction_type, quantity,
                reference_id, notes, transaction_time
            ) VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, ?)
            """,
            (
                inventory_id,
                sku,
                warehouse_id,
                quantity,
                reference_id or f"SUPPLY-REQUEST-{request_id}",
                notes,
                transaction_time,
            ),
        )

        new_received = int(received) + quantity
        new_status = (
            "RECEIVED"
            if new_received >= int(ordered)
            else "PARTIALLY RECEIVED"
        )

        cursor.execute(
            """
            UPDATE supply_requests
            SET quantity_received = ?, status = ?, updated_at = ?
            WHERE request_id = ?
            """,
            (new_received, new_status, transaction_time, request_id),
        )

        connection.commit()

        return {
            "success": True,
            "request_id": request_id,
            "sku": sku,
            "warehouse_id": warehouse_id,
            "quantity_received": quantity,
            "new_system_quantity": new_system_quantity,
            "new_available_quantity": new_available_quantity,
            "new_quantity_received": new_received,
            "status": new_status,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
