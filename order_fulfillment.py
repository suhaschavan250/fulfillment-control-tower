import sqlite3


DB_PATH = "fulfillment.db"

# Explicit operational states. Using IN rather than NOT IN prevents a future
# or malformed status from accidentally becoming live inventory demand.
INVENTORY_DEMAND_ACTIVE_STATUSES = (
    "Pending",
    "Confirmed",
    "Ready to Pick",
    "Picking",
    "Reserved",
)

FULFILLMENT_ACTIVE_STATUSES = (
    "Pending",
    "Confirmed",
    "Ready to Pick",
    "Picking",
    "Reserved",
    "Picked",
    "Packed",
    "Staged",
)


def get_connection():
    return sqlite3.connect(DB_PATH)


def _order_items(cursor, order_id):
    # Inventory assessment is SKU-level. If a marketplace feed contains the
    # same SKU on multiple lines, combine those lines before assessing stock
    # so demand is neither double-counted nor assessed independently.
    return cursor.execute(
        """
        SELECT sku, MAX(product_name) AS product_name, SUM(quantity) AS quantity
        FROM order_items
        WHERE order_id = ?
        GROUP BY sku
        ORDER BY sku
        """,
        (order_id,),
    ).fetchall()


def _reservation_and_pick_balance(cursor, order_id, sku):
    row = cursor.execute(
        """
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN transaction_type = 'RESERVE' THEN quantity
                    WHEN transaction_type IN ('PICK', 'RELEASE') THEN quantity
                    ELSE 0
                END
            ), 0) AS reserved_balance,
            COALESCE(SUM(
                CASE
                    WHEN transaction_type = 'PICK' THEN -quantity
                    ELSE 0
                END
            ), 0) AS picked_quantity
        FROM inventory_transactions
        WHERE reference_id = ?
          AND sku = ?
          AND warehouse_id = 'WH01'
        """,
        (order_id, sku),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def get_inventory_allocation_snapshot():
    """Build the single read-only live inventory allocation snapshot.

    This is the authoritative allocation engine for order-level transfer and
    shortage decisions. It evaluates every active order/SKU together so the
    same physical stock cannot be counted independently for multiple orders.

    Rules:
    - Active demand is grouped by order + SKU.
    - Existing order-specific reservations are respected first.
    - Any remaining current reserved stock is allocated by the same priority
      order used for demand allocation, so SKU-level and order-level coverage
      cannot disagree merely because reservation history is incomplete.
    - WH01 stock is allocated first because it is the shipping warehouse.
    - Other-warehouse stock is allocated next and is therefore transfer stock.
    - Anything still uncovered is a genuine network shortage.

    Read-only: this function performs SELECTs only.
    """
    conn = get_connection()
    try:
        placeholders = ", ".join("?" for _ in INVENTORY_DEMAND_ACTIVE_STATUSES)

        order_rows = conn.execute(
            f"""
            SELECT
                o.order_id,
                o.priority,
                o.promised_ship_by,
                oi.sku,
                MAX(oi.product_name) AS product_name,
                SUM(oi.quantity) AS requested_quantity
            FROM orders o
            INNER JOIN order_items oi
                ON oi.order_id = o.order_id
            WHERE o.order_status IN ({placeholders})
            GROUP BY
                o.order_id,
                o.priority,
                o.promised_ship_by,
                oi.sku
            ORDER BY
                oi.sku,
                CASE o.priority
                    WHEN 'Critical' THEN 1
                    WHEN 'High' THEN 2
                    ELSE 3
                END,
                o.promised_ship_by,
                o.order_id
            """,
            INVENTORY_DEMAND_ACTIVE_STATUSES,
        ).fetchall()

        inventory_rows = conn.execute(
            """
            SELECT
                sku,
                warehouse_id,
                MAX(COALESCE(system_quantity, 0) - COALESCE(reserved_quantity, 0), 0) AS available_quantity,
                COALESCE(reserved_quantity, 0)
            FROM inventory
            ORDER BY sku, warehouse_id
            """
        ).fetchall()

        reservation_rows = conn.execute(
            """
            SELECT
                reference_id AS order_id,
                sku,
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'RESERVE' THEN quantity
                            WHEN transaction_type IN ('PICK', 'RELEASE') THEN -quantity
                            ELSE 0
                        END
                    ),
                    0
                ) AS reserved_balance,
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'PICK' THEN -quantity
                            ELSE 0
                        END
                    ),
                    0
                ) AS picked_quantity
            FROM inventory_transactions
            WHERE warehouse_id = 'WH01'
              AND reference_id IS NOT NULL
            GROUP BY reference_id, sku
            """
        ).fetchall()
    finally:
        conn.close()

    orders_by_sku = {}
    order_rows_by_id = {}
    for order_id, priority, promised_ship_by, sku, product_name, requested_quantity in order_rows:
        sku = str(sku)
        order_id = str(order_id)
        row = {
            "order_id": order_id,
            "priority": priority or "Normal",
            "promised_ship_by": promised_ship_by,
            "sku": sku,
            "product_name": product_name or "",
            "requested_quantity": int(requested_quantity or 0),
        }
        orders_by_sku.setdefault(sku, []).append(row)
        order_rows_by_id.setdefault(order_id, []).append(row)

    inventory_by_sku = {}
    for sku, warehouse_id, available_quantity, reserved_quantity in inventory_rows:
        sku = str(sku)
        inventory_by_sku.setdefault(sku, []).append(
            {
                "warehouse_id": str(warehouse_id),
                "available_quantity": int(available_quantity or 0),
                "reserved_quantity": int(reserved_quantity or 0),
            }
        )

    reservation_map = {}
    picked_map = {}
    for order_id, sku, reserved_balance, picked_quantity in reservation_rows:
        key = (str(order_id), str(sku))
        reservation_map[key] = max(int(reserved_balance or 0), 0)
        picked_map[key] = max(int(picked_quantity or 0), 0)

    order_item_allocations = {}
    order_summary = {}
    sku_summary = {}

    all_skus = sorted(set(orders_by_sku) | set(inventory_by_sku))

    for sku in all_skus:
        sku_orders = orders_by_sku.get(sku, [])
        sku_inventory = inventory_by_sku.get(sku, [])

        wh01_available = sum(
            row["available_quantity"]
            for row in sku_inventory
            if row["warehouse_id"] == "WH01"
        )
        other_available = sum(
            row["available_quantity"]
            for row in sku_inventory
            if row["warehouse_id"] != "WH01"
        )
        network_available = wh01_available + other_available
        current_reserved = sum(
            row["reserved_quantity"]
            for row in sku_inventory
        )

        wh01_remaining = wh01_available
        other_remaining = other_available

        # Only transaction-backed reservations belong to an order.
        # inventory.reserved_quantity is a warehouse-level balance and must
        # never be silently assigned to another order. This keeps Order Flow
        # and pick_order_stock() on the same source of truth.
        effective_reserved = {}
        for order in sku_orders:
            key = (order["order_id"], sku)
            known_reserved = max(reservation_map.get(key, 0), 0)
            max_outstanding = max(
                order["requested_quantity"] - picked_map.get(key, 0),
                0,
            )
            effective_reserved[key] = min(known_reserved, max_outstanding)

        total_remaining_requirement = 0
        total_effective_reserved = 0
        total_wh01_allocated = 0
        total_other_allocated = 0
        total_shortage = 0
        transfer_order_ids = []
        shortage_order_ids = []
        affected_order_ids = []

        for order in sku_orders:
            order_id = order["order_id"]
            key = (order_id, sku)
            requested_quantity = order["requested_quantity"]
            picked_quantity = picked_map.get(key, 0)
            reserved_quantity = min(
                effective_reserved.get(key, 0),
                max(requested_quantity - picked_quantity, 0),
            )
            remaining_requirement = max(
                requested_quantity - picked_quantity,
                0,
            )
            unreserved_requirement = max(
                remaining_requirement - reserved_quantity,
                0,
            )

            wh01_allocated = min(unreserved_requirement, wh01_remaining)
            wh01_remaining -= wh01_allocated

            remaining_after_wh01 = max(
                unreserved_requirement - wh01_allocated,
                0,
            )
            other_allocated = min(remaining_after_wh01, other_remaining)
            other_remaining -= other_allocated

            shortage = max(
                remaining_after_wh01 - other_allocated,
                0,
            )

            if wh01_allocated or other_allocated or shortage or reserved_quantity:
                affected_order_ids.append(order_id)
            if other_allocated > 0:
                transfer_order_ids.append(order_id)
            if shortage > 0:
                shortage_order_ids.append(order_id)

            total_remaining_requirement += remaining_requirement
            total_effective_reserved += reserved_quantity
            total_wh01_allocated += wh01_allocated
            total_other_allocated += other_allocated
            total_shortage += shortage

            status = (
                "SHORTAGE"
                if shortage > 0
                else "TRANSFER_REQUIRED"
                if other_allocated > 0
                else "AVAILABLE"
            )

            allocation = {
                "order_id": order_id,
                "sku": sku,
                "product_name": order["product_name"],
                "priority": order["priority"],
                "promised_ship_by": order["promised_ship_by"],
                "requested_quantity": requested_quantity,
                "picked_quantity": picked_quantity,
                "reserved_quantity": reserved_quantity,
                "remaining_requirement": remaining_requirement,
                "unreserved_requirement": unreserved_requirement,
                "wh01_available_total": wh01_available,
                "other_warehouse_available_total": other_available,
                "network_available_total": network_available,
                "wh01_allocated": wh01_allocated,
                "transfer_required": other_allocated,
                "shortage": shortage,
                "status": status,
            }
            order_item_allocations[(order_id, sku)] = allocation

            summary = order_summary.setdefault(
                order_id,
                {
                    "order_id": order_id,
                    "transfer_required": 0,
                    "shortage": 0,
                    "has_transfer": False,
                    "has_shortage": False,
                },
            )
            summary["transfer_required"] += other_allocated
            summary["shortage"] += shortage
            summary["has_transfer"] = summary["has_transfer"] or other_allocated > 0
            summary["has_shortage"] = summary["has_shortage"] or shortage > 0

        unreserved_demand = max(
            total_remaining_requirement - current_reserved,
            0,
        )
        net_shortage = max(
            unreserved_demand - network_available,
            0,
        )
        destination_gap = max(
            unreserved_demand - wh01_available,
            0,
        )
        transferable_quantity = min(
            destination_gap,
            other_available,
        )

        sku_summary[sku] = {
            "sku": sku,
            "total_open_demand": total_remaining_requirement,
            "reserved_quantity": current_reserved,
            "effective_reserved_quantity": total_effective_reserved,
            "unreserved_demand": unreserved_demand,
            "total_available": network_available,
            "destination_warehouse_id": "WH01",
            "destination_available": wh01_available,
            "destination_gap": destination_gap,
            "other_warehouse_available": {
                row["warehouse_id"]: row["available_quantity"]
                for row in sku_inventory
                if row["warehouse_id"] != "WH01" and row["available_quantity"] > 0
            },
            "transferable_quantity": transferable_quantity,
            "net_shortage": net_shortage,
            "affected_orders": list(dict.fromkeys(affected_order_ids)),
            "affected_order_count": len(set(affected_order_ids)),
            "transfer_orders": list(dict.fromkeys(transfer_order_ids)),
            "shortage_orders": list(dict.fromkeys(shortage_order_ids)),
            "transfer_order_count": len(set(transfer_order_ids)),
            "shortage_order_count": len(set(shortage_order_ids)),
            "order_shortage_total": total_shortage,
            "priority_summary": [
                f"{priority}: {sum(row['requested_quantity'] for row in sku_orders if row['priority'] == priority)}"
                for priority in ("Critical", "High", "Normal")
                if any(row['priority'] == priority for row in sku_orders)
            ],
        }

    return {
        "sku_summary": sku_summary,
        "order_item_allocations": order_item_allocations,
        "order_summary": order_summary,
    }


def _sku_order_allocation(cursor, sku):
    """Return the authoritative live allocation for one SKU.

    Kept as a compatibility helper for existing callers. The actual logic
    lives in get_inventory_allocation_snapshot().
    Read-only.
    """
    snapshot = get_inventory_allocation_snapshot()
    return {
        order_id: allocation
        for (order_id, allocation_sku), allocation
        in snapshot["order_item_allocations"].items()
        if allocation_sku == str(sku)
    }

def check_order_stock(order_id):
    """Return the authoritative live inventory assessment for one order.

    This is read-only. It reads the same batch allocation snapshot used by
    dashboard metrics, Inventory Alerts and action routing, so those surfaces
    cannot independently calculate different shortage/transfer states.
    """
    conn = get_connection()
    try:
        order = conn.execute(
            "SELECT order_id, order_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()

    if order is None:
        raise ValueError(f"Order {order_id} does not exist.")

    snapshot = get_inventory_allocation_snapshot()
    order_id = str(order_id)

    item_rows = [
        allocation
        for (allocation_order_id, _sku), allocation
        in snapshot["order_item_allocations"].items()
        if allocation_order_id == order_id
    ]

    if not item_rows:
        return {
            "order_id": order_id,
            "order_status": order[1],
            "overall_status": "AVAILABLE",
            "items": [],
        }

    item_rows.sort(key=lambda row: str(row["sku"]))

    has_shortage = any(
        int(item["shortage"]) > 0
        for item in item_rows
    )
    has_transfer = any(
        int(item["transfer_required"]) > 0
        for item in item_rows
    )

    details = []
    for item in item_rows:
        details.append(
            {
                "sku": item["sku"],
                "product_name": item["product_name"],
                "required_quantity": int(item["requested_quantity"]),
                "picked_quantity": int(item["picked_quantity"]),
                "reserved_quantity": int(item["reserved_quantity"]),
                "remaining_requirement": int(item["remaining_requirement"]),
                "wh01_available": int(item["wh01_available_total"]),
                "wh01_coverage": int(item["wh01_allocated"]),
                "other_warehouse_available": int(item["other_warehouse_available_total"]),
                "transfer_required": int(item["transfer_required"]),
                "shortage": int(item["shortage"]),
                "status": item["status"],
            }
        )

    overall_status = (
        "SHORTAGE"
        if has_shortage
        else "TRANSFER_REQUIRED"
        if has_transfer
        else "AVAILABLE"
    )

    return {
        "order_id": order_id,
        "order_status": order[1],
        "overall_status": overall_status,
        "items": details,
    }

def get_next_fulfillment_action(order_id):
    """Return the next live operational action for an order. Read-only."""
    conn = get_connection()
    try:
        order = conn.execute(
            "SELECT order_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None:
            raise ValueError(f"Order {order_id} does not exist.")

        status = str(order[0])

        if status == "Picked":
            return "PACK"
        if status == "Packed":
            return "STAGE"
        if status == "Staged":
            return "SHIP"
        if status in ("Shipped", "Delivered", "Completed"):
            return "COMPLETED"

        stock = check_order_stock(order_id)

        # If an order has both transferable stock and a remaining network
        # shortage, transfer the available internal stock first. After the
        # transfer, the live assessment will recalculate the remaining
        # shortage and return SHORTAGE if external supply is still required.
        has_transfer = any(
            int(item.get("transfer_required", 0)) > 0
            for item in stock.get("items", [])
        )
        has_shortage = any(
            int(item.get("shortage", 0)) > 0
            for item in stock.get("items", [])
        )

        # A transfer request is an intermediate operational state. The order
        # must wait for the warehouse employee to confirm physical movement
        # before Reserve Stock becomes the next valid action.
        if has_transfer:
            pending_transfer = False
            for item in stock.get("items", []):
                transfer_required = int(item.get("transfer_required", 0))
                if transfer_required <= 0:
                    continue

                pending = conn.execute(
                    """
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM stock_transfers
                    WHERE reference_order_id = ?
                      AND sku = ?
                      AND to_warehouse_id = 'WH01'
                      AND transfer_status IN ('REQUESTED', 'IN_TRANSIT')
                    """,
                    (order_id, item["sku"]),
                ).fetchone()[0] or 0

                if int(pending) >= transfer_required:
                    pending_transfer = True
                    break

            if pending_transfer:
                return "TRANSFER_PENDING"

            return "TRANSFER"
        if has_shortage:
            return "SHORTAGE"

        # If every outstanding item is already reserved, the next action is pick.
        all_reserved = True
        items = stock["items"]
        for item in items:
            if int(item["remaining_requirement"]) > int(item["reserved_quantity"]):
                all_reserved = False
                break

        if all_reserved and items:
            return "PICK"

        return "RESERVE"
    finally:
        conn.close()
