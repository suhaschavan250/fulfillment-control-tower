import os
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

from order_fulfillment import (
    check_order_stock,
    get_inventory_allocation_snapshot,
    get_next_fulfillment_action,
    INVENTORY_DEMAND_ACTIVE_STATUSES,
    FULFILLMENT_ACTIVE_STATUSES,
)

from inventory import (
    receive_stock,
    receive_stock_for_supply_request,
    stock_count,
    transfer_stock,
    request_stock_transfer,
    request_order_transfers,
    complete_stock_transfer,
    reserve_stock,
    pick_stock,
    reserve_order_stock,
    pick_order_stock,
)

from fulfillment_operations import (
    mark_order_picked,
    mark_order_packed,
    mark_order_staged,
    mark_order_shipped,
    get_order_fulfillment_history,
)

from data_import import (
    validate_orders_dataframe,
    validate_order_items_dataframe,
    validate_products_dataframe,
    validate_warehouses_dataframe,
    validate_inventory_reconciliation,
    preview_inventory_reconciliation,
    apply_inventory_reconciliation,
    import_dataframe,
    import_marketplace_order_batch,
    get_database_counts,
)


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Fulfillment Control Tower",
    page_icon="📦",
    layout="wide"
)


# ============================================================
# DATABASE
# ============================================================

DB_PATH = "fulfillment.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def derive_available_quantity(system_quantity, reserved_quantity):
    """Derive live available stock from physical and reserved balances. Read-only."""
    return max(
        int(system_quantity or 0) - int(reserved_quantity or 0),
        0,
    )


def derive_inventory_status(system_quantity, reserved_quantity):
    """Derive warehouse stock status from physical balances. Read-only."""
    available = derive_available_quantity(system_quantity, reserved_quantity)
    if available > 0:
        return "Available"
    if int(reserved_quantity or 0) > 0:
        return "Reserved"
    return "Shortage"


def normalize_inventory_balances(inventory_df):
    """Normalize live inventory quantities/status in memory without writing SQLite."""
    if inventory_df is None or inventory_df.empty:
        return inventory_df.copy() if inventory_df is not None else pd.DataFrame()

    normalized = inventory_df.copy()
    normalized["system_quantity"] = pd.to_numeric(
        normalized["system_quantity"], errors="coerce"
    ).fillna(0).astype(int)
    normalized["reserved_quantity"] = pd.to_numeric(
        normalized["reserved_quantity"], errors="coerce"
    ).fillna(0).astype(int)
    normalized["available_quantity"] = (
        normalized["system_quantity"] - normalized["reserved_quantity"]
    ).clip(lower=0).astype(int)
    normalized["inventory_status"] = [
        derive_inventory_status(system, reserved)
        for system, reserved in zip(
            normalized["system_quantity"],
            normalized["reserved_quantity"],
        )
    ]
    return normalized


# ============================================================
# LIVE DATA DEFINITIONS
# ============================================================

# These status sets are imported from the fulfillment backend so the UI and
# business logic cannot silently drift apart.
def get_active_orders_df(orders_df):
    """Return orders that can still create inventory demand. Read-only."""
    if orders_df is None or orders_df.empty:
        return orders_df.copy() if orders_df is not None else pd.DataFrame()
    return orders_df[
        orders_df["order_status"].isin(INVENTORY_DEMAND_ACTIVE_STATUSES)
    ].copy()


def get_fulfillment_active_orders_df(orders_df):
    """Return orders still active in the fulfillment pipeline. Read-only."""
    if orders_df is None or orders_df.empty:
        return orders_df.copy() if orders_df is not None else pd.DataFrame()
    return orders_df[
        orders_df["order_status"].isin(FULFILLMENT_ACTIVE_STATUSES)
    ].copy()


def get_active_order_status_clause():
    """Return the explicit SQL predicate for inventory-demand orders."""
    placeholders = ", ".join("?" for _ in INVENTORY_DEMAND_ACTIVE_STATUSES)
    return (
        f"order_status IN ({placeholders})",
        list(INVENTORY_DEMAND_ACTIVE_STATUSES),
    )


def get_fulfillment_active_order_status_clause():
    """Return the explicit SQL predicate for fulfillment-active orders."""
    placeholders = ", ".join("?" for _ in FULFILLMENT_ACTIVE_STATUSES)
    return (
        f"order_status IN ({placeholders})",
        list(FULFILLMENT_ACTIVE_STATUSES),
    )


def count_live_inventory_alerts(inventory_alerts, scenario):
    """Count the same unique SKU-level alert rows shown to the operator. Read-only."""
    if inventory_alerts is None or inventory_alerts.empty:
        return 0

    scenario_alerts = inventory_alerts[
        inventory_alerts["scenario"] == scenario
    ].copy()

    if scenario_alerts.empty:
        return 0

    return int(
        scenario_alerts["sku"].astype(str).nunique()
    )


def get_inventory_alert_action_state(sku, warehouse_id, inventory_df=None, allocation_snapshot=None, scenario=None):
    """Return the live next-action state for one Inventory alert. Read-only."""
    demand = get_sku_demand_summary(str(sku), allocation_snapshot=allocation_snapshot)

    if inventory_df is None:
        inventory_df = load_inventory()

    source_available = 0
    source_warehouse_id = None

    if inventory_df is not None and not inventory_df.empty:
        source_rows = inventory_df[
            (inventory_df["sku"].astype(str) == str(sku))
            & (inventory_df["warehouse_id"].astype(str) != str(warehouse_id))
            & (inventory_df["available_quantity"] > 0)
        ].sort_values("available_quantity", ascending=False)

        if not source_rows.empty:
            source_warehouse_id = str(source_rows.iloc[0]["warehouse_id"])
            source_available = int(source_rows.iloc[0]["available_quantity"])

    transfer_quantity = min(
        max(int(demand["destination_gap"]), 0),
        max(int(demand["transferable_quantity"]), 0),
    )

    if transfer_quantity > 0 and int(demand["net_shortage"]) > 0:
        recommended_action = "Transfer First → Supply Planning"
    elif transfer_quantity > 0:
        recommended_action = "Transfer Stock"
    elif int(demand["net_shortage"]) > 0:
        recommended_action = "Supply Planning"
    elif str(scenario) == "Low Stock":
        # Low Stock is a replenishment-planning condition, not a receipt event.
        # Receive Stock is reserved for stock that has physically arrived.
        recommended_action = "Supply Planning"
    elif str(warehouse_id) == "WH01" and int(demand["unreserved_demand"]) > 0:
        recommended_action = "Reserve Stock"
    else:
        recommended_action = "Review"

    return {
        "demand": demand,
        "source_warehouse_id": source_warehouse_id,
        "source_available": source_available,
        "transfer_quantity": transfer_quantity,
        "recommended_action": recommended_action,
    }


def enrich_inventory_alerts_with_demand(alerts, allocation_snapshot=None):
    """Add live SKU demand fields to inventory alert rows. Read-only.

    The alert scenario and the network shortage are intentionally separate
    concepts. A SKU can require a transfer to WH01 and still have a remaining
    network shortage after all transferable stock is considered.
    """
    if alerts is None or alerts.empty:
        return pd.DataFrame() if alerts is None else alerts.copy()

    demand_rows = []
    for sku in alerts["sku"].astype(str).drop_duplicates():
        demand = get_sku_demand_summary(sku, allocation_snapshot=allocation_snapshot)
        demand_rows.append(
            {
                "sku": sku,
                "total_open_demand": demand["total_open_demand"],
                "reserved_quantity": demand["reserved_quantity"],
                "unreserved_demand": demand["unreserved_demand"],
                "total_available": demand["total_available"],
                "net_shortage": demand["net_shortage"],
                "affected_orders": demand["affected_order_count"],
            }
        )

    demand_df = pd.DataFrame(demand_rows)
    return alerts.merge(demand_df, on="sku", how="left")


# ============================================================
# CORE DATA RESET
# ============================================================


def clear_marketplace_operational_data():
    """Compatibility wrapper for the true fresh marketplace reset."""
    return reset_orders_and_items()


def clear_master_dataset(data_type):
    """Clear Products or Warehouses only when no dependent records exist."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        if data_type == "products":
            table = "products"
            label = "Products"
            checks = {
                "inventory": "SELECT COUNT(*) FROM inventory",
                "order_items": "SELECT COUNT(*) FROM order_items",
            }
        elif data_type == "warehouses":
            table = "warehouses"
            label = "Warehouses"
            checks = {
                "inventory": "SELECT COUNT(*) FROM inventory",
                "stock_transfers": "SELECT COUNT(*) FROM stock_transfers",
            }
        else:
            raise ValueError("Unsupported master dataset.")

        dependencies = []
        for dependency_table, query in checks.items():
            try:
                count = int(cur.execute(query).fetchone()[0])
            except sqlite3.OperationalError:
                count = 0
            if count:
                dependencies.append(f"{dependency_table}: {count}")

        if dependencies:
            raise ValueError(
                f"{label} cannot be cleared safely while dependent data exists "
                f"({', '.join(dependencies)})."
            )

        cur.execute("BEGIN")
        cur.execute(f"DELETE FROM {table}")
        affected = cur.rowcount
        conn.commit()
        return affected

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(
    BASE_DIR,
    "data",
)

if not os.path.isdir(DATA_DIR):
    DATA_DIR = BASE_DIR


# The reset workflow restores the original core datasets used by the
# application. It does not touch products or warehouses.
ORDERS_RESET_FILE = os.path.join(
    DATA_DIR,
    "marketplace_orders_300_final.csv",
)

ORDER_ITEMS_RESET_FILE = os.path.join(
    DATA_DIR,
    "marketplace_order_items_300.csv",
)

INVENTORY_RESET_FILE = os.path.join(
    DATA_DIR,
    "inventory.csv",
)


def _read_reset_csv(path):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Reset source file was not found: {path}"
        )

    return pd.read_csv(path)


def reset_orders_and_items():
    """Completely clear the current marketplace dataset.

    This is a TRUE RESET, not a restore-to-baseline operation.

    Cleared:
      - orders
      - order_items
      - fulfillment events
      - stock transfer requests/history
      - inventory transaction ledger
      - supply requests

    Inventory physical/system quantities are preserved, but all order-related
    reservations are removed so inventory returns to:
        reserved = 0
        available = system_quantity

    Products and warehouses are untouched.
    """
    conn = get_connection()

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")

        cleared = {}

        for table in (
            "fulfillment_events",
            "stock_transfers",
            "inventory_transactions",
            "supply_requests",
        ):
            try:
                cur = conn.execute(f"DELETE FROM {table}")
                cleared[table] = max(cur.rowcount, 0)
            except sqlite3.OperationalError:
                cleared[table] = 0

        # Physical/system stock remains. Only reservation state belongs to the
        # order workflow and must be cleared when all orders are removed.
        try:
            cur = conn.execute(
                """
                UPDATE inventory
                SET reserved_quantity = 0,
                    available_quantity = MAX(system_quantity, 0)
                """
            )
            cleared["inventory_reservations"] = max(cur.rowcount, 0)
        except sqlite3.OperationalError:
            cleared["inventory_reservations"] = 0

        cur = conn.execute("DELETE FROM order_items")
        cleared["order_items"] = max(cur.rowcount, 0)

        cur = conn.execute("DELETE FROM orders")
        cleared["orders"] = max(cur.rowcount, 0)

        conn.commit()
        return cleared

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_inventory_to_baseline():
    """Completely clear the current inventory dataset.

    The function name is retained for compatibility with existing calls, but
    RESET semantics are intentionally empty. Products, warehouses, orders and
    order_items are preserved. Inventory and inventory-dependent operational
    state are cleared so the next Inventory CSV becomes the new starting state.
    """
    conn = get_connection()

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")

        cleared = {}

        for table in (
            "stock_transfers",
            "inventory_transactions",
            "supply_requests",
        ):
            try:
                cur = conn.execute(f"DELETE FROM {table}")
                cleared[table] = max(cur.rowcount, 0)
            except sqlite3.OperationalError:
                cleared[table] = 0

        # Reservations cannot survive an inventory reset. Fulfillment events
        # are not deleted here because they describe order lifecycle history,
        # not the physical inventory snapshot itself.
        cur = conn.execute("DELETE FROM inventory")
        cleared["inventory"] = max(cur.rowcount, 0)

        conn.commit()
        return cleared

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_live_inventory_scenarios(inventory_df):
    """Derive one consistent live scenario/status for every inventory position.

    Inventory status is purely physical: Available, Reserved or Shortage.
    Scenario is operational: Sufficient, Low Stock, Transfer Required or
    Shortage. Both are derived from the same normalized balances, and the
    scenario is calculated at SKU + warehouse level so multiple bins cannot
    create conflicting status/scenario values for the same physical position.

    Read-only: this function never writes to SQLite.
    """
    if inventory_df is None or inventory_df.empty:
        return inventory_df.copy() if inventory_df is not None else pd.DataFrame()

    live_df = normalize_inventory_balances(inventory_df)
    planning_df = get_supply_planning_df(live_df)
    planning_map = {
        str(row["SKU"]): row
        for _, row in planning_df.iterrows()
    } if not planning_df.empty else {}

    position = (
        live_df
        .groupby(["sku", "warehouse_id"], as_index=False)
        .agg(
            system_quantity=("system_quantity", "sum"),
            reserved_quantity=("reserved_quantity", "sum"),
            available_quantity=("available_quantity", "sum"),
        )
    )

    scenario_map = {}
    for _, row in position.iterrows():
        sku = str(row["sku"])
        warehouse_id = str(row["warehouse_id"])
        available = int(row["available_quantity"] or 0)
        plan = planning_map.get(sku)

        if plan is None:
            scenario = (
                "Low Stock"
                if 0 < available <= LOW_STOCK_THRESHOLD
                else "Sufficient"
            )
        elif warehouse_id == "WH01":
            transfer_recommended = int(plan["Transfer Recommended"])
            additional_supply = int(plan["Additional Supply Required"])
            unreserved_demand = int(plan["Unreserved Demand"])

            if transfer_recommended > 0:
                scenario = "Transfer Required"
            elif additional_supply > 0:
                scenario = "Shortage"
            elif 0 < available <= LOW_STOCK_THRESHOLD:
                scenario = "Low Stock"
            elif unreserved_demand > 0 and available <= 0:
                scenario = "Shortage"
            else:
                scenario = "Sufficient"
        else:
            # Source/overflow stock is not itself a customer-facing transfer
            # alert. It remains operationally sufficient unless it is low.
            scenario = (
                "Low Stock"
                if 0 < available <= LOW_STOCK_THRESHOLD
                else "Sufficient"
            )

        scenario_map[(sku, warehouse_id)] = scenario

    live_df["scenario"] = [
        scenario_map.get(
            (str(sku), str(warehouse_id)),
            "Sufficient",
        )
        for sku, warehouse_id in zip(
            live_df["sku"], live_df["warehouse_id"]
        )
    ]

    return live_df


def get_live_inventory_alerts(inventory_df=None, filter_inventory_df=None):
    """Return one live operational alert per SKU/issue. Read-only.

    Inventory itself is stored at SKU + warehouse (and may contain more than
    one physical inventory row/bin). Inventory Alerts are deliberately a
    higher-level operational queue: one actionable issue per SKU.

    Rules:
    - Transfer/Shortage are represented at WH01 because WH01 is the shipping
      warehouse. Source stock is never shown as a second transfer alert.
    - Low Stock is represented once per SKU. WH01 is preferred when it is low;
      otherwise the lowest-stock low warehouse is used as the representative
      position.
    - If a SKU has Transfer Required or Shortage, its Low Stock condition is
      suppressed from the operational queue because the transfer/shortage is
      the more important action.
    - Numeric inventory quantities are aggregated by SKU + warehouse before
      an alert is created, so multiple bins/rows cannot create duplicate
      buttons or distorted alert quantities.
    - A page filter narrows the candidate inventory positions before the
      operational alert is consolidated.

    Read-only: no database mutation occurs.
    """
    if inventory_df is None:
        inventory_df = load_inventory()
    if inventory_df is None or inventory_df.empty:
        return pd.DataFrame()

    base = normalize_inventory_balances(inventory_df)

    if filter_inventory_df is not None:
        if filter_inventory_df.empty:
            return base.iloc[0:0].copy().reset_index(drop=True)
        filter_keys = (
            filter_inventory_df[["sku", "warehouse_id"]]
            .astype(str)
            .drop_duplicates()
        )
        base = base.copy()
        base["sku"] = base["sku"].astype(str)
        base["warehouse_id"] = base["warehouse_id"].astype(str)
        base = base.merge(
            filter_keys,
            on=["sku", "warehouse_id"],
            how="inner",
        )

    if base.empty:
        return base.reset_index(drop=True)

    # Collapse multiple physical inventory rows/bins into one live position
    # before generating an operational alert. Product/warehouse metadata is
    # stable for a given SKU/warehouse; quantities are summed.
    group_columns = ["sku", "warehouse_id", "scenario"]
    numeric_columns = [
        "system_quantity",
        "reserved_quantity",
        "available_quantity",
    ]
    for column in numeric_columns:
        if column in base.columns:
            base[column] = pd.to_numeric(
                base[column],
                errors="coerce",
            ).fillna(0).astype(int)

    agg_map = {}
    for column in base.columns:
        if column in group_columns or column in numeric_columns:
            continue
        if column == "bin_location":
            agg_map[column] = lambda values: ", ".join(
                dict.fromkeys(
                    str(value)
                    for value in values
                    if str(value) not in {"", "nan", "None"}
                )
            )
        else:
            agg_map[column] = "first"

    aggregated = (
        base.groupby(
            group_columns,
            as_index=False,
            dropna=False,
        )
        .agg({
            **{column: "sum" for column in numeric_columns if column in base.columns},
            **agg_map,
        })
    )

    # Transfer/shortage alerts are destination-side operational conditions.
    destination_alerts = aggregated[
        (aggregated["warehouse_id"].astype(str) == "WH01")
        & aggregated["scenario"].isin(["Shortage", "Transfer Required"])
    ].copy()

    # Low Stock is a planning signal. Consolidate all low-stock warehouse
    # positions for a SKU into one operational alert. Prefer WH01 when it is
    # low; otherwise show the lowest available low-stock warehouse.
    low_stock_candidates = aggregated[
        aggregated["scenario"] == "Low Stock"
    ].copy()

    low_stock_rows = []
    if not low_stock_candidates.empty:
        for sku, sku_rows in low_stock_candidates.groupby(
            low_stock_candidates["sku"].astype(str),
            sort=False,
        ):
            sku_rows = sku_rows.copy()
            wh01_rows = sku_rows[
                sku_rows["warehouse_id"].astype(str) == "WH01"
            ]
            if not wh01_rows.empty:
                chosen = wh01_rows.sort_values(
                    "available_quantity",
                    ascending=True,
                ).iloc[0].copy()
            else:
                chosen = sku_rows.sort_values(
                    ["available_quantity", "warehouse_id"],
                    ascending=[True, True],
                ).iloc[0].copy()

            low_warehouses = sorted(
                sku_rows["warehouse_id"].astype(str).unique().tolist()
            )
            chosen["low_stock_warehouses"] = ", ".join(low_warehouses)
            low_stock_rows.append(chosen)

    low_stock_alerts = (
        pd.DataFrame(low_stock_rows)
        if low_stock_rows
        else aggregated.iloc[0:0].copy()
    )

    # Transfer/shortage takes precedence over low stock for the same SKU.
    operational_skus = set(
        destination_alerts["sku"].astype(str).tolist()
    )
    if not low_stock_alerts.empty:
        low_stock_alerts = low_stock_alerts[
            ~low_stock_alerts["sku"].astype(str).isin(operational_skus)
        ].copy()

    alerts = pd.concat(
        [destination_alerts, low_stock_alerts],
        ignore_index=True,
    )

    if alerts.empty:
        return alerts.reset_index(drop=True)

    # Final operational uniqueness guard. This is deliberately SKU + scenario
    # rather than SKU + warehouse: one SKU/issue gets one action button.
    alerts = (
        alerts
        .drop_duplicates(
            subset=["sku", "scenario"],
            keep="first",
        )
        .reset_index(drop=True)
    )

    scenario_rank = {
        "Shortage": 1,
        "Transfer Required": 2,
        "Low Stock": 3,
    }
    alerts["_scenario_rank"] = (
        alerts["scenario"].map(scenario_rank).fillna(99)
    )

    return (
        alerts
        .sort_values(["_scenario_rank", "sku"])
        .drop(columns=["_scenario_rank"])
        .reset_index(drop=True)
    )


# ============================================================
# DATA LOADING
# ============================================================

def load_orders():

    conn = get_connection()

    query = """
        SELECT
            order_id,
            marketplace_order_id,
            order_date,
            channel,
            customer_name,
            shipping_city,
            shipping_state,
            pincode,
            priority,
            priority_reason,
            promised_ship_by,
            promised_delivery_date,
            payment_status,
            order_status,
            total_items,
            order_value,
            customer_type
        FROM orders
        ORDER BY
            CASE priority
                WHEN 'Critical' THEN 1
                WHEN 'High' THEN 2
                ELSE 3
            END,
            promised_ship_by ASC
    """

    df = pd.read_sql_query(
        query,
        conn
    )

    conn.close()

    return df


def load_inventory():

    conn = get_connection()

    query = """
        SELECT
            i.inventory_id,
            i.warehouse_id,
            w.warehouse_name,
            i.sku,
            p.product_name,
            p.category,
            p.brand,
            p.variant,
            p.size,
            p.color,
            i.bin_location,
            i.system_quantity,
            i.reserved_quantity,
            MAX(i.system_quantity - i.reserved_quantity, 0) AS available_quantity,
            i.last_verified_at,
            i.inventory_status,
            i.scenario
        FROM inventory i
        LEFT JOIN warehouses w
            ON i.warehouse_id = w.warehouse_id
        LEFT JOIN products p
            ON i.sku = p.sku
        ORDER BY
            i.warehouse_id,
            i.sku
    """

    df = pd.read_sql_query(
        query,
        conn
    )

    conn.close()

    # Available quantity and inventory status are always derived from the
    # authoritative physical balances. Scenario is then derived from live
    # demand/planning. Nothing is written back during a page refresh.
    df = normalize_inventory_balances(df)
    df = apply_live_inventory_scenarios(df)

    return df


def load_transfers():

    conn = get_connection()

    query = """
        SELECT
            st.transfer_id,
            st.sku,
            p.product_name,
            st.from_warehouse_id,
            fw.warehouse_name AS from_warehouse,
            st.to_warehouse_id,
            tw.warehouse_name AS to_warehouse,
            st.quantity,
            st.transfer_status,
            st.reference_order_id,
            st.requested_at,
            st.completed_at,
            st.notes
        FROM stock_transfers st
        LEFT JOIN products p
            ON st.sku = p.sku
        LEFT JOIN warehouses fw
            ON st.from_warehouse_id = fw.warehouse_id
        LEFT JOIN warehouses tw
            ON st.to_warehouse_id = tw.warehouse_id
        ORDER BY st.requested_at DESC
    """

    df = pd.read_sql_query(
        query,
        conn
    )

    conn.close()

    return df


def load_fulfillment_events():

    conn = get_connection()

    query = """
        SELECT
            fe.event_id,
            fe.order_id,
            fe.event_type,
            fe.event_status,
            fe.notes,
            fe.event_time
        FROM fulfillment_events fe
        ORDER BY fe.event_time DESC
        LIMIT 20
    """

    df = pd.read_sql_query(
        query,
        conn
    )

    conn.close()

    return df


# ============================================================
# LIVE INVENTORY ALERT STATE
# ============================================================

LOW_STOCK_THRESHOLD = 12


def refresh_inventory_alert_state(
    sku,
    warehouse_ids=None,
    action_type=None,
    transfer_destination_warehouse_id=None,
):
    """Align stored inventory_status with a completed physical action.

    Operational scenario is never written here. Scenario remains a derived
    live state calculated by ``load_inventory``. This helper only keeps the
    stored physical-status field consistent after a completed inventory
    action.

    Mutating: ``inventory.inventory_status`` may be updated.
    """
    warehouse_ids = {str(value) for value in (warehouse_ids or [])}
    conn = get_connection()
    try:
        rows = pd.read_sql_query(
            "SELECT inventory_id, warehouse_id, system_quantity, reserved_quantity FROM inventory WHERE sku = ?",
            conn,
            params=(sku,),
        )
        for _, row in rows.iterrows():
            warehouse_id = str(row["warehouse_id"])
            if warehouse_ids and warehouse_id not in warehouse_ids:
                continue
            if action_type in {
                "Receive Stock", "Transfer Stock", "Reserve Stock", "Pick Stock", "Stock Count"
            }:
                system_quantity = int(row["system_quantity"] or 0)
                reserved = int(row["reserved_quantity"] or 0)
                status = derive_inventory_status(system_quantity, reserved)

                conn.execute(
                    "UPDATE inventory SET inventory_status = ? WHERE inventory_id = ?",
                    (status, row["inventory_id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def load_order_items(order_id):

    conn = get_connection()

    query = """
        SELECT
            oi.line_item_id,
            oi.order_id,
            oi.sku,
            oi.product_name,
            oi.variant,
            oi.quantity,
            oi.unit_price,
            oi.line_total
        FROM order_items oi
        WHERE oi.order_id = ?
        ORDER BY oi.line_item_id
    """

    df = pd.read_sql_query(
        query,
        conn,
        params=(order_id,)
    )

    conn.close()

    return df


# ============================================================
# SKU DEMAND CALCULATION
# ============================================================

def get_sku_demand_summary(sku, allocation_snapshot=None):
    """Return the authoritative live demand/allocation summary for one SKU.

    This is read-only. The underlying allocation is calculated by the single
    shared engine in order_fulfillment.py, which is also used by Dashboard
    order metrics, Inventory Alerts and order action routing.
    """
    snapshot = allocation_snapshot or get_inventory_allocation_snapshot()
    summary = snapshot["sku_summary"].get(str(sku))

    if summary is not None:
        return summary.copy()

    return {
        "sku": str(sku),
        "total_open_demand": 0,
        "reserved_quantity": 0,
        "effective_reserved_quantity": 0,
        "unreserved_demand": 0,
        "total_available": 0,
        "destination_warehouse_id": "WH01",
        "destination_available": 0,
        "destination_gap": 0,
        "other_warehouse_available": {},
        "transferable_quantity": 0,
        "net_shortage": 0,
        "affected_orders": [],
        "affected_order_count": 0,
        "transfer_orders": [],
        "shortage_orders": [],
        "transfer_order_count": 0,
        "shortage_order_count": 0,
        "order_shortage_total": 0,
        "priority_summary": [],
    }

def get_sku_reservation_allocation(sku, warehouse_id="WH01"):
    """Calculate a read-only reservation allocation plan for one SKU.

    Stock is allocated in priority order: Critical, High, Normal, then
    promised ship date and order ID. Existing order-specific reservations
    are deducted before calculating any new allocation.

    This function does not modify the database.
    """

    conn = get_connection()

    try:
        active_clause, active_params = get_active_order_status_clause()
        orders_df = pd.read_sql_query(
            f"""
            SELECT
                o.order_id,
                o.priority,
                o.promised_ship_by,
                SUM(oi.quantity) AS requested_quantity
            FROM order_items oi
            INNER JOIN orders o
                ON o.order_id = oi.order_id
            WHERE oi.sku = ?
              AND o.{active_clause}
            GROUP BY o.order_id, o.priority, o.promised_ship_by
            ORDER BY
                CASE o.priority
                    WHEN 'Critical' THEN 1
                    WHEN 'High' THEN 2
                    ELSE 3
                END,
                o.promised_ship_by,
                o.order_id
            """,
            conn,
            params=(sku, *active_params),
        )

        reservation_df = pd.read_sql_query(
            """
            SELECT
                reference_id AS order_id,
                COALESCE(
                    SUM(
                        CASE
                            WHEN transaction_type = 'RESERVE'
                                THEN quantity
                            WHEN transaction_type IN ('PICK', 'RELEASE')
                                THEN -quantity
                            ELSE 0
                        END
                    ),
                    0
                ) AS reserved_quantity
            FROM inventory_transactions
            WHERE sku = ?
              AND warehouse_id = ?
              AND reference_id IS NOT NULL
            GROUP BY reference_id
            """,
            conn,
            params=(sku, warehouse_id),
        )

        inventory_row = conn.execute(
            """
            SELECT
                MAX(system_quantity - reserved_quantity, 0) AS available_quantity,
                reserved_quantity
            FROM inventory
            WHERE sku = ?
              AND warehouse_id = ?
            """,
            (sku, warehouse_id),
        ).fetchone()
    finally:
        conn.close()

    available_quantity = (
        int(inventory_row[0])
        if inventory_row is not None
        else 0
    )
    current_reserved_quantity = (
        int(inventory_row[1])
        if inventory_row is not None
        else 0
    )

    reservation_map = {}

    if not reservation_df.empty and current_reserved_quantity > 0:
        historical_reservations = {
            str(row["order_id"]): max(
                int(row["reserved_quantity"]),
                0,
            )
            for _, row in reservation_df.iterrows()
        }

        # Inventory.reserved_quantity is the current source of truth. The
        # transaction ledger is used only to identify which active orders
        # hold that current reservation. This prevents an Inventory Reset
        # from leaving historical reservations falsely attached to orders.
        remaining_current_reserved = current_reserved_quantity
        for _, order_row in orders_df.iterrows():
            order_key = str(order_row["order_id"])
            historical_quantity = historical_reservations.get(order_key, 0)
            if historical_quantity <= 0 or remaining_current_reserved <= 0:
                continue
            applied_quantity = min(
                historical_quantity,
                remaining_current_reserved,
            )
            reservation_map[order_key] = applied_quantity
            remaining_current_reserved -= applied_quantity

    allocation_rows = []
    remaining_available = available_quantity

    for _, row in orders_df.iterrows():
        order_id = str(row["order_id"])
        requested_quantity = int(row["requested_quantity"])
        already_reserved = min(
            requested_quantity,
            reservation_map.get(order_id, 0),
        )
        remaining_demand = max(
            requested_quantity - already_reserved,
            0,
        )

        allocated_now = min(
            remaining_demand,
            remaining_available,
        )

        remaining_after_allocation = max(
            remaining_demand - allocated_now,
            0,
        )

        remaining_available -= allocated_now

        allocation_rows.append(
            {
                "order_id": order_id,
                "priority": row["priority"] or "Normal",
                "promised_ship_by": row["promised_ship_by"],
                "requested_quantity": requested_quantity,
                "already_reserved": already_reserved,
                "remaining_demand": remaining_demand,
                "allocated_now": allocated_now,
                "remaining_short": remaining_after_allocation,
            }
        )

    allocation_df = pd.DataFrame(
        allocation_rows,
        columns=[
            "order_id",
            "priority",
            "promised_ship_by",
            "requested_quantity",
            "already_reserved",
            "remaining_demand",
            "allocated_now",
            "remaining_short",
        ],
    )

    if not allocation_df.empty:
        allocation_df["allocation_status"] = allocation_df.apply(
            lambda row: (
                "Fully Reserved"
                if row["remaining_demand"] == 0
                else "Fully Allocated"
                if row["remaining_short"] == 0
                else "Partially Allocated"
                if row["allocated_now"] > 0
                else "Unfulfilled"
            ),
            axis=1,
        )
    else:
        allocation_df["allocation_status"] = pd.Series(dtype=str)

    return {
        "sku": sku,
        "warehouse_id": warehouse_id,
        "available_quantity": available_quantity,
        "allocation_df": allocation_df,
        "total_requested": int(
            allocation_df["requested_quantity"].sum()
        ) if not allocation_df.empty else 0,
        "total_already_reserved": int(
            allocation_df["already_reserved"].sum()
        ) if not allocation_df.empty else 0,
        "total_allocated_now": int(
            allocation_df["allocated_now"].sum()
        ) if not allocation_df.empty else 0,
        "total_unfulfilled": int(
            allocation_df["remaining_short"].sum()
        ) if not allocation_df.empty else 0,
        "remaining_available": remaining_available,
    }


# ============================================================
# METRICS
# ============================================================

def calculate_order_inventory_metrics(orders_df, allocation_snapshot=None):
    """Calculate live order-level transfer and shortage metrics from one snapshot.

    Read-only. No per-order recalculation or silent exception-to-zero fallback
    is used. The same authoritative allocation snapshot feeds Dashboard,
    Inventory Alerts and order-level action routing.
    """
    active_orders = get_active_orders_df(orders_df)
    if active_orders.empty:
        return {
            "orders_requiring_transfer": 0,
            "orders_with_shortage": 0,
        }

    active_order_ids = set(
        active_orders["order_id"].astype(str).tolist()
    )
    snapshot = allocation_snapshot or get_inventory_allocation_snapshot()

    transfer_order_ids = {
        str(order_id)
        for order_id, summary in snapshot["order_summary"].items()
        if str(order_id) in active_order_ids
        and bool(summary.get("has_transfer"))
    }
    shortage_order_ids = {
        str(order_id)
        for order_id, summary in snapshot["order_summary"].items()
        if str(order_id) in active_order_ids
        and bool(summary.get("has_shortage"))
    }

    return {
        "orders_requiring_transfer": len(transfer_order_ids),
        "orders_with_shortage": len(shortage_order_ids),
    }

def calculate_metrics(
    orders_df,
    inventory_df,
    transfers_df,
    allocation_snapshot=None,
):
    """Calculate dashboard metrics from the same live datasets shown below.

    Read-only: no database mutation occurs.
    """

    today = datetime.now().date()

    total_orders = len(orders_df)

    fulfillment_active_orders = get_fulfillment_active_orders_df(orders_df)

    priority_orders = len(
        fulfillment_active_orders[
            fulfillment_active_orders["priority"].isin(["High", "Critical"])
        ]
    )

    critical_orders = len(
        fulfillment_active_orders[
            fulfillment_active_orders["priority"] == "Critical"
        ]
    )

    overdue_orders = 0
    if not fulfillment_active_orders.empty:
        temp_orders = fulfillment_active_orders.copy()
        temp_orders["promised_ship_by"] = pd.to_datetime(
            temp_orders["promised_ship_by"],
            errors="coerce",
        )
        overdue_orders = int(
            (temp_orders["promised_ship_by"].dt.normalize() < pd.Timestamp.today().normalize())
            .fillna(False)
            .sum()
        )

    order_inventory_metrics = calculate_order_inventory_metrics(
        orders_df,
        allocation_snapshot=allocation_snapshot,
    )

    live_alerts = get_live_inventory_alerts(inventory_df)

    low_stock = count_live_inventory_alerts(
        live_alerts, "Low Stock"
    )

    active_transfers = len(
        transfers_df[
            transfers_df["transfer_status"].isin(
                ["REQUESTED", "IN_TRANSIT"]
            )
        ]
    )

    return {
        "total_orders": total_orders,
        "priority_orders": priority_orders,
        "critical_orders": critical_orders,
        "overdue_orders": overdue_orders,
        "orders_requiring_transfer": int(
            order_inventory_metrics["orders_requiring_transfer"]
        ),
        "orders_with_shortage": int(
            order_inventory_metrics["orders_with_shortage"]
        ),
        "low_stock": int(low_stock),
        "active_transfers": int(active_transfers),
    }


# ============================================================
# ACTION REQUIRED
# ============================================================

def get_action_required_orders(orders_df, allocation_snapshot=None):
    """Return only inventory-gating actions from one authoritative snapshot.

    The Dashboard queue intentionally excludes PICK/PACK/STAGE/SHIP. Those are
    execution steps handled by Fulfillment Operations after inventory is ready.
    Read-only and does not silently convert calculation errors into zero rows.
    """
    active_orders = get_active_orders_df(orders_df)
    if active_orders.empty:
        return pd.DataFrame(columns=["order_id", "next_action"])

    snapshot = allocation_snapshot or get_inventory_allocation_snapshot()
    rows = []

    for order_id in active_orders["order_id"].astype(str):
        order_summary = snapshot["order_summary"].get(str(order_id), {})
        items = [
            allocation
            for (allocation_order_id, _sku), allocation
            in snapshot["order_item_allocations"].items()
            if allocation_order_id == str(order_id)
        ]

        if bool(order_summary.get("has_transfer")):
            next_action = "TRANSFER"
        elif bool(order_summary.get("has_shortage")):
            next_action = "SHORTAGE"
        else:
            all_reserved = bool(items) and all(
                int(item["remaining_requirement"])
                <= int(item["reserved_quantity"])
                for item in items
            )
            next_action = None if all_reserved else "RESERVE"

        if next_action is not None:
            rows.append({
                "order_id": str(order_id),
                "next_action": next_action,
            })

    return pd.DataFrame(rows, columns=["order_id", "next_action"])


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title(
        "Fulfillment Control Tower"
    )

    st.caption(
        "Operational overview of live orders, inventory and fulfillment activity. All metrics and tables are recalculated from the current SQLite state on page load. Order and inventory views use one shared live allocation engine for transfer and shortage decisions."
    )

    orders_df = load_orders()

    inventory_df = load_inventory()

    transfers_df = load_transfers()

    allocation_snapshot = get_inventory_allocation_snapshot()

    events_df = load_fulfillment_events()

    metrics = calculate_metrics(
        orders_df,
        inventory_df,
        transfers_df,
        allocation_snapshot=allocation_snapshot,
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Total Orders",
        metrics["total_orders"]
    )

    col2.metric(
        "Priority Orders",
        metrics["priority_orders"]
    )

    col3.metric(
        "Critical Orders",
        metrics["critical_orders"]
    )

    col4.metric(
        "Overdue Orders",
        metrics["overdue_orders"]
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Orders Requiring Transfer",
        metrics["orders_requiring_transfer"]
    )

    col2.metric(
        "Orders With Shortage",
        metrics["orders_with_shortage"]
    )

    col3.metric(
        "Low Stock Items",
        metrics["low_stock"]
    )

    col4.metric(
        "Active Transfers",
        metrics["active_transfers"]
    )

    st.divider()

    st.subheader(
        "Action Required"
    )

    action_orders = (
        get_action_required_orders(
            orders_df,
            allocation_snapshot=allocation_snapshot,
        )
    )

    if action_orders.empty:

        st.success(
            "No immediate fulfillment actions required."
        )

    else:

        action_display = (
            action_orders.merge(
                orders_df[
                    [
                        "order_id",
                        "priority",
                        "customer_name",
                        "promised_ship_by",
                        "order_status",
                        "order_value"
                    ]
                ],
                on="order_id",
                how="left"
            )
        )

        # --------------------------------------------------------
        # ACTIONABLE QUEUE
        # --------------------------------------------------------
        # The dashboard should not only report what needs attention;
        # it should provide a direct handoff into the appropriate
        # operational workflow. Clicking an action button below only
        # changes Streamlit navigation/session state. It does not
        # modify SQLite. The database changes only after the employee
        # executes the final action in the destination workflow.

        action_display = action_display.copy()

        priority_rank = {
            "Critical": 1,
            "High": 2,
            "Normal": 3,
        }

        action_display["_priority_rank"] = (
            action_display["priority"]
            .map(priority_rank)
            .fillna(4)
        )

        action_display["_ship_date"] = pd.to_datetime(
            action_display["promised_ship_by"],
            errors="coerce"
        )

        action_display = action_display.sort_values(
            ["_priority_rank", "_ship_date", "order_id"]
        ).reset_index(drop=True)

        action_display = action_display.drop(
            columns=["_priority_rank", "_ship_date"]
        )

        st.caption(
            "This queue shows inventory-gating actions only: transfer, shortage and reservation. "
            "Once stock is reserved, the order moves to Fulfillment Operations for pick, pack, stage and ship execution."
        )

        for action_index, action_row in action_display.head(15).iterrows():

            row_col1, row_col2, row_col3, row_col4, row_col5 = st.columns(
                [1.5, 1.0, 2.2, 2.0, 1.4]
            )

            row_col1.write(
                f"**{action_row['order_id']}**"
            )

            row_col2.write(
                f"**{action_row['priority']}**"
            )

            row_col3.write(
                f"{action_row['customer_name']}"
            )

            row_col4.write(
                f"{action_row['next_action']} | "
                f"Ship by: {action_row['promised_ship_by']}"
            )

            if row_col5.button(
                "Open Action",
                key=f"dashboard_action_{action_index}_{action_row['order_id']}",
                type="primary"
            ):

                order_id = str(action_row["order_id"])
                next_action = str(action_row["next_action"])

                # Navigation handoff only. No database mutation occurs here.
                if next_action == "SHORTAGE":

                    st.session_state["requested_navigation_page"] = "Supply Planning"
                    st.session_state["supply_request_reference_order"] = order_id

                    try:
                        stock_assessment = check_order_stock(order_id)
                        shortage_items = [
                            item for item in stock_assessment.get("items", [])
                            if int(item.get("shortage", 0)) > 0
                        ]
                        if shortage_items:
                            item = shortage_items[0]
                            st.session_state["supply_request_sku"] = str(item["sku"])
                            st.session_state["supply_request_quantity"] = max(int(item["shortage"]), 1)
                            st.session_state["supply_request_warehouse"] = "WH01"
                    except Exception:
                        pass

                elif next_action in ["TRANSFER", "RESERVE", "PICK"]:

                    st.session_state["requested_navigation_page"] = "Inventory Actions"
                    st.session_state["inventory_action_order_id"] = order_id

                    try:
                        stock_assessment = check_order_stock(order_id)
                        if next_action == "TRANSFER":
                            candidate_items = [
                                item for item in stock_assessment.get("items", [])
                                if int(item.get("transfer_required", 0)) > 0
                            ]
                        else:
                            candidate_items = stock_assessment.get("items", [])

                        if candidate_items:
                            item = candidate_items[0]
                            st.session_state["transfer_sku_general"] = str(item["sku"])
                            st.session_state["reserve_order"] = order_id
                            st.session_state["pick_order"] = order_id
                            if next_action == "TRANSFER":
                                st.session_state["requested_inventory_action"] = "Transfer Stock"
                                st.session_state["transfer_reference_order"] = order_id
                                st.session_state["transfer_to"] = "WH01"
                                source_df = inventory_df[
                                    (inventory_df["sku"] == str(item["sku"]))
                                    & (inventory_df["warehouse_id"] != "WH01")
                                    & (inventory_df["available_quantity"] > 0)
                                ].sort_values("available_quantity", ascending=False)
                                if not source_df.empty:
                                    st.session_state["transfer_from"] = str(source_df.iloc[0]["warehouse_id"])
                                    st.session_state["transfer_quantity"] = min(
                                        int(item.get("transfer_required", 1)),
                                        int(source_df.iloc[0]["available_quantity"]),
                                    )
                            elif next_action == "RESERVE":
                                st.session_state["requested_inventory_action"] = "Reserve Stock"
                            elif next_action == "PICK":
                                st.session_state["requested_inventory_action"] = "Pick Stock"
                    except Exception:
                        st.session_state["requested_inventory_action"] = {
                            "TRANSFER": "Transfer Stock",
                            "RESERVE": "Reserve Stock",
                            "PICK": "Pick Stock",
                        }[next_action]

                st.rerun()

        if len(action_display) > 15:
            st.caption(
                f"Showing the 15 highest-priority action(s) out of "
                f"{len(action_display)} requiring attention."
            )

    st.divider()

    st.subheader(
        "Inventory Alerts"
    )

    st.caption(
        "Live alert state calculated from current orders, reservations, inventory, transfers and confirmed incoming supply."
    )

    dashboard_alerts = get_live_inventory_alerts(inventory_df)

    if dashboard_alerts.empty:

        st.success(
            "No inventory alerts."
        )

    else:

        dashboard_alerts = enrich_inventory_alerts_with_demand(
            dashboard_alerts,
            allocation_snapshot=allocation_snapshot,
        ).copy()

        dashboard_alerts["recommended_action"] = dashboard_alerts.apply(
            lambda row: get_inventory_alert_action_state(
                row["sku"],
                row["warehouse_id"],
                inventory_df,
                allocation_snapshot=allocation_snapshot,
                scenario=row["scenario"],
            )["recommended_action"],
            axis=1,
        )

        dashboard_alert_columns = [
            "sku",
            "product_name",
            "warehouse_id",
            "warehouse_name",
            "available_quantity",
            "unreserved_demand",
            "net_shortage",
            "scenario",
            "recommended_action",
        ]

        st.dataframe(
            dashboard_alerts[dashboard_alert_columns],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    st.subheader(
        "Recent Fulfillment Activity"
    )

    if events_df.empty:

        st.info(
            "No fulfillment events recorded yet."
        )

    else:

        st.dataframe(
            events_df,
            use_container_width=True,
            hide_index=True
        )

    st.divider()

    st.subheader(
        "Recent Stock Transfers"
    )

    if transfers_df.empty:

        st.info(
            "No stock transfers recorded yet."
        )

    else:

        st.dataframe(
            transfers_df,
            use_container_width=True,
            hide_index=True
        )


# ============================================================
# ORDERS PAGE
# ============================================================

def show_orders_page():

    st.title(
        "Orders"
    )

    st.caption(
        "Review orders, fulfillment status and inventory requirements"
    )

    orders_df = load_orders()

    if orders_df.empty:

        st.warning(
            "No orders found."
        )

        return

    st.subheader(
        "Order Filters"
    )

    col1, col2, col3 = st.columns(3)

    with col1:

        search_order = st.text_input(
            "Search Order ID",
            placeholder="Enter order ID"
        )

    with col2:

        selected_priority = st.selectbox(
            "Priority",
            [
                "All",
                "Critical",
                "High",
                "Normal"
            ]
        )

    with col3:

        status_options = [
            "All"
        ] + sorted(
            orders_df[
                "order_status"
            ]
            .dropna()
            .unique()
            .tolist()
        )

        selected_status = st.selectbox(
            "Order Status",
            status_options
        )

    filtered_orders = (
        orders_df.copy()
    )

    if search_order:

        filtered_orders = (
            filtered_orders[
                filtered_orders[
                    "order_id"
                ]
                .astype(str)
                .str.contains(
                    search_order,
                    case=False,
                    na=False
                )
            ]
        )

    if selected_priority != "All":

        filtered_orders = (
            filtered_orders[
                filtered_orders[
                    "priority"
                ]
                == selected_priority
            ]
        )

    if selected_status != "All":

        filtered_orders = (
            filtered_orders[
                filtered_orders[
                    "order_status"
                ]
                == selected_status
            ]
        )

    allocation_snapshot = get_inventory_allocation_snapshot()
    order_inventory_metrics = calculate_order_inventory_metrics(
        filtered_orders,
        allocation_snapshot=allocation_snapshot,
    )

    st.subheader("Order-Level Inventory Metrics")

    metric_col1, metric_col2, metric_col3 = st.columns(3)

    metric_col1.metric(
        "Total Orders",
        len(filtered_orders)
    )

    metric_col2.metric(
        "Orders Requiring Transfer",
        order_inventory_metrics["orders_requiring_transfer"]
    )

    metric_col3.metric(
        "Orders With Shortage",
        order_inventory_metrics["orders_with_shortage"]
    )

    st.subheader(
        f"Orders ({len(filtered_orders)})"
    )

    order_columns = [
        "order_id",
        "priority",
        "customer_name",
        "shipping_city",
        "promised_ship_by",
        "promised_delivery_date",
        "order_status",
        "total_items",
        "order_value"
    ]

    st.dataframe(
        filtered_orders[
            order_columns
        ],
        use_container_width=True,
        hide_index=True
    )

    if filtered_orders.empty:

        st.warning(
            "No orders match the selected filters."
        )

        return

    selected_order = st.selectbox(
        "Select an Order",
        filtered_orders[
            "order_id"
        ].tolist()
    )

    selected_order_data = (
        filtered_orders[
            filtered_orders[
                "order_id"
            ]
            == selected_order
        ].iloc[0]
    )

    st.divider()

    st.subheader(
        f"Order Details — {selected_order}"
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Priority",
        selected_order_data[
            "priority"
        ]
    )

    col2.metric(
        "Status",
        selected_order_data[
            "order_status"
        ]
    )

    col3.metric(
        "Order Value",
        f"₹{selected_order_data['order_value']:,.2f}"
    )

    col4.metric(
        "Total Items",
        selected_order_data[
            "total_items"
        ]
    )

    st.subheader(
        "Customer & Shipping"
    )

    customer_col1, customer_col2 = (
        st.columns(2)
    )

    with customer_col1:

        st.write(
            f"**Customer:** "
            f"{selected_order_data['customer_name']}"
        )

        st.write(
            f"**Customer Type:** "
            f"{selected_order_data['customer_type']}"
        )

        st.write(
            f"**Channel:** "
            f"{selected_order_data['channel']}"
        )

    with customer_col2:

        st.write(
            f"**City:** "
            f"{selected_order_data['shipping_city']}"
        )

        st.write(
            f"**State:** "
            f"{selected_order_data['shipping_state']}"
        )

        st.write(
            f"**Pincode:** "
            f"{selected_order_data['pincode']}"
        )

    st.write(
        f"**Promised Ship By:** "
        f"{selected_order_data['promised_ship_by']}"
    )

    st.write(
        f"**Promised Delivery:** "
        f"{selected_order_data['promised_delivery_date']}"
    )

    st.write(
        f"**Payment Status:** "
        f"{selected_order_data['payment_status']}"
    )

    if pd.notna(
        selected_order_data[
            "priority_reason"
        ]
    ):

        st.info(
            "Priority Reason: "
            + str(
                selected_order_data[
                    "priority_reason"
                ]
            )
        )

    st.subheader(
        "Order Items"
    )

    order_items = load_order_items(
        selected_order
    )

    st.dataframe(
        order_items,
        use_container_width=True,
        hide_index=True
    )

    st.subheader(
        "Inventory Assessment"
    )

    try:

        stock_result = (
            check_order_stock(
                selected_order
            )
        )

        stock_status = (
            stock_result[
                "overall_status"
            ]
        )

        if stock_status == "AVAILABLE":

            st.success(
                "All required stock is available in the main warehouse."
            )

        elif stock_status == "TRANSFER_REQUIRED":

            st.warning(
                "Stock is available, but transfer from another warehouse is required."
            )

        else:

            st.error(
                "The order has one or more stock shortages."
            )

        item_details = (
            stock_result[
                "items"
            ]
        )

        if item_details:

            inventory_assessment = (
                pd.DataFrame(
                    item_details
                )
            )

            st.dataframe(
                inventory_assessment,
                use_container_width=True,
                hide_index=True
            )

    except Exception as e:

        st.error(
            f"Unable to calculate inventory assessment: {e}"
        )

    st.subheader(
        "Recommended Next Action"
    )

    try:

        next_action = (
            get_next_fulfillment_action(
                selected_order
            )
        )

        if next_action == "TRANSFER":

            st.warning(
                "TRANSFER — Move required stock to the main warehouse."
            )

        elif next_action == "RESERVE":

            st.info(
                "RESERVE — Stock is available and should be reserved for this order."
            )

        elif next_action == "PICK":

            st.info(
                "PICK — Stock is reserved and the order is ready for picking."
            )

        elif next_action == "SHORTAGE":

            st.error(
                "SHORTAGE — Required inventory is not currently available."
            )

        elif next_action == "COMPLETED":

            st.success(
                "COMPLETED — All required picking has been completed."
            )

        else:

            st.info(
                f"Next action: {next_action}"
            )

    except Exception as e:

        st.error(
            f"Unable to determine next action: {e}"
        )


# ============================================================
# INVENTORY PAGE
# ============================================================

def _show_inventory_overview_page():

    st.title(
        "Inventory"
    )

    st.caption(
        "Live inventory position across fulfillment warehouses. Inventory metrics and the Inventory Alerts table are calculated from the current SQLite state."
    )

    inventory_df = load_inventory()

    if inventory_df.empty:

        st.warning(
            "No inventory records found."
        )

        return

    total_system_quantity = (
        inventory_df[
            "system_quantity"
        ].sum()
    )

    total_reserved_quantity = (
        inventory_df[
            "reserved_quantity"
        ].sum()
    )

    total_available_quantity = (
        inventory_df[
            "available_quantity"
        ].sum()
    )

    allocation_snapshot = get_inventory_allocation_snapshot()
    live_alerts = get_live_inventory_alerts(inventory_df)
    live_alert_details = enrich_inventory_alerts_with_demand(
        live_alerts,
        allocation_snapshot=allocation_snapshot,
    )

    # Shortage is a SKU-level network condition. A SKU may be marked
    # Transfer Required because WH01 needs stock from WH02 while still
    # having an additional network shortage. Count each shortage SKU once,
    # even if duplicate source rows exist in the inventory table.
    shortage_alerts = int(
        live_alert_details.loc[
            live_alert_details["net_shortage"] > 0,
            "sku"
        ].astype(str).nunique()
    ) if not live_alert_details.empty else 0

    transfer_alerts = count_live_inventory_alerts(
        live_alerts, "Transfer Required"
    )

    low_stock_alerts = count_live_inventory_alerts(
        live_alerts, "Low Stock"
    )

    st.subheader("Network Inventory Metrics")

    col1, col2, col3, col4 = (
        st.columns(4)
    )

    col1.metric(
        "Total Inventory Items",
        len(inventory_df)
    )

    col2.metric(
        "System Quantity",
        int(total_system_quantity)
    )

    col3.metric(
        "Reserved Stock",
        int(total_reserved_quantity)
    )

    col4.metric(
        "Available Stock",
        int(total_available_quantity)
    )

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Low Stock Items",
        low_stock_alerts
    )

    col2.metric(
        "Transfer Required",
        transfer_alerts
    )

    col3.metric(
        "Shortage Items",
        shortage_alerts
    )

    st.divider()

    st.subheader(
        "Inventory Filters"
    )

    st.caption(
        "Inventory Records show physical stock only: System Quantity, Reserved, Available and Inventory Status. Operational issues and actions are shown separately in Inventory Alerts below."
    )

    col1, col2, col3, col4 = (
        st.columns(4)
    )

    with col1:

        search_sku = st.text_input(
            "Search SKU",
            placeholder="e.g. TS-BLK-M"
        )

    with col2:

        warehouse_options = [
            "All"
        ] + sorted(
            inventory_df[
                "warehouse_id"
            ]
            .dropna()
            .unique()
            .tolist()
        )

        selected_warehouse = (
            st.selectbox(
                "Warehouse",
                warehouse_options
            )
        )

    with col3:

        status_options = [
            "All"
        ] + sorted(
            inventory_df[
                "inventory_status"
            ]
            .dropna()
            .unique()
            .tolist()
        )

        selected_inventory_status = (
            st.selectbox(
                "Inventory Status",
                status_options
            )
        )

    filtered_inventory = (
        inventory_df.copy()
    )

    if search_sku:

        filtered_inventory = (
            filtered_inventory[
                filtered_inventory[
                    "sku"
                ]
                .astype(str)
                .str.contains(
                    search_sku,
                    case=False,
                    na=False
                )
            ]
        )

    if selected_warehouse != "All":

        filtered_inventory = (
            filtered_inventory[
                filtered_inventory[
                    "warehouse_id"
                ]
                == selected_warehouse
            ]
        )

    if selected_inventory_status != "All":

        filtered_inventory = (
            filtered_inventory[
                filtered_inventory[
                    "inventory_status"
                ]
                == selected_inventory_status
            ]
        )

    filtered_live_alerts = get_live_inventory_alerts(
        inventory_df,
        filter_inventory_df=filtered_inventory,
    )
    allocation_snapshot = get_inventory_allocation_snapshot()
    filtered_live_alert_details = enrich_inventory_alerts_with_demand(
        filtered_live_alerts,
        allocation_snapshot=allocation_snapshot,
    )

    st.subheader("Selected View Metrics")

    filtered_system_quantity = int(filtered_inventory["system_quantity"].sum())
    filtered_reserved_quantity = int(filtered_inventory["reserved_quantity"].sum())
    filtered_available_quantity = int(filtered_inventory["available_quantity"].sum())

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    metric_col1.metric("Inventory Items", len(filtered_inventory))
    metric_col2.metric("System Quantity", filtered_system_quantity)
    metric_col3.metric("Reserved Stock", filtered_reserved_quantity)
    metric_col4.metric("Available Stock", filtered_available_quantity)

    filtered_shortage_items = int(
        filtered_live_alert_details.loc[
            filtered_live_alert_details["net_shortage"] > 0,
            "sku"
        ].astype(str).nunique()
    ) if not filtered_live_alert_details.empty else 0

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Low Stock Items", count_live_inventory_alerts(filtered_live_alerts, "Low Stock"))
    metric_col2.metric("Transfer Required", count_live_inventory_alerts(filtered_live_alerts, "Transfer Required"))
    metric_col3.metric("Shortage Items", filtered_shortage_items)

    st.subheader(
        f"Inventory Records ({len(filtered_inventory)})"
    )

    display_columns = [
        "sku",
        "product_name",
        "warehouse_id",
        "warehouse_name",
        "bin_location",
        "system_quantity",
        "reserved_quantity",
        "available_quantity",
        "inventory_status"
    ]

    st.dataframe(
        filtered_inventory[
            display_columns
        ],
        use_container_width=True,
        hide_index=True
    )

    st.divider()

    st.subheader(
        "Inventory Alerts"
    )

    st.caption(
        "One operational alert per SKU. Inventory Records above show physical warehouse stock; this section shows only the operational issue and the next action. If both transfer and shortage exist, transfer is handled first and the remaining shortage goes to Supply Planning."
    )

    alerts = filtered_live_alerts

    if alerts.empty:

        st.success(
            "No inventory alerts for the selected filters."
        )

    else:

        alert_display = enrich_inventory_alerts_with_demand(
            alerts
        ).copy()

        alert_display["recommended_action"] = alert_display.apply(
            lambda row: get_inventory_alert_action_state(
                row["sku"],
                row["warehouse_id"],
                inventory_df,
                allocation_snapshot=allocation_snapshot,
                scenario=row["scenario"],
            )["recommended_action"],
            axis=1,
        )

        # Keep the operational alert table at SKU/issue level. Physical
        # warehouse status belongs in Inventory Records; this table answers
        # only what operational problem exists and what to do next.
        def _operational_issue(row):
            transfer_qty = int(row.get("transfer_quantity", 0) or 0)
            shortage_qty = int(row.get("net_shortage", 0) or 0)
            scenario_value = str(row.get("scenario", ""))
            if transfer_qty > 0 and shortage_qty > 0:
                return "Transfer + Shortage"
            if transfer_qty > 0:
                return "Transfer Required"
            if shortage_qty > 0:
                return "Shortage"
            if scenario_value == "Low Stock":
                return "Low Stock"
            return scenario_value

        # Use the same live action state for the displayed quantities so the
        # table and its buttons cannot disagree.
        alert_display["transfer_quantity"] = alert_display.apply(
            lambda row: get_inventory_alert_action_state(
                row["sku"],
                row["warehouse_id"],
                inventory_df,
                allocation_snapshot=allocation_snapshot,
                scenario=row["scenario"],
            )["transfer_quantity"],
            axis=1,
        )
        alert_display["operational_issue"] = alert_display.apply(
            _operational_issue,
            axis=1,
        )
        alert_display["additional_supply_required"] = alert_display[
            "net_shortage"
        ].astype(int).clip(lower=0)

        alert_columns = [
            "sku",
            "product_name",
            "available_quantity",
            "unreserved_demand",
            "transfer_quantity",
            "additional_supply_required",
            "affected_orders",
            "operational_issue",
            "recommended_action",
        ]

        st.dataframe(
            alert_display[alert_columns].rename(columns={
                "available_quantity": "WH01 Available",
                "unreserved_demand": "Unreserved Demand",
                "transfer_quantity": "Transfer Recommended",
                "additional_supply_required": "Additional Supply Required",
                "affected_orders": "Affected Orders",
                "operational_issue": "Issue",
                "recommended_action": "Next Action",
            }),
            use_container_width=True,
            hide_index=True,
        )

        st.caption(
            "Each operational alert appears once per SKU. Use the recommended action to hand the issue to Inventory Actions or Supply Planning. Selecting an action does not change the database; the database changes only when the final action button is clicked."
        )

        # ----------------------------------------------------
        # ALERT-TO-ACTION HANDOFF
        # ----------------------------------------------------

        for alert_index, (_, alert_row) in enumerate(
            alerts.iterrows()
        ):

            sku = str(alert_row["sku"])
            warehouse_id = str(alert_row["warehouse_id"])
            scenario = str(alert_row["scenario"])
            product_name = str(alert_row["product_name"])
            action_state = get_inventory_alert_action_state(
                sku,
                warehouse_id,
                inventory_df,
                allocation_snapshot=allocation_snapshot,
                scenario=scenario,
            )
            demand = action_state["demand"]
            transfer_quantity = action_state["transfer_quantity"]
            network_shortage = int(demand["net_shortage"])

            row_col1, row_col2, row_col3, row_col4 = st.columns(
                [2.0, 2.2, 2.6, 1.7]
            )

            row_col1.write(
                f"**{sku}** — {product_name}"
            )

            if (
                transfer_quantity > 0
                and network_shortage > 0
            ):
                row_col2.write(
                    "**Transfer first → Supply Planning**"
                )
                row_col3.write(
                    f"Transfer: {transfer_quantity} | "
                    f"Additional Supply: {network_shortage}"
                )
            elif transfer_quantity > 0:
                row_col2.write(
                    "**Transfer Required**"
                )
                row_col3.write(
                    f"Transfer: {transfer_quantity}"
                )
            elif network_shortage > 0:
                row_col2.write(
                    "**Supply Planning**"
                )
                row_col3.write(
                    f"Additional Supply: {network_shortage}"
                )
            else:
                row_col2.write(
                    f"{warehouse_id} — {scenario}"
                )
                row_col3.write(
                    f"Available: {int(alert_row['available_quantity'])}"
                )

            if transfer_quantity > 0:
                button_label = (
                    "1. Transfer Stock"
                    if network_shortage > 0
                    else "Transfer Stock"
                )

                if row_col4.button(
                    button_label,
                    key=f"inventory_alert_transfer_{alert_index}_{sku}_{scenario}_{warehouse_id}",
                ):
                    # Carry the alert into Inventory Actions. Selecting the
                    # action is read-only; the database changes only when the
                    # Transfer Stock button is clicked there.
                    st.session_state["requested_navigation_page"] = (
                        "Inventory Actions"
                    )

                    st.session_state["inventory_action_context"] = {
                        "scenario": scenario,
                        "sku": sku,
                        "warehouse_id": warehouse_id,
                        "product_name": product_name,
                        "available_quantity": int(
                            alert_row["available_quantity"]
                        ),
                        "total_open_demand": demand["total_open_demand"],
                        "unreserved_demand": demand["unreserved_demand"],
                        "net_shortage": network_shortage,
                        "affected_orders": demand["affected_orders"],
                        "transfer_quantity": transfer_quantity,
                        "next_step": (
                            "Supply Planning"
                            if network_shortage > 0
                            else None
                        ),
                    }

                    source_warehouse_id = action_state["source_warehouse_id"]
                    source_available = action_state["source_available"]

                    st.session_state["inventory_action_type"] = "Transfer Stock"
                    st.session_state["transfer_reference_order"] = "No specific order"
                    st.session_state["transfer_sku_general"] = sku
                    st.session_state["transfer_from"] = (
                        source_warehouse_id
                        if source_warehouse_id is not None
                        else warehouse_id
                    )
                    st.session_state["transfer_to"] = warehouse_id
                    st.session_state["transfer_quantity"] = max(
                        min(transfer_quantity, source_available),
                        1,
                    )
                    st.session_state["transfer_demand_quantity"] = max(
                        int(demand["destination_gap"]),
                        0,
                    )
                    st.session_state["transfer_affected_orders"] = demand[
                        "affected_orders"
                    ]
                    st.rerun()

            elif network_shortage > 0:
                if row_col4.button(
                    "Supply Planning",
                    key=f"inventory_alert_supply_{alert_index}_{sku}_{scenario}_{warehouse_id}",
                ):
                    # A pure network shortage goes directly to Supply
                    # Planning. No inventory transaction occurs here.
                    st.session_state["requested_navigation_page"] = (
                        "Supply Planning"
                    )
                    st.session_state["supply_request_sku"] = sku
                    st.session_state["supply_request_quantity"] = max(
                        network_shortage,
                        1,
                    )
                    st.session_state["supply_request_quantity_sku"] = sku
                    st.session_state["supply_request_last_calculated_quantity"] = network_shortage
                    st.session_state["supply_request_warehouse"] = "WH01"
                    st.session_state["supply_request_priority"] = (
                        "Critical"
                        if any("Critical:" in item for item in demand["priority_summary"])
                        else "High"
                        if any("High:" in item for item in demand["priority_summary"])
                        else "Normal"
                    )
                    st.session_state["supply_request_priority_sku"] = sku
                    st.rerun()

            elif scenario == "Low Stock":
                if row_col4.button(
                    "Supply Planning",
                    key=f"inventory_alert_low_stock_supply_{alert_index}_{sku}_{scenario}_{warehouse_id}",
                ):
                    # Low Stock is a replenishment-planning condition. It is
                    # not evidence that stock has physically arrived, so it
                    # must not route the operator to Receive Stock.
                    st.session_state["requested_navigation_page"] = (
                        "Supply Planning"
                    )
                    st.session_state["supply_request_sku"] = sku
                    st.session_state["supply_request_quantity_sku"] = sku
                    st.session_state["supply_request_warehouse"] = "WH01"
                    st.session_state["supply_request_priority"] = "Normal"
                    st.session_state["supply_request_priority_sku"] = sku
                    st.session_state.pop("supply_request_quantity", None)
                    st.session_state.pop("supply_request_last_calculated_quantity", None)
                    st.rerun()

            else:
                if row_col4.button(
                    "Inventory Actions",
                    key=f"inventory_alert_action_{alert_index}_{sku}_{scenario}_{warehouse_id}",
                ):
                    st.session_state["requested_navigation_page"] = (
                        "Inventory Actions"
                    )
                    st.session_state["inventory_action_context"] = {
                        "scenario": scenario,
                        "sku": sku,
                        "warehouse_id": warehouse_id,
                        "product_name": product_name,
                        "available_quantity": int(alert_row["available_quantity"]),
                        "total_open_demand": demand["total_open_demand"],
                        "unreserved_demand": demand["unreserved_demand"],
                        "net_shortage": network_shortage,
                        "affected_orders": demand["affected_orders"],
                    }
                    st.rerun()

# ============================================================
# SUPPLY PLANNING / SUPPLY REQUESTS
# ============================================================

def ensure_supply_request_table():
    """Create the lightweight supply-request table if it does not exist.

    This changes only the SQLite schema. It does not create stock or
    operational transactions. It is called only when supply-request
    functionality is used.
    """

    conn = get_connection()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                warehouse_id TEXT NOT NULL,
                quantity_requested INTEGER NOT NULL,
                quantity_ordered INTEGER NOT NULL DEFAULT 0,
                quantity_received INTEGER NOT NULL DEFAULT 0,
                priority TEXT NOT NULL DEFAULT 'Normal',
                status TEXT NOT NULL DEFAULT 'OPEN',
                required_by TEXT,
                expected_arrival_date TEXT,
                reference_id TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_supply_requests_df():
    """Read supply requests without creating or changing the table."""

    conn = get_connection()

    try:
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'supply_requests'
            """
        ).fetchone()

        if exists is None:
            return pd.DataFrame(
                columns=[
                    "request_id",
                    "sku",
                    "warehouse_id",
                    "quantity_requested",
                    "quantity_ordered",
                    "quantity_received",
                    "priority",
                    "status",
                    "required_by",
                    "expected_arrival_date",
                    "reference_id",
                    "notes",
                    "created_at",
                    "updated_at",
                ]
            )

        return pd.read_sql_query(
            """
            SELECT
                request_id,
                sku,
                warehouse_id,
                quantity_requested,
                quantity_ordered,
                quantity_received,
                priority,
                status,
                required_by,
                expected_arrival_date,
                reference_id,
                notes,
                created_at,
                updated_at
            FROM supply_requests
            ORDER BY
                CASE status
                    WHEN 'OPEN' THEN 1
                    WHEN 'ORDERED' THEN 2
                    WHEN 'PARTIALLY RECEIVED' THEN 3
                    WHEN 'RECEIVED' THEN 4
                    WHEN 'FULFILLED' THEN 5
                    WHEN 'CANCELLED' THEN 6
                    ELSE 7
                END,
                CASE priority
                    WHEN 'Critical' THEN 1
                    WHEN 'High' THEN 2
                    ELSE 3
                END,
                created_at DESC
            """,
            conn,
        )
    finally:
        conn.close()


def _get_supply_planning_maps(inventory_df=None):
    """Return live planning plus lookup maps used by Supply Request reconciliation."""
    planning_df = get_supply_planning_df(inventory_df)
    if planning_df.empty:
        return planning_df, {}, {}

    physical_gap_map = {
        str(row["SKU"]): int(row["Physical Supply Gap"])
        for _, row in planning_df.iterrows()
    }
    additional_requirement_map = {
        str(row["SKU"]): int(row["Additional Supply Required"])
        for _, row in planning_df.iterrows()
    }
    return planning_df, physical_gap_map, additional_requirement_map


def fulfill_supply_requests_from_live_inventory(inventory_df=None):
    """Synchronize active Supply Requests with actual live physical coverage.

    An ORDERED request is NOT fulfilled merely because its quantity is treated
    as confirmed incoming in planning. It remains ORDERED until physical
    inventory coverage changes, or the underlying demand disappears. If live
    physical stock makes the SKU's physical supply gap zero, the request is
    moved to FULFILLED. This mutation affects only supply_requests.
    """
    ensure_supply_request_table()
    planning_df, physical_gap_map, _ = _get_supply_planning_maps(inventory_df)

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT request_id, sku, status
            FROM supply_requests
            WHERE status IN ('OPEN', 'ORDERED', 'PARTIALLY RECEIVED')
            """
        ).fetchall()

        fulfilled = []
        now = datetime.now().isoformat(timespec="seconds")

        for request_id, sku, status in rows:
            physical_gap = physical_gap_map.get(str(sku), 0)
            if physical_gap > 0:
                continue

            conn.execute(
                """
                UPDATE supply_requests
                SET
                    status = 'FULFILLED',
                    updated_at = ?,
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN
                            'Fulfilled because live physical inventory no longer shows a supply gap.'
                        ELSE
                            notes || ' | Fulfilled because live physical inventory no longer shows a supply gap.'
                    END
                WHERE request_id = ?
                """,
                (now, int(request_id)),
            )
            fulfilled.append(int(request_id))

        if fulfilled:
            conn.commit()
        return fulfilled
    finally:
        conn.close()


def reconcile_supply_requests_after_core_reset():
    """Reconcile active requests after an Orders/Inventory core reset.

    Historical requests are retained. A request is fulfilled only when the
    restored live physical state no longer has a supply gap for its SKU.
    """
    return fulfill_supply_requests_from_live_inventory()


def synchronize_supply_requests_with_live_state(inventory_df=None):
    """Reconcile active Supply Requests against current live physical state."""
    return fulfill_supply_requests_from_live_inventory(inventory_df)


def get_confirmed_incoming_by_sku_warehouse():
    """Return ordered/partially received incoming supply by SKU and warehouse."""
    requests_df = get_supply_requests_df()
    if requests_df.empty:
        return {}

    active = requests_df[
        requests_df["status"].isin(["ORDERED", "PARTIALLY RECEIVED"])
    ].copy()
    if active.empty:
        return {}

    active["remaining_incoming"] = (
        pd.to_numeric(active["quantity_ordered"], errors="coerce").fillna(0)
        - pd.to_numeric(active["quantity_received"], errors="coerce").fillna(0)
    ).clip(lower=0)

    grouped = active.groupby(["sku", "warehouse_id"])["remaining_incoming"].sum()
    return {
        (str(sku), str(warehouse_id)): int(quantity)
        for (sku, warehouse_id), quantity in grouped.items()
        if int(quantity) > 0
    }


def get_confirmed_incoming_by_sku():
    """Return confirmed incoming supply by SKU. Read-only compatibility helper."""
    incoming = get_confirmed_incoming_by_sku_warehouse()
    totals = {}
    for (sku, _warehouse_id), quantity in incoming.items():
        totals[sku] = totals.get(sku, 0) + quantity
    return totals


def get_supply_planning_df(inventory_df=None):
    """Build SKU-level supply planning from live demand, inventory and incoming supply."""
    if inventory_df is None:
        inventory_df = load_inventory()

    inv = inventory_df.copy() if inventory_df is not None else pd.DataFrame()
    if not inv.empty:
        inv = normalize_inventory_balances(inv)
    if inv.empty:
        inv = pd.DataFrame(columns=["sku", "warehouse_id", "available_quantity"])
    else:
        inv["available_quantity"] = pd.to_numeric(
            inv["available_quantity"], errors="coerce"
        ).fillna(0).astype(int)

    wh01 = inv[inv["warehouse_id"] == "WH01"].groupby("sku")["available_quantity"].sum()
    other = inv[inv["warehouse_id"] != "WH01"].groupby("sku")["available_quantity"].sum()
    network = inv.groupby("sku")["available_quantity"].sum()

    # Supply Planning consumes the same authoritative live allocation engine
    # as Dashboard and Inventory Alerts. This prevents planning quantities from
    # drifting from the shortage shown elsewhere in the application.
    allocation_snapshot = get_inventory_allocation_snapshot()
    allocation_summaries = allocation_snapshot["sku_summary"]

    demand_map = {
        str(sku): int(summary["total_open_demand"])
        for sku, summary in allocation_summaries.items()
    }
    reserved_map = {
        str(sku): int(summary["reserved_quantity"])
        for sku, summary in allocation_summaries.items()
    }
    incoming_map = get_confirmed_incoming_by_sku_warehouse()

    inventory_skus = set(inv["sku"].dropna().astype(str))
    demand_skus = set(demand_map)
    request_skus = set(str(value) for value in get_supply_requests_df().get("sku", pd.Series(dtype=str)).dropna())
    planning_skus = sorted(inventory_skus | demand_skus | request_skus)

    rows = []
    for sku in planning_skus:
        open_demand = demand_map.get(sku, 0)
        reserved = reserved_map.get(sku, 0)
        unreserved_demand = max(open_demand - reserved, 0)
        wh01_available = int(wh01.get(sku, 0))
        other_available = int(other.get(sku, 0))
        network_available = int(network.get(sku, 0))

        wh01_gap = max(unreserved_demand - wh01_available, 0)
        transfer_recommended = min(wh01_gap, other_available)
        physical_supply_gap = max(wh01_gap - transfer_recommended, 0)

        incoming_wh01 = incoming_map.get((sku, "WH01"), 0)
        incoming_other = sum(
            quantity
            for (incoming_sku, warehouse_id), quantity in incoming_map.items()
            if incoming_sku == sku and warehouse_id != "WH01"
        )
        confirmed_incoming = incoming_wh01 + incoming_other
        procurement_requirement = max(physical_supply_gap - confirmed_incoming, 0)

        if open_demand == 0:
            recommended_action = "No Open Demand"
        elif unreserved_demand == 0:
            recommended_action = "Reserve Stock"
        elif transfer_recommended > 0 and procurement_requirement > 0:
            recommended_action = "Transfer + Supply Request"
        elif transfer_recommended > 0:
            recommended_action = "Transfer Stock"
        elif confirmed_incoming > 0:
            recommended_action = "Incoming Supply"
        elif procurement_requirement > 0:
            recommended_action = "Supply Request Required"
        else:
            recommended_action = "Review"

        rows.append(
            {
                "SKU": sku,
                "Open Demand": open_demand,
                "Reserved": reserved,
                "Unreserved Demand": unreserved_demand,
                "WH01 Available": wh01_available,
                "Other Warehouse Available": other_available,
                "Network Available": network_available,
                "Confirmed Incoming": confirmed_incoming,
                "Incoming to WH01": incoming_wh01,
                "Incoming to Other Warehouse": incoming_other,
                "WH01 Gap": wh01_gap,
                "Transfer Recommended": transfer_recommended,
                "Physical Supply Gap": physical_supply_gap,
                "Additional Supply Required": procurement_requirement,
                "Recommended Action": recommended_action,
            }
        )

    return pd.DataFrame(rows)


def create_supply_request(
    sku,
    warehouse_id,
    quantity_requested,
    priority="Normal",
    required_by=None,
    expected_arrival_date=None,
    notes=None,
):
    """Create an OPEN Supply Request. This does not add inventory."""
    quantity_requested = int(quantity_requested)
    if quantity_requested <= 0:
        raise ValueError("Supply request quantity must be greater than zero.")

    ensure_supply_request_table()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM products WHERE sku = ? AND active = 1", (sku,)).fetchone() is None:
            raise ValueError(f"SKU {sku} does not exist or is inactive.")
        if conn.execute("SELECT 1 FROM warehouses WHERE warehouse_id = ? AND active = 1", (warehouse_id,)).fetchone() is None:
            raise ValueError(f"Warehouse {warehouse_id} does not exist or is inactive.")

        active_request = conn.execute(
            """
            SELECT request_id, quantity_requested, quantity_ordered, quantity_received, status
            FROM supply_requests
            WHERE sku = ?
              AND warehouse_id = ?
              AND status IN ('OPEN', 'ORDERED', 'PARTIALLY RECEIVED')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sku, warehouse_id),
        ).fetchone()

        if active_request is not None:
            request_id, requested, ordered, received, status = active_request
            raise ValueError(
                f"An active Supply Request already exists for {sku} in {warehouse_id} "
                f"(Request #{int(request_id)}, status {status}). "
                "Use the existing request instead of creating a duplicate."
            )

        cursor = conn.execute(
            """
            INSERT INTO supply_requests (
                sku, warehouse_id, quantity_requested, quantity_ordered,
                quantity_received, priority, status, required_by,
                expected_arrival_date, reference_id, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, 0, 0, ?, 'OPEN', ?, ?, NULL, ?, ?, ?)
            """,
            (
                sku, warehouse_id, quantity_requested, priority,
                required_by, expected_arrival_date, notes, now, now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_supply_request_ordered(
    request_id,
    quantity_ordered,
    expected_arrival_date=None,
    reference_id=None,
    notes=None,
):
    """Mark a supply request as ORDERED. This does not add inventory."""

    quantity_ordered = int(quantity_ordered)

    if quantity_ordered <= 0:
        raise ValueError("Ordered quantity must be greater than zero.")

    ensure_supply_request_table()

    conn = get_connection()

    try:
        row = conn.execute(
            """
            SELECT
                quantity_requested,
                quantity_received,
                status
            FROM supply_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Supply request was not found.")

        requested, received, status = int(row[0]), int(row[1]), str(row[2])

        if status in ["RECEIVED", "CANCELLED"]:
            raise ValueError(
                f"Supply request cannot be ordered because its status is {status}."
            )

        if quantity_ordered < received:
            raise ValueError(
                "Ordered quantity cannot be less than quantity already received."
            )

        now = datetime.now().isoformat(timespec="seconds")
        new_status = "PARTIALLY RECEIVED" if received > 0 else "ORDERED"

        conn.execute(
            """
            UPDATE supply_requests
            SET
                quantity_ordered = ?,
                status = ?,
                expected_arrival_date = ?,
                reference_id = ?,
                notes = ?,
                updated_at = ?
            WHERE request_id = ?
            """,
            (
                quantity_ordered,
                new_status,
                expected_arrival_date,
                reference_id,
                notes,
                now,
                request_id,
            ),
        )
        conn.commit()

        return {
            "request_id": request_id,
            "quantity_requested": requested,
            "quantity_ordered": quantity_ordered,
            "quantity_received": received,
            "status": new_status,
        }
    finally:
        conn.close()


def record_supply_receipt(request_id, quantity_received):
    """Record physical receipt against a supply request.

    This updates only the supply-request record. Inventory itself must already
    have been updated through Receive Stock.
    """

    quantity_received = int(quantity_received)

    if quantity_received <= 0:
        raise ValueError("Receipt quantity must be greater than zero.")

    ensure_supply_request_table()

    conn = get_connection()

    try:
        row = conn.execute(
            """
            SELECT
                quantity_ordered,
                quantity_received,
                status
            FROM supply_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Supply request was not found.")

        ordered = int(row[0])
        received = int(row[1])
        status = str(row[2])
        remaining = max(ordered - received, 0)

        if status not in ["ORDERED", "PARTIALLY RECEIVED"]:
            raise ValueError(
                "Only ordered or partially received supply requests can receive stock."
            )

        if quantity_received > remaining:
            raise ValueError(
                f"Receipt exceeds remaining confirmed incoming quantity ({remaining})."
            )

        new_received = received + quantity_received
        new_status = (
            "RECEIVED"
            if new_received >= ordered
            else "PARTIALLY RECEIVED"
        )
        now = datetime.now().isoformat(timespec="seconds")

        conn.execute(
            """
            UPDATE supply_requests
            SET
                quantity_received = ?,
                status = ?,
                updated_at = ?
            WHERE request_id = ?
            """,
            (
                new_received,
                new_status,
                now,
                request_id,
            ),
        )
        conn.commit()

        return {
            "request_id": request_id,
            "quantity_received": quantity_received,
            "new_quantity_received": new_received,
            "status": new_status,
        }
    finally:
        conn.close()


def get_supply_request_priority(sku):
    """Return a read-only system-suggested priority for a supply request.

    The suggestion is derived from active order demand for the SKU. The
    highest order priority is considered first, then promised ship dates are
    used to escalate urgency when an order is due today/overdue or due very
    soon. This function does not modify the database.
    """

    conn = get_connection()

    try:
        active_clause, active_params = get_active_order_status_clause()
        demand_df = pd.read_sql_query(
            f"""
            SELECT
                o.order_id,
                o.priority,
                o.promised_ship_by,
                oi.quantity
            FROM order_items oi
            INNER JOIN orders o
                ON o.order_id = oi.order_id
            WHERE oi.sku = ?
              AND o.{active_clause}
            ORDER BY
                CASE o.priority
                    WHEN 'Critical' THEN 1
                    WHEN 'High' THEN 2
                    ELSE 3
                END,
                o.promised_ship_by,
                o.order_id
            """,
            conn,
            params=(sku, *active_params),
        )
    finally:
        conn.close()

    if demand_df.empty:
        return {
            "priority": "Normal",
            "reason": "No active order demand was found for this SKU.",
            "highest_order_priority": "Normal",
            "earliest_ship_by": None,
        }

    priority_rank = {
        "Critical": 3,
        "High": 2,
        "Normal": 1,
    }

    normalized_priorities = (
        demand_df["priority"]
        .fillna("Normal")
        .astype(str)
        .where(
            demand_df["priority"].fillna("Normal").astype(str).isin(
                priority_rank
            ),
            "Normal",
        )
    )

    highest_priority = max(
        normalized_priorities.tolist(),
        key=lambda value: priority_rank[value],
    )

    ship_dates = pd.to_datetime(
        demand_df["promised_ship_by"],
        errors="coerce",
    ).dropna()

    earliest_ship_by = (
        ship_dates.min().date()
        if not ship_dates.empty
        else None
    )

    suggested_priority = highest_priority
    reason = (
        f"Highest active order priority for {sku} is "
        f"{highest_priority}."
    )

    if earliest_ship_by is not None:
        days_to_ship = (earliest_ship_by - datetime.now().date()).days

        if days_to_ship <= 0:
            suggested_priority = "Critical"
            reason = (
                f"An active {highest_priority} demand for {sku} is due "
                f"today or is overdue ({earliest_ship_by})."
            )
        elif days_to_ship <= 1:
            if priority_rank[highest_priority] < priority_rank["High"]:
                suggested_priority = "High"
                reason = (
                    f"The earliest active demand for {sku} is due on "
                    f"{earliest_ship_by}, so Normal demand is escalated to "
                    "High for supply planning."
                )
            else:
                suggested_priority = highest_priority
                reason = (
                    f"The earliest active demand for {sku} is due on "
                    f"{earliest_ship_by} and has {highest_priority} priority."
                )
        else:
            reason = (
                f"Highest active order priority is {highest_priority}; "
                f"earliest promised ship-by is {earliest_ship_by}."
            )

    return {
        "priority": suggested_priority,
        "reason": reason,
        "highest_order_priority": highest_priority,
        "earliest_ship_by": earliest_ship_by,
    }



def _show_supply_planning_overview_page():
    st.title("Supply Planning")

    # A successful request creation triggers a rerun so the planning view
    # refreshes. Keep a one-time confirmation in session state so the
    # employee still sees clear feedback after the rerun.
    supply_request_flash = st.session_state.pop(
        "supply_request_flash",
        None,
    )

    if supply_request_flash:
        st.success(
            f"Supply Request #{supply_request_flash['request_id']} created "
            f"for {supply_request_flash['sku']} — "
            f"{supply_request_flash['quantity']} unit(s) requested. "
            "No inventory was added."
        )

    st.caption(
        "SKU-level planning view combining open demand, live inventory, transfers and confirmed incoming supply. "
        "This page is read-only until you explicitly raise a Supply Request."
    )

    inventory_df = load_inventory()
    synchronize_supply_requests_with_live_state(inventory_df)
    planning_df = get_supply_planning_df(inventory_df)

    if planning_df.empty:
        st.info("No inventory or demand data is available for supply planning.")
        return

    shortage_df = planning_df[
        planning_df["Additional Supply Required"] > 0
    ].copy()

    # Low Stock is a planning/replenishment signal, but it is not a
    # shortage. Include WH01 low-stock SKUs in the planning handoff so a
    # Low Stock alert can open Supply Planning without incorrectly routing
    # the operator to Receive Stock or disappearing from the planning view.
    low_stock_skus = set(
        inventory_df.loc[
            (pd.to_numeric(inventory_df["available_quantity"], errors="coerce").fillna(0) > 0)
            & (pd.to_numeric(inventory_df["available_quantity"], errors="coerce").fillna(0) <= LOW_STOCK_THRESHOLD),
            "sku",
        ]
        .dropna()
        .astype(str)
    )
    low_stock_planning_df = planning_df[
        planning_df["SKU"].astype(str).isin(low_stock_skus)
    ].copy()

    planning_action_skus = set(shortage_df["SKU"].astype(str)) | set(
        low_stock_planning_df["SKU"].astype(str)
    )
    planning_action_df = planning_df[
        planning_df["SKU"].astype(str).isin(planning_action_skus)
    ].copy()

    st.subheader("Supply Position")

    display_df = planning_df.copy()

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
    )

    if planning_action_df.empty:
        st.success("No additional supply is currently required and no Low Stock items need planning review.")
        return

    st.divider()
    st.subheader("Raise Supply Request")

    if not shortage_df.empty and not low_stock_planning_df.empty:
        st.caption(
            "Shortage rows have a calculated additional-supply requirement. "
            "Low Stock rows are shown for replenishment review; Low Stock does not by itself create a calculated shortage quantity."
        )
    elif not shortage_df.empty:
        st.caption(
            "The calculated quantity below is based on the current live network shortage. "
            "Review it before raising the Supply Request."
        )
    else:
        st.caption(
            "Low Stock items are shown for replenishment review. Low Stock does not by itself create a calculated shortage quantity; enter a replenishment quantity only if you decide one is required."
        )

    pending_sku = st.session_state.pop("supply_request_sku", None)
    pending_quantity = st.session_state.pop("supply_request_quantity", None)
    pending_warehouse = st.session_state.pop("supply_request_warehouse", None)
    pending_priority = st.session_state.pop("supply_request_priority", None)

    sku_options = planning_action_df["SKU"].tolist()

    if pending_sku in sku_options:
        st.session_state["supply_request_sku"] = pending_sku
    elif "supply_request_sku" not in st.session_state:
        st.session_state["supply_request_sku"] = sku_options[0]

    selected_sku = st.selectbox(
        "SKU",
        sku_options,
        key="supply_request_sku",
    )

    selected_row = planning_df[
        planning_df["SKU"] == selected_sku
    ].iloc[0]

    calculated_requirement = int(
        selected_row["Additional Supply Required"]
    )
    selected_is_low_stock = selected_sku in low_stock_skus and calculated_requirement <= 0

    quantity_state_sku = st.session_state.get("supply_request_quantity_sku")
    last_calculated_quantity = st.session_state.get(
        "supply_request_last_calculated_quantity"
    )

    if selected_sku != quantity_state_sku:
        if pending_quantity is not None and selected_sku == pending_sku:
            st.session_state["supply_request_quantity"] = max(int(pending_quantity), 1)
        elif calculated_requirement > 0:
            st.session_state["supply_request_quantity"] = calculated_requirement
        else:
            # Low Stock has no automatic replenishment quantity. Keep a safe
            # editable minimum rather than inventing a procurement amount.
            st.session_state["supply_request_quantity"] = 1
        st.session_state["supply_request_quantity_sku"] = selected_sku
        st.session_state["supply_request_last_calculated_quantity"] = calculated_requirement
    elif last_calculated_quantity is not None:
        # Keep a manually edited quantity, but automatically follow live
        # planning when the user has not changed the previous suggestion.
        current_quantity = int(st.session_state.get("supply_request_quantity", 0) or 0)
        if current_quantity == int(last_calculated_quantity):
            st.session_state["supply_request_quantity"] = max(calculated_requirement, 1)
        st.session_state["supply_request_last_calculated_quantity"] = calculated_requirement

    if pending_warehouse:
        st.session_state["supply_request_warehouse"] = pending_warehouse
    elif "supply_request_warehouse" not in st.session_state:
        st.session_state["supply_request_warehouse"] = "WH01"

    priority_analysis = get_supply_request_priority(selected_sku)
    suggested_priority = priority_analysis["priority"]
    suggested_priority_reason = priority_analysis["reason"]

    # The system automatically populates the priority when the SKU changes.
    # Once the employee manually changes the dropdown, that choice is kept.
    # This gives an intelligent default without taking away operational control.
    last_priority_sku = st.session_state.get("supply_request_priority_sku")

    if pending_priority and selected_sku == pending_sku:
        st.session_state["supply_request_priority"] = pending_priority
        st.session_state["supply_request_priority_sku"] = selected_sku
    elif last_priority_sku != selected_sku:
        st.session_state["supply_request_priority"] = suggested_priority
        st.session_state["supply_request_priority_sku"] = selected_sku
    elif "supply_request_priority" not in st.session_state:
        st.session_state["supply_request_priority"] = suggested_priority
        st.session_state["supply_request_priority_sku"] = selected_sku

    if selected_is_low_stock:
        st.info(
            f"{selected_sku} is Low Stock in WH01. No shortage quantity is calculated automatically; "
            "review the live position and enter a replenishment quantity only if required."
        )
    else:
        st.info(
            f"Calculated additional supply required for {selected_sku}: "
            f"{calculated_requirement} unit(s)."
        )

    st.caption(
        f"System-suggested priority: **{suggested_priority}** — "
        f"{suggested_priority_reason}"
    )

    quantity = st.number_input(
        "Quantity Requested",
        min_value=1,
        step=1,
        key="supply_request_quantity",
    )

    warehouse_id = st.selectbox(
        "Receiving Warehouse",
        sorted(inventory_df["warehouse_id"].dropna().unique().tolist()),
        key="supply_request_warehouse",
    )

    priority = st.selectbox(
        "Priority",
        ["Critical", "High", "Normal"],
        key="supply_request_priority",
    )

    if priority != suggested_priority:
        st.caption(
            f"Manual override: the system suggested **{suggested_priority}** "
            "based on current order priority and promised ship-by dates."
        )

    required_by = st.date_input(
        "Required By",
        value=datetime.now().date(),
        key="supply_request_required_by",
    )

    expected_arrival = st.date_input(
        "Expected Arrival Date",
        value=datetime.now().date(),
        key="supply_request_expected_arrival",
    )

    notes = st.text_area(
        "Notes",
        placeholder="Reason for supply request / operational notes",
        key="supply_request_notes",
    )

    if st.button(
        "Raise Supply Request",
        type="primary",
        key="raise_supply_request_button",
    ):
        try:
            request_id = create_supply_request(
                sku=selected_sku,
                warehouse_id=warehouse_id,
                quantity_requested=int(quantity),
                priority=priority,
                required_by=required_by.isoformat(),
                expected_arrival_date=expected_arrival.isoformat(),
                notes=notes or None,
            )

            # Persist the confirmation across the rerun. Without this, the
            # success message is rendered and immediately discarded when
            # st.rerun() refreshes the page, making the button feel
            # unresponsive even though the database insert succeeded.
            st.session_state["supply_request_flash"] = {
                "request_id": request_id,
                "sku": selected_sku,
                "quantity": int(quantity),
            }

            st.rerun()
        except Exception as e:
            st.error(f"Unable to create Supply Request: {e}")


def show_supply_requests_page():
    st.title("Supply Requests")

    st.caption(
        "Track shortage-driven supply requests from request through ordering and physical receipt. "
        "Creating or ordering a request does not add inventory; Receive Stock does that when goods physically arrive."
    )

    synchronize_supply_requests_with_live_state()
    requests_df = get_supply_requests_df()

    if requests_df.empty:
        st.info("No Supply Requests have been raised yet.")
        return

    active_requests = requests_df[
        requests_df["status"].isin(["OPEN", "ORDERED", "PARTIALLY RECEIVED"])
    ].copy()

    st.subheader("Active Supply Request Queue")

    if active_requests.empty:
        st.success("No active Supply Requests. Current live inventory does not require an open supply action.")

        closed_requests = requests_df[
            requests_df["status"].isin(["RECEIVED", "FULFILLED", "CANCELLED"])
        ].copy()

        if not closed_requests.empty:
            st.divider()
            st.subheader("Supply Request History")
            st.dataframe(
                closed_requests,
                use_container_width=True,
                hide_index=True,
            )

        return

    st.dataframe(
        active_requests,
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Update Supply Request")

    request_options = active_requests["request_id"].astype(int).tolist()

    request_id = st.selectbox(
        "Request",
        request_options,
        key="manage_supply_request_id",
        format_func=lambda x: (
            f"#{x} | "
            f"{active_requests.loc[active_requests['request_id'] == x, 'sku'].iloc[0]} | "
            f"{active_requests.loc[active_requests['request_id'] == x, 'status'].iloc[0]}"
        ),
    )

    selected = active_requests[
        active_requests["request_id"] == request_id
    ].iloc[0]

    st.write(
        f"**SKU:** {selected['sku']}  |  "
        f"**Warehouse:** {selected['warehouse_id']}  |  "
        f"**Requested:** {int(selected['quantity_requested'])}  |  "
        f"**Ordered:** {int(selected['quantity_ordered'])}  |  "
        f"**Received:** {int(selected['quantity_received'])}"
    )

    if selected["status"] == "OPEN":
        st.subheader("Mark as Ordered")

        ordered_quantity = st.number_input(
            "Quantity Ordered",
            min_value=1,
            value=max(int(selected["quantity_requested"]), 1),
            step=1,
            key="supply_ordered_quantity",
        )

        expected_arrival = st.date_input(
            "Expected Arrival Date",
            value=(
                pd.to_datetime(selected["expected_arrival_date"], errors="coerce").date()
                if pd.notna(selected["expected_arrival_date"])
                else datetime.now().date()
            ),
            key="supply_order_expected_arrival",
        )

        reference_id = st.text_input(
            "PO / Reference ID",
            value=str(selected["reference_id"] or ""),
            key="supply_order_reference",
        )

        notes = st.text_area(
            "Notes",
            value=str(selected["notes"] or ""),
            key="supply_order_notes",
        )

        if st.button(
            "Mark Supply as Ordered",
            type="primary",
            key="mark_supply_ordered_button",
        ):
            try:
                result = mark_supply_request_ordered(
                    request_id=request_id,
                    quantity_ordered=int(ordered_quantity),
                    expected_arrival_date=expected_arrival.isoformat(),
                    reference_id=reference_id or None,
                    notes=notes or None,
                )
                st.success(
                    f"Supply Request #{request_id} marked ORDERED for "
                    f"{result['quantity_ordered']} unit(s)."
                )
                st.rerun()
            except Exception as e:
                st.error(f"Unable to update Supply Request: {e}")

    elif selected["status"] in ["ORDERED", "PARTIALLY RECEIVED"]:
        remaining = max(
            int(selected["quantity_ordered"]) - int(selected["quantity_received"]),
            0,
        )

        st.info(
            f"Confirmed incoming: {remaining} unit(s). "
            "Use Inventory → Warehouse Operations → Receive Stock when the physical stock arrives."
        )


    closed_requests = requests_df[
        requests_df["status"].isin(["RECEIVED", "FULFILLED", "CANCELLED"])
    ].copy()

    if not closed_requests.empty:
        st.divider()
        st.subheader("Supply Request History")
        st.dataframe(
            closed_requests,
            use_container_width=True,
            hide_index=True,
        )



def show_transfer_queue_page():
    """Warehouse-side transfer queue.

    Transfer requests created from Fulfillment are workflow records only.
    Inventory changes and inventory-history transactions are created only
    when the warehouse employee completes the physical transfer here.
    """
    st.title("Transfer Queue")
    st.caption(
        "Warehouse execution queue for stock transfers requested by Fulfillment. "
        "Requesting a transfer does not change inventory; completing it moves stock and creates the inventory ledger entries."
    )

    transfers_df = load_transfers()

    if transfers_df.empty:
        st.success("There are no transfer requests.")
        return

    active_df = transfers_df[
        transfers_df["transfer_status"].isin(["REQUESTED", "IN_TRANSIT"])
    ].copy()

    completed_df = transfers_df[
        transfers_df["transfer_status"] == "COMPLETED"
    ].copy()

    col1, col2, col3 = st.columns(3)
    col1.metric("Pending / In Transit", len(active_df))
    col2.metric("Requested", int((active_df["transfer_status"] == "REQUESTED").sum()))
    col3.metric("Completed", len(completed_df))

    st.divider()
    st.subheader("Pending Physical Transfers")

    if active_df.empty:
        st.success("No transfer requires warehouse action.")
        return

    # One transfer is selected at a time so the operator has all context
    # before confirming the physical movement.
    active_options = active_df["transfer_id"].astype(int).tolist()
    selected_transfer_id = st.selectbox(
        "Select Transfer",
        active_options,
        key="transfer_queue_selected_id",
        format_func=lambda transfer_id: (
            f"TR-{int(transfer_id):05d} | "
            f"{active_df.loc[active_df['transfer_id'] == transfer_id, 'sku'].iloc[0]} | "
            f"{int(active_df.loc[active_df['transfer_id'] == transfer_id, 'quantity'].iloc[0])} unit(s) | "
            f"{active_df.loc[active_df['transfer_id'] == transfer_id, 'transfer_status'].iloc[0]}"
        ),
    )

    selected = active_df[
        active_df["transfer_id"].astype(int) == int(selected_transfer_id)
    ].iloc[0]

    st.subheader("Transfer Details")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transfer ID", f"TR-{int(selected['transfer_id']):05d}")
    c2.metric("SKU", str(selected["sku"]))
    c3.metric("Quantity", int(selected["quantity"]))
    c4.metric("Status", str(selected["transfer_status"]))

    details_left, details_right = st.columns(2)
    with details_left:
        st.write(f"**Product:** {selected.get('product_name', '')}")
        st.write(f"**From:** {selected.get('from_warehouse', selected.get('from_warehouse_id', ''))}")
        st.write(f"**To:** {selected.get('to_warehouse', selected.get('to_warehouse_id', ''))}")
    with details_right:
        st.write(f"**Reference Order:** {selected.get('reference_order_id') or 'General Transfer'}")
        st.write(f"**Requested At:** {selected.get('requested_at', '')}")
        if pd.notna(selected.get("notes")) and str(selected.get("notes")) not in {"", "nan", "None"}:
            st.write(f"**Request Notes:** {selected['notes']}")

    st.info(
        "Confirm only after the physical stock has actually been moved from the source warehouse and received at the destination warehouse."
    )

    completion_notes = st.text_area(
        "Completion Notes",
        placeholder="Optional: pallet/bin/receiver or handover details",
        key=f"transfer_completion_notes_{int(selected_transfer_id)}",
    )

    if st.button(
        "Complete Physical Transfer",
        type="primary",
        key=f"complete_transfer_{int(selected_transfer_id)}",
    ):
        try:
            result = complete_stock_transfer(
                transfer_id=int(selected_transfer_id),
                notes=completion_notes or None,
            )

            st.success(
                f"Transfer TR-{int(result['transfer_id']):05d} completed. "
                f"{result['quantity']} unit(s) moved from {result['from_warehouse']} "
                f"to {result['to_warehouse']}."
            )
            st.info(
                "Inventory was updated atomically. TRANSFER_OUT and TRANSFER_IN "
                "transactions were recorded in Inventory History. Fulfillment will "
                "recalculate the affected order from the live inventory state."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Unable to complete transfer: {exc}")

    st.divider()
    st.subheader("Transfer History")

    history_view = transfers_df.copy()
    history_view["transfer_id"] = history_view["transfer_id"].apply(
        lambda value: f"TR-{int(value):05d}"
    )
    history_view = history_view.rename(columns={
        "transfer_id": "Transfer ID",
        "sku": "SKU",
        "product_name": "Product",
        "from_warehouse": "From",
        "to_warehouse": "To",
        "quantity": "Quantity",
        "transfer_status": "Status",
        "reference_order_id": "Reference Order",
        "requested_at": "Requested At",
        "completed_at": "Completed At",
        "notes": "Notes",
    })
    history_columns = [
        "Transfer ID", "SKU", "Product", "From", "To", "Quantity",
        "Status", "Reference Order", "Requested At", "Completed At", "Notes"
    ]
    st.dataframe(
        history_view[history_columns],
        use_container_width=True,
        hide_index=True,
    )


def show_inventory_history_page():
    st.title("Inventory History")

    st.caption(
        "Read-only transaction history for inventory receipts, reservations, picks, transfers and adjustments."
    )

    conn = get_connection()

    try:
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'inventory_transactions'
            """
        ).fetchone()

        if exists is None:
            st.info("No inventory transaction history is available yet.")
            return

        history_df = pd.read_sql_query(
            """
            SELECT
                it.transaction_id,
                it.transaction_time,
                it.sku,
                p.product_name,
                it.warehouse_id,
                w.warehouse_name,
                it.transaction_type,
                it.quantity,
                it.reference_id,
                it.notes
            FROM inventory_transactions it
            LEFT JOIN products p
                ON p.sku = it.sku
            LEFT JOIN warehouses w
                ON w.warehouse_id = it.warehouse_id
            ORDER BY it.transaction_time DESC, it.transaction_id DESC
            """,
            conn,
        )
    finally:
        conn.close()

    if history_df.empty:
        st.info("No inventory transactions have been recorded yet.")
        return

    col1, col2, col3 = st.columns(3)

    with col1:
        sku_filter = st.selectbox(
            "SKU",
            ["All"] + sorted(history_df["sku"].dropna().astype(str).unique().tolist()),
            key="history_sku_filter",
        )

    with col2:
        type_filter = st.selectbox(
            "Transaction Type",
            ["All"] + sorted(history_df["transaction_type"].dropna().astype(str).unique().tolist()),
            key="history_type_filter",
        )

    with col3:
        warehouse_filter = st.selectbox(
            "Warehouse",
            ["All"] + sorted(history_df["warehouse_id"].dropna().astype(str).unique().tolist()),
            key="history_warehouse_filter",
        )

    filtered = history_df.copy()

    if sku_filter != "All":
        filtered = filtered[filtered["sku"] == sku_filter]

    if type_filter != "All":
        filtered = filtered[filtered["transaction_type"] == type_filter]

    if warehouse_filter != "All":
        filtered = filtered[filtered["warehouse_id"] == warehouse_filter]

    filtered = filtered.rename(
        columns={
            "transaction_id": "Transaction ID",
            "transaction_time": "Time",
            "sku": "SKU",
            "product_name": "Product",
            "warehouse_id": "Warehouse",
            "warehouse_name": "Warehouse Name",
            "transaction_type": "Change Type",
            "quantity": "Quantity Change",
            "reference_id": "Reference",
            "notes": "Notes",
        }
    )

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# INVENTORY ACTIONS
# ============================================================

def load_product_details(sku):

    conn = get_connection()

    query = """
        SELECT
            sku,
            product_name,
            category,
            brand,
            variant,
            size,
            color,
            unit_price
        FROM products
        WHERE sku = ?
    """

    df = pd.read_sql_query(
        query,
        conn,
        params=(sku,)
    )

    conn.close()

    if df.empty:
        return None

    return df.iloc[0]


def load_order_summary(order_id):

    orders_df = load_orders()

    selected = orders_df[
        orders_df["order_id"] == order_id
    ]

    if selected.empty:
        return None

    return selected.iloc[0]


def load_order_item_options(order_id):

    items_df = load_order_items(order_id)

    if items_df.empty:
        return items_df

    items_df = items_df.copy()

    items_df["display"] = (
        items_df["sku"].astype(str)
        + " | "
        + items_df["product_name"].astype(str)
        + " | Qty "
        + items_df["quantity"].astype(str)
    )

    return items_df


def show_product_inventory_context(
    inventory_df,
    sku,
    warehouse_id,
    title="Current Inventory"
):

    selected_inventory = inventory_df[
        (
            inventory_df["sku"] == sku
        )
        &
        (
            inventory_df["warehouse_id"] == warehouse_id
        )
    ]

    product = load_product_details(sku)

    st.subheader(title)

    if product is not None:

        col1, col2, col3 = st.columns(3)

        col1.write(
            f"**Product:** {product['product_name']}"
        )

        col2.write(
            f"**SKU:** {product['sku']}"
        )

        col3.write(
            f"**Variant:** {product['variant']}"
        )

    if selected_inventory.empty:

        st.warning(
            f"No inventory record found for {sku} in {warehouse_id}."
        )

        return None

    row = selected_inventory.iloc[0]

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "System Quantity",
        int(row["system_quantity"])
    )

    col2.metric(
        "Reserved",
        int(row["reserved_quantity"])
    )

    col3.metric(
        "Available",
        int(row["available_quantity"])
    )

    col4.metric(
        "Bin",
        str(row["bin_location"])
    )

    st.caption(
        f"Warehouse: {row['warehouse_name']} | "
        f"Status: {row['inventory_status']}"
    )

    return row


def show_order_context(
    order_id,
    inventory_df=None,
    title="Selected Order"
):

    order = load_order_summary(order_id)

    if order is None:
        return None

    st.subheader(title)

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Priority",
        order["priority"]
    )

    col2.metric(
        "Order Status",
        order["order_status"]
    )

    col3.metric(
        "Ship By",
        str(order["promised_ship_by"])
    )

    col4.metric(
        "Order Value",
        f"₹{order['order_value']:,.2f}"
    )

    st.write(
        f"**Customer:** {order['customer_name']}  |  "
        f"**Destination:** {order['shipping_city']}, "
        f"{order['shipping_state']} - {order['pincode']}"
    )

    if pd.notna(order["priority_reason"]):
        st.caption(
            f"Priority reason: {order['priority_reason']}"
        )

    items_df = load_order_items(order_id)

    if not items_df.empty:

        st.write("**Order Items**")

        st.dataframe(
            items_df[
                [
                    "sku",
                    "product_name",
                    "variant",
                    "quantity"
                ]
            ],
            use_container_width=True,
            hide_index=True
        )

    if inventory_df is not None and not items_df.empty:

        assessment_rows = []

        for _, item in items_df.iterrows():

            sku = item["sku"]

            main = inventory_df[
                (
                    inventory_df["sku"] == sku
                )
                &
                (
                    inventory_df["warehouse_id"] == "WH01"
                )
            ]

            other = inventory_df[
                (
                    inventory_df["sku"] == sku
                )
                &
                (
                    inventory_df["warehouse_id"] != "WH01"
                )
            ]

            main_available = (
                int(main["available_quantity"].sum())
                if not main.empty else 0
            )

            other_available = (
                int(other["available_quantity"].sum())
                if not other.empty else 0
            )

            assessment_rows.append(
                {
                    "SKU": sku,
                    "Required": int(item["quantity"]),
                    "WH01 Available": main_available,
                    "Other Warehouse Available": other_available,
                }
            )

        if assessment_rows:

            st.write("**Inventory Position**")

            st.dataframe(
                pd.DataFrame(assessment_rows),
                use_container_width=True,
                hide_index=True
            )

    return order


def show_inventory_actions_page(allowed_actions=None):

    st.title(
        "Inventory Actions"
    )

    st.caption(
        "Guided manual actions: the system shows the relevant order, product and inventory information, but nothing changes until you explicitly click the action button."
    )

    st.warning(
        "The action buttons below change the SQLite database. Selecting an order, SKU or warehouse is read-only."
    )

    action_message = st.session_state.pop(
        "inventory_action_message",
        None,
    )

    if action_message:
        st.success(action_message)

    next_supply_context = st.session_state.get(
        "next_supply_context"
    )

    if next_supply_context:
        st.info(
            f"Transfer completed for {next_supply_context['sku']}. "
            f"Next step: Supply Planning for "
            f"{next_supply_context['quantity']} additional unit(s)."
        )

        if st.button(
            "2. Continue to Supply Planning",
            type="primary",
            key="continue_to_supply_planning_after_transfer",
        ):
            st.session_state["requested_navigation_page"] = "Supply Planning"
            st.session_state["supply_request_sku"] = next_supply_context["sku"]
            st.session_state["supply_request_quantity"] = max(
                int(next_supply_context["quantity"]),
                1,
            )
            st.session_state["supply_request_quantity_sku"] = next_supply_context["sku"]
            st.session_state["supply_request_last_calculated_quantity"] = int(
                next_supply_context["quantity"]
            )
            st.session_state["supply_request_warehouse"] = "WH01"
            st.session_state["supply_request_priority"] = next_supply_context.get(
                "priority",
                "Normal",
            )
            st.session_state["supply_request_priority_sku"] = next_supply_context["sku"]
            st.session_state.pop("next_supply_context", None)
            st.rerun()

    if "inventory_action_context" in st.session_state:

        context = st.session_state["inventory_action_context"]

        st.info(
            f"Opened from Inventory Alert: {context.get('scenario', 'Alert')} | "
            f"SKU: {context.get('sku', '')} | "
            f"Warehouse: {context.get('warehouse_id', '')}. "
            "Review the carried information before executing the action."
        )

        if context.get("transfer_quantity", 0) > 0 and context.get("net_shortage", 0) > 0:
            st.warning(
                f"Workflow: transfer {context.get('transfer_quantity', 0)} unit(s) first, "
                f"then use Supply Planning for the remaining "
                f"{context.get('net_shortage', 0)} unit(s) of network shortage."
            )
        elif context.get("net_shortage", 0) > 0:
            st.warning(
                f"Calculated network shortage: {context.get('net_shortage', 0)} unit(s) "
                f"across {len(context.get('affected_orders', []))} active order(s)."
            )

        if st.button("Clear Alert Context", key="clear_inventory_action_context"):
            st.session_state.pop("inventory_action_context", None)
            st.rerun()

    # Apply a requested workflow handoff BEFORE the action selectbox is
    # instantiated. Streamlit does not allow changing a widget's session
    # state after that widget has already been created in the current run.
    requested_inventory_action = st.session_state.pop(
        "requested_inventory_action",
        None,
    )

    if requested_inventory_action:
        st.session_state["inventory_action_type"] = requested_inventory_action

    available_actions = (
        allowed_actions
        if allowed_actions is not None
        else [
            "Receive Stock",
            "Stock Count",
            "Transfer Stock",
            "Reserve Stock",
            "Pick Stock",
        ]
    )

    if not available_actions:
        st.info("No warehouse operations are configured.")
        return

    if st.session_state.get("inventory_action_type") not in available_actions:
        st.session_state["inventory_action_type"] = available_actions[0]

    action = st.selectbox(
        "Warehouse Operation",
        available_actions,
        key="inventory_action_type",
    )

    st.divider()

    inventory_df = load_inventory()

    if inventory_df.empty:
        st.error(
            "No inventory records are available."
        )
        return

    sku_options = sorted(
        inventory_df["sku"]
        .dropna()
        .unique()
        .tolist()
    )

    warehouse_options = sorted(
        inventory_df["warehouse_id"]
        .dropna()
        .unique()
        .tolist()
    )

    # --------------------------------------------------------
    # RECEIVE STOCK
    # --------------------------------------------------------

    if action == "Receive Stock":

        st.subheader("Receive Stock")

        st.info(
            "Use this when new physical stock has physically arrived at the warehouse. "
            "Receive Stock adds the received quantity to live inventory and records a receipt transaction. "
            "Do not use this for an inventory CSV snapshot or stock-count reconciliation."
        )

        col1, col2 = st.columns(2)

        with col1:

            sku = st.selectbox(
                "Product / SKU",
                sku_options,
                key="receive_sku",
                format_func=lambda x: (
                    f"{x} | "
                    f"{load_product_details(x)['product_name']}"
                    if load_product_details(x) is not None
                    else x
                ),
            )

        with col2:

            warehouse_id = st.selectbox(
                "Warehouse",
                warehouse_options,
                key="receive_warehouse",
            )

        show_product_inventory_context(
            inventory_df,
            sku,
            warehouse_id,
            "Current Stock Before Receipt"
        )

        demand_context = get_sku_demand_summary(sku)

        # ----------------------------------------------------
        # Optional Supply Request linkage
        # ----------------------------------------------------
        supply_requests_df = get_supply_requests_df()
        matching_requests = pd.DataFrame()

        if not supply_requests_df.empty:
            matching_requests = supply_requests_df[
                (supply_requests_df["sku"] == sku)
                & (supply_requests_df["warehouse_id"] == warehouse_id)
                & (supply_requests_df["status"].isin(["ORDERED", "PARTIALLY RECEIVED"]))
            ].copy()

            if not matching_requests.empty:
                matching_requests["remaining"] = (
                    pd.to_numeric(matching_requests["quantity_ordered"], errors="coerce").fillna(0)
                    - pd.to_numeric(matching_requests["quantity_received"], errors="coerce").fillna(0)
                ).clip(lower=0)
                matching_requests = matching_requests[
                    matching_requests["remaining"] > 0
                ]

        request_options = ["No Supply Request"]

        if not matching_requests.empty:
            request_options += matching_requests["request_id"].astype(int).tolist()

        selected_request = st.selectbox(
            "Supply Request (optional)",
            request_options,
            key="receive_supply_request",
            format_func=lambda value: (
                value
                if value == "No Supply Request"
                else (
                    f"Request #{value} | Expected "
                    f"{int(matching_requests.loc[matching_requests['request_id'] == value, 'quantity_ordered'].iloc[0])} | "
                    f"Received "
                    f"{int(matching_requests.loc[matching_requests['request_id'] == value, 'quantity_received'].iloc[0])} | "
                    f"Remaining "
                    f"{int(matching_requests.loc[matching_requests['request_id'] == value, 'remaining'].iloc[0])}"
                )
            ),
        )

        selected_request_remaining = None

        if selected_request != "No Supply Request":
            selected_request_remaining = int(
                matching_requests.loc[
                    matching_requests["request_id"] == selected_request,
                    "remaining",
                ].iloc[0]
            )
            st.info(
                f"Supply Request #{selected_request}: "
                f"{selected_request_remaining} unit(s) remain confirmed incoming. "
                "The receipt quantity cannot exceed this remaining amount."
            )

        if demand_context["net_shortage"] > 0:
            st.warning(
                f"Current unreserved demand for {sku}: "
                f"{demand_context['unreserved_demand']} unit(s). "
                f"Net shortage across all active orders: "
                f"{demand_context['net_shortage']} unit(s). "
                "The suggested receipt quantity covers the current shortage; "
                "you may enter a larger quantity if more stock has actually been received."
            )

            if demand_context["affected_orders"]:
                st.caption(
                    "Affected orders: "
                    + ", ".join(demand_context["affected_orders"])
                )

        st.divider()

        col1, col2 = st.columns(2)

        with col1:

            quantity = st.number_input(
                "Quantity Received",
                min_value=1,
                step=1,
                value=1,
                key="receive_quantity",
            )

        with col2:

            reference_id = st.text_input(
                "Reference ID",
                placeholder="GRN / PO / receipt number",
                key="receive_reference",
            )

        notes = st.text_area(
            "Notes",
            placeholder="Optional receiving notes",
            key="receive_notes",
        )

        if st.button(
            "Receive Stock",
            type="primary",
            key="receive_stock_button",
        ):

            try:

                if selected_request != "No Supply Request" and selected_request_remaining is not None:
                    if int(quantity) > selected_request_remaining:
                        raise ValueError(
                            f"Receipt quantity cannot exceed the remaining confirmed incoming quantity "
                            f"({selected_request_remaining})."
                        )

                if selected_request != "No Supply Request":
                    result = receive_stock_for_supply_request(
                        request_id=int(selected_request),
                        sku=sku,
                        warehouse_id=warehouse_id,
                        quantity=int(quantity),
                        reference_id=reference_id or None,
                        notes=notes or None,
                    )
                else:
                    result = receive_stock(
                        sku=sku,
                        warehouse_id=warehouse_id,
                        quantity=int(quantity),
                        reference_id=reference_id or None,
                        notes=notes or None,
                    )

                refresh_inventory_alert_state(
                    sku=result["sku"],
                    warehouse_ids=[result["warehouse_id"]],
                    action_type="Receive Stock",
                )
                fulfilled_request_ids = synchronize_supply_requests_with_live_state()

                st.session_state["inventory_action_message"] = (
                    f"Received {result['quantity_received']} unit(s) of "
                    f"{result['sku']} into {result['warehouse_id']}."
                    + (
                        f" Supply Request #{selected_request} was updated."
                        if selected_request != "No Supply Request"
                        else ""
                    )
                    + (
                        f" Supply Request(s) {fulfilled_request_ids} are now fulfilled because live inventory no longer requires additional supply."
                        if fulfilled_request_ids
                        else ""
                    )
                )
                st.rerun()

                st.success(
                    f"Received {result['quantity_received']} unit(s) of "
                    f"{result['sku']} into {result['warehouse_id']}."
                )

                col1, col2 = st.columns(2)

                col1.metric(
                    "New System Quantity",
                    result["new_system_quantity"],
                )

                col2.metric(
                    "New Available Quantity",
                    result["new_available_quantity"],
                )

            except Exception as e:

                st.error(
                    f"Receive stock failed: {e}"
                )

    # --------------------------------------------------------
    # STOCK COUNT
    # --------------------------------------------------------

    elif action == "Stock Count":

        st.subheader("Stock Count")

        st.info(
            "Use this after a physical count. The current system quantity, reserved quantity and bin are shown before you enter the physical count."
        )

        col1, col2 = st.columns(2)

        with col1:

            sku = st.selectbox(
                "Product / SKU",
                sku_options,
                key="count_sku",
                format_func=lambda x: (
                    f"{x} | "
                    f"{load_product_details(x)['product_name']}"
                    if load_product_details(x) is not None
                    else x
                ),
            )

        with col2:

            warehouse_id = st.selectbox(
                "Warehouse",
                warehouse_options,
                key="count_warehouse",
            )

        current_row = show_product_inventory_context(
            inventory_df,
            sku,
            warehouse_id,
            "Current System Position"
        )

        current_system_quantity = (
            int(current_row["system_quantity"])
            if current_row is not None
            else 0
        )

        st.divider()

        col1, col2 = st.columns(2)

        with col1:

            physical_quantity = st.number_input(
                "Physical Quantity Counted",
                min_value=0,
                step=1,
                value=current_system_quantity,
                key="count_quantity",
            )

        with col2:

            reference_id = st.text_input(
                "Reference ID",
                placeholder="Count sheet / audit reference",
                key="count_reference",
            )

        difference_preview = (
            int(physical_quantity)
            - current_system_quantity
        )

        st.metric(
            "Expected Adjustment",
            f"{difference_preview:+d} units"
        )

        notes = st.text_area(
            "Notes",
            placeholder="Optional stock count notes",
            key="count_notes",
        )

        if st.button(
            "Apply Stock Count",
            type="primary",
            key="stock_count_button",
        ):

            try:

                result = stock_count(
                    sku=sku,
                    warehouse_id=warehouse_id,
                    physical_quantity=int(physical_quantity),
                    reference_id=reference_id or None,
                    notes=notes or None,
                )

                st.success(
                    f"Stock count completed for {result['sku']} "
                    f"in {result['warehouse_id']}."
                )

                col1, col2, col3 = st.columns(3)

                col1.metric(
                    "Previous System Quantity",
                    result["previous_system_quantity"],
                )

                col2.metric(
                    "Physical Quantity",
                    result["physical_quantity"],
                )

                col3.metric(
                    "Difference",
                    result["difference"],
                )

                refresh_inventory_alert_state(
                    sku=result["sku"],
                    warehouse_ids=[result["warehouse_id"]],
                    action_type="Stock Count",
                )
                synchronize_supply_requests_with_live_state()

                st.session_state["inventory_action_message"] = (
                    f"Stock count completed for {result['sku']} "
                    f"in {result['warehouse_id']}."
                )
                st.rerun()

                st.info(
                    f"New available quantity: {result['new_available_quantity']}"
                )

            except Exception as e:

                st.error(
                    f"Stock count failed: {e}"
                )

    # --------------------------------------------------------
    # TRANSFER STOCK
    # --------------------------------------------------------

    elif action == "Transfer Stock":

        st.subheader("Transfer Stock")

        st.info(
            "Select an order when the transfer is order-specific. The order and inventory position are displayed for reference; the transfer occurs only after you click Transfer Stock."
        )

        order_options = ["No specific order"] + load_orders()["order_id"].dropna().tolist()

        reference_selection = st.selectbox(
            "Reference Order",
            order_options,
            key="transfer_reference_order",
        )

        reference_order_id = (
            None
            if reference_selection == "No specific order"
            else reference_selection
        )

        if reference_order_id:

            show_order_context(
                reference_order_id,
                inventory_df,
                "Transfer Reference"
            )

        st.divider()

        if reference_order_id:

            reference_items = load_order_item_options(
                reference_order_id
            )

            if not reference_items.empty:

                selected_item_display = st.selectbox(
                    "Order Item / SKU",
                    reference_items["display"].tolist(),
                    key="transfer_order_item",
                )

                selected_item = reference_items[
                    reference_items["display"] == selected_item_display
                ].iloc[0]

                sku = selected_item["sku"]

            else:

                sku = st.selectbox(
                    "Product / SKU",
                    sku_options,
                    key="transfer_sku_general",
                )

        else:

            sku = st.selectbox(
                "Product / SKU",
                sku_options,
                key="transfer_sku_general",
                format_func=lambda x: (
                    f"{x} | "
                    f"{load_product_details(x)['product_name']}"
                    if load_product_details(x) is not None
                    else x
                ),
            )

        col1, col2 = st.columns(2)

        with col1:

            from_warehouse_id = st.selectbox(
                "From Warehouse",
                warehouse_options,
                key="transfer_from",
            )

        with col2:

            to_warehouse_id = st.selectbox(
                "To Warehouse",
                warehouse_options,
                key="transfer_to",
            )

        source_row = show_product_inventory_context(
            inventory_df,
            sku,
            from_warehouse_id,
            "Source Warehouse Stock"
        )

        source_available = (
            int(source_row["available_quantity"])
            if source_row is not None
            else 0
        )

        if from_warehouse_id == to_warehouse_id:
            st.warning(
                "Source and destination warehouses must be different."
            )

        demand_context = get_sku_demand_summary(sku)

        if demand_context["destination_gap"] > 0:
            st.warning(
                f"Main warehouse gap for {sku}: "
                f"{demand_context['destination_gap']} unit(s). "
                f"Main warehouse currently has "
                f"{demand_context['destination_available']} available "
                f"against {demand_context['unreserved_demand']} unit(s) of "
                "unreserved demand. The suggested transfer quantity is "
                "capped by the selected source warehouse stock."
            )

        if demand_context["net_shortage"] > 0:
            st.info(
                f"Network-wide shortage after using available stock: "
                f"{demand_context['net_shortage']} unit(s)."
            )

        if demand_context["affected_orders"]:
            st.caption(
                "Affected orders: "
                + ", ".join(demand_context["affected_orders"])
            )

        max_transfer = max(source_available, 1)

        default_transfer_quantity = (
            min(
                source_available,
                max(demand_context["destination_gap"], 1),
            )
            if source_available > 0
            else 1
        )

        quantity = st.number_input(
            "Transfer Quantity",
            min_value=1,
            max_value=max_transfer,
            step=1,
            value=default_transfer_quantity,
            key="transfer_quantity",
        )

        if source_available == 0:
            st.error(
                "No available stock exists in the selected source warehouse. The transfer cannot be completed until stock is available."
            )

        notes = st.text_area(
            "Notes",
            placeholder="Optional transfer notes",
            key="transfer_notes",
        )

        if st.button(
            "Transfer Stock",
            type="primary",
            key="transfer_stock_button",
            disabled=(source_available <= 0 or from_warehouse_id == to_warehouse_id),
        ):

            try:

                result = transfer_stock(
                    sku=sku,
                    from_warehouse_id=from_warehouse_id,
                    to_warehouse_id=to_warehouse_id,
                    quantity=int(quantity),
                    reference_order_id=reference_order_id,
                    notes=notes or None,
                )

                st.success(
                    f"Transfer completed: {result['quantity']} unit(s) "
                    f"of {result['sku']} moved from "
                    f"{result['from_warehouse']} to "
                    f"{result['to_warehouse']}."
                )

                refresh_inventory_alert_state(
                    sku=result["sku"],
                    warehouse_ids=[
                        from_warehouse_id,
                        to_warehouse_id,
                    ],
                    action_type="Transfer Stock",
                    transfer_destination_warehouse_id=to_warehouse_id,
                )
                synchronize_supply_requests_with_live_state()

                # Recalculate the live state AFTER the transfer. If the
                # network is still short, the transfer is only step 1 and
                # Supply Planning becomes the explicit next step.
                post_transfer_planning = get_supply_planning_df(load_inventory())
                post_transfer_row = post_transfer_planning[
                    post_transfer_planning["SKU"] == result["sku"]
                ]

                additional_supply = 0
                if not post_transfer_row.empty:
                    additional_supply = int(
                        post_transfer_row.iloc[0]["Additional Supply Required"]
                    )

                if additional_supply > 0:
                    priority_analysis = get_supply_request_priority(result["sku"])
                    st.session_state["next_supply_context"] = {
                        "sku": result["sku"],
                        "quantity": additional_supply,
                        "priority": priority_analysis["priority"],
                    }
                    st.session_state["inventory_action_message"] = (
                        f"Transfer completed: {result['quantity']} unit(s) "
                        f"of {result['sku']} moved from "
                        f"{result['from_warehouse']} to "
                        f"{result['to_warehouse']}. "
                        f"Additional supply still required: {additional_supply} unit(s)."
                    )
                else:
                    st.session_state["inventory_action_message"] = (
                        f"Transfer completed: {result['quantity']} unit(s) "
                        f"of {result['sku']} moved from "
                        f"{result['from_warehouse']} to "
                        f"{result['to_warehouse']}. "
                        "No additional supply is currently required."
                    )

                # Reload from SQLite so the Inventory page and action state
                # reflect the completed transfer. The database mutation has
                # already succeeded at this point.
                st.rerun()

            except Exception as e:

                st.error(
                    f"Stock transfer failed: {e}"
                )

    # --------------------------------------------------------
    # RESERVE STOCK
    # --------------------------------------------------------

    elif action == "Reserve Stock":

        st.subheader("Reserve Stock")

        st.info(
            "Select the order first. The application then shows its items and inventory position so the employee does not have to remember the SKU or required quantity."
        )

        order_options = get_active_orders_df(load_orders())["order_id"].dropna().tolist()

        if not order_options:
            st.warning("No orders are currently eligible for reservation.")
            return

        order_id = st.selectbox(
            "Order",
            order_options,
            key="reserve_order",
        )

        show_order_context(
            order_id,
            inventory_df,
            "Selected Order"
        )

        items_df = load_order_item_options(order_id)

        if items_df.empty:
            st.warning("No order items were found for this order.")
            return

        selected_item_display = st.selectbox(
            "Order Item / SKU",
            items_df["display"].tolist(),
            key="reserve_order_item",
        )

        selected_item = items_df[
            items_df["display"] == selected_item_display
        ].iloc[0]

        sku = selected_item["sku"]
        required_quantity = int(selected_item["quantity"])

        # Customer reservations are always made in the Main Fulfillment Warehouse.
        st.session_state["reserve_warehouse"] = "WH01"
        warehouse_id = st.selectbox(
            "Warehouse",
            ["WH01"],
            key="reserve_warehouse",
        )

        current_row = show_product_inventory_context(
            inventory_df,
            sku,
            warehouse_id,
            "Selected SKU Inventory"
        )

        available_quantity = (
            int(current_row["available_quantity"])
            if current_row is not None
            else 0
        )

        st.write(
            f"**Order requirement:** {required_quantity} unit(s)  |  "
            f"**Currently available:** {available_quantity} unit(s)"
        )

        if available_quantity < required_quantity:
            st.warning(
                "There is not enough available stock in this warehouse to reserve the full order requirement."
            )

        # ----------------------------------------------------
        # SKU-level reservation allocation preview
        # ----------------------------------------------------

        allocation_plan = get_sku_reservation_allocation(
            sku=sku,
            warehouse_id=warehouse_id,
        )

        allocation_df = allocation_plan["allocation_df"]

        if not allocation_df.empty:
            st.divider()
            st.subheader("Reservation Allocation")
            st.caption(
                "Read-only allocation view. Orders are considered by priority "
                "(Critical, High, Normal), then promised ship date. No stock is "
                "reserved by this preview."
            )

            display_allocation = allocation_df[[
                "order_id",
                "priority",
                "requested_quantity",
                "already_reserved",
                "allocated_now",
                "remaining_short",
                "allocation_status",
            ]].copy()

            display_allocation = display_allocation.rename(
                columns={
                    "order_id": "Order",
                    "priority": "Priority",
                    "requested_quantity": "Requested",
                    "already_reserved": "Already Reserved",
                    "allocated_now": "Can Allocate Now",
                    "remaining_short": "Remaining Short",
                    "allocation_status": "Allocation Status",
                }
            )

            st.dataframe(
                display_allocation,
                use_container_width=True,
                hide_index=True,
            )

            metric_col1, metric_col2, metric_col3 = st.columns(3)

            metric_col1.metric(
                "Available in Warehouse",
                allocation_plan["available_quantity"],
            )

            metric_col2.metric(
                "Can Allocate Now",
                allocation_plan["total_allocated_now"],
            )

            metric_col3.metric(
                "Remaining Unfulfilled",
                allocation_plan["total_unfulfilled"],
            )

        max_reservation = max(
            1,
            min(required_quantity, available_quantity)
        )

        quantity = st.number_input(
            "Reservation Quantity",
            min_value=1,
            max_value=max_reservation,
            step=1,
            value=1,
            key="reserve_quantity",
        )

        notes = st.text_area(
            "Notes",
            placeholder="Optional reservation notes",
            key="reserve_notes",
        )

        if st.button(
            "Reserve Stock",
            type="primary",
            key="reserve_stock_button",
        ):

            try:

                result = reserve_stock(
                    sku=sku,
                    warehouse_id=warehouse_id,
                    quantity=int(quantity),
                    order_id=order_id,
                    notes=notes or None,
                )

                st.success(
                    f"Reserved {result['quantity_reserved']} unit(s) of "
                    f"{result['sku']} for order {result['order_id']}."
                )

                refresh_inventory_alert_state(
                    sku=result["sku"],
                    warehouse_ids=[result["warehouse_id"]],
                )
                synchronize_supply_requests_with_live_state()

                st.session_state["inventory_action_message"] = (
                    f"Reserved {result['quantity_reserved']} unit(s) of "
                    f"{result['sku']} for order {result['order_id']}. "
                    "Pick Stock has been opened for the same order."
                )

                # Carry the completed reservation directly into the next
                # operational step. The employee does not need to remember
                # the order, SKU or warehouse.
                st.session_state["requested_inventory_action"] = "Pick Stock"
                st.session_state["pick_order"] = result["order_id"]
                st.session_state["pick_order_item"] = selected_item_display
                st.session_state["pick_warehouse"] = result["warehouse_id"]
                st.session_state["pick_quantity"] = int(
                    result["quantity_reserved"]
                )
                st.session_state["requested_navigation_page"] = (
                    "Inventory Actions"
                )

                st.rerun()

                col1, col2 = st.columns(2)

                col1.metric(
                    "New Reserved Quantity",
                    result["new_reserved_quantity"],
                )

                col2.metric(
                    "New Available Quantity",
                    result["new_available_quantity"],
                )

            except Exception as e:

                st.error(
                    f"Stock reservation failed: {e}"
                )

    # --------------------------------------------------------
    # PICK STOCK
    # --------------------------------------------------------

    elif action == "Pick Stock":

        st.subheader("Pick Stock")

        st.info(
            "Select the order first. The application shows the order items, reservation context and warehouse stock before the physical pick is recorded."
        )

        if st.session_state.get("pick_order"):
            st.success(
                f"Ready for picking: order {st.session_state['pick_order']}. "
                "The previous reservation has been carried forward automatically."
            )

        order_options = get_active_orders_df(load_orders())["order_id"].dropna().tolist()

        if not order_options:
            st.warning("No orders are currently eligible for reservation.")
            return

        order_id = st.selectbox(
            "Order",
            order_options,
            key="pick_order",
        )

        show_order_context(
            order_id,
            inventory_df,
            "Selected Order"
        )

        items_df = load_order_item_options(order_id)

        if items_df.empty:
            st.warning("No order items were found for this order.")
            return

        selected_item_display = st.selectbox(
            "Order Item / SKU",
            items_df["display"].tolist(),
            key="pick_order_item",
        )

        selected_item = items_df[
            items_df["display"] == selected_item_display
        ].iloc[0]

        sku = selected_item["sku"]
        required_quantity = int(selected_item["quantity"])

        # Customer picks are always performed in the Main Fulfillment Warehouse.
        st.session_state["pick_warehouse"] = "WH01"
        warehouse_id = st.selectbox(
            "Warehouse",
            ["WH01"],
            key="pick_warehouse",
        )

        current_row = show_product_inventory_context(
            inventory_df,
            sku,
            warehouse_id,
            "Selected SKU Inventory"
        )

        reserved_quantity = (
            int(current_row["reserved_quantity"])
            if current_row is not None
            else 0
        )

        st.write(
            f"**Order requirement:** {required_quantity} unit(s)  |  "
            f"**Warehouse reserved:** {reserved_quantity} unit(s)"
        )

        if reserved_quantity == 0:
            st.warning(
                "No reserved stock is currently visible for this SKU in the selected warehouse. Reserve stock before picking."
            )

        max_pick = max(
            1,
            min(required_quantity, reserved_quantity)
        )

        quantity = st.number_input(
            "Pick Quantity",
            min_value=1,
            max_value=max_pick,
            step=1,
            value=1,
            key="pick_quantity",
        )

        notes = st.text_area(
            "Notes",
            placeholder="Optional picking notes",
            key="pick_notes",
        )

        if st.button(
            "Pick Stock",
            type="primary",
            key="pick_stock_button",
        ):

            try:

                result = pick_stock(
                    sku=sku,
                    warehouse_id=warehouse_id,
                    quantity=int(quantity),
                    order_id=order_id,
                    notes=notes or None,
                )

                st.success(
                    f"Picked {result['quantity_picked']} unit(s) of "
                    f"{result['sku']} for order {result['order_id']}."
                )

                refresh_inventory_alert_state(
                    sku=result["sku"],
                    warehouse_ids=[result["warehouse_id"]],
                )

                st.session_state["inventory_action_message"] = (
                    f"Picked {result['quantity_picked']} unit(s) of "
                    f"{result['sku']} for order {result['order_id']}. "
                    "The same order remains selected for the next picking step."
                )

                # Keep the workflow anchored to the same order so the employee
                # never has to search for or remember the order ID again.
                st.session_state["requested_inventory_action"] = "Pick Stock"
                st.session_state["pick_order"] = result["order_id"]
                st.session_state["pick_warehouse"] = result["warehouse_id"]
                st.session_state["pick_order_item"] = selected_item_display
                st.session_state["requested_navigation_page"] = (
                    "Inventory Actions"
                )

                st.rerun()

                col1, col2, col3 = st.columns(3)

                col1.metric(
                    "New System Quantity",
                    result["new_system_quantity"],
                )

                col2.metric(
                    "New Reserved Quantity",
                    result["new_reserved_quantity"],
                )

                col3.metric(
                    "New Available Quantity",
                    result["new_available_quantity"],
                )

            except Exception as e:

                st.error(
                    f"Stock picking failed: {e}"
                )


# ============================================================
# FULFILLMENT OPERATIONS
# ============================================================

def show_fulfillment_operations_page():
    """Live fulfillment control tower for order execution.

    The page is intentionally order-centric rather than operation-centric.
    The operator selects an order, sees every line item and its live stock
    position, and is presented with the next valid action calculated by the
    shared fulfillment backend.
    """

    st.title("Fulfillment Operations")
    st.caption(
        "Order-centric execution: inspect the complete order, resolve inventory first, "
        "then move the order through Pick → Pack → Stage → Ship."
    )

    orders_df = load_orders()
    if orders_df.empty:
        st.warning("No orders are available.")
        return

    active_df = get_fulfillment_active_orders_df(orders_df).copy()

    # --------------------------------------------------------
    # LIVE ACTION QUEUE
    # --------------------------------------------------------
    queue_rows = []
    for _, row in active_df.iterrows():
        order_id = str(row["order_id"])
        try:
            action = get_next_fulfillment_action(order_id)
        except Exception:
            action = "ERROR"
        queue_rows.append({
            "order_id": order_id,
            "priority": row.get("priority", ""),
            "promised_ship_by": row.get("promised_ship_by", ""),
            "order_status": row.get("order_status", ""),
            "customer_name": row.get("customer_name", ""),
            "next_action": action,
        })

    queue_df = pd.DataFrame(queue_rows)

    action_labels = {
        "TRANSFER": "TRANSFER REQUIRED",
        "TRANSFER_PENDING": "TRANSFER IN PROGRESS",
        "SHORTAGE": "SHORTAGE",
        "RESERVE": "RESERVE STOCK",
        "PICK": "PICK STOCK",
        "MARK_PICKED": "MARK ORDER PICKED",
        "PACK": "PACK",
        "STAGE": "STAGE",
        "SHIP": "SHIP",
        "COMPLETED": "COMPLETED",
        "ERROR": "REVIEW",
    }

    action_rank = {
        "TRANSFER": 1,
        "TRANSFER_PENDING": 2,
        "SHORTAGE": 3,
        "RESERVE": 4,
        "PICK": 5,
        "MARK_PICKED": 6,
        "PACK": 7,
        "STAGE": 8,
        "SHIP": 9,
        "COMPLETED": 10,
        "ERROR": 11,
    }
    queue_df["_rank"] = queue_df["next_action"].map(action_rank).fillna(99)
    queue_df = queue_df.sort_values(
        ["_rank", "priority", "promised_ship_by", "order_id"],
        kind="stable",
    ).reset_index(drop=True)

    metrics = {
        "TRANSFER": int((queue_df["next_action"] == "TRANSFER").sum()),
        "TRANSFER_PENDING": int((queue_df["next_action"] == "TRANSFER_PENDING").sum()),
        "SHORTAGE": int((queue_df["next_action"] == "SHORTAGE").sum()),
        "RESERVE": int((queue_df["next_action"] == "RESERVE").sum()),
        "PICK": int((queue_df["next_action"] == "PICK").sum()),
        "MARK_PICKED": int((queue_df["next_action"] == "MARK_PICKED").sum()),
        "PACK": int((queue_df["next_action"] == "PACK").sum()),
        "STAGE": int((queue_df["next_action"] == "STAGE").sum()),
        "SHIP": int((queue_df["next_action"] == "SHIP").sum()),
    }

    st.subheader("Live Fulfillment Queue")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Transfer Required", metrics["TRANSFER"])
    c2.metric("Shortage", metrics["SHORTAGE"])
    c3.metric("Reserve", metrics["RESERVE"])
    c4.metric("Pick", metrics["PICK"])
    c5.metric("Downstream", metrics["PACK"] + metrics["STAGE"] + metrics["SHIP"])

    if queue_df.empty:
        st.success("There are no active fulfillment orders.")
        return

    queue_filter = st.multiselect(
        "Show orders requiring",
        options=[
            "TRANSFER",
            "TRANSFER_PENDING",
            "SHORTAGE",
            "RESERVE",
            "PICK",
            "PACK",
            "STAGE",
            "SHIP",
        ],
        default=[
            "TRANSFER",
            "TRANSFER_PENDING",
            "SHORTAGE",
            "RESERVE",
            "PICK",
            "PACK",
            "STAGE",
            "SHIP",
        ],
        key="fulfillment_queue_filter",
        format_func=lambda x: action_labels.get(x, x),
    )

    visible_queue = queue_df[
        queue_df["next_action"].isin(queue_filter)
    ].copy()

    display_queue = visible_queue[
        [
            "order_id",
            "priority",
            "promised_ship_by",
            "order_status",
            "customer_name",
            "next_action",
        ]
    ].rename(columns={
        "order_id": "Order ID",
        "priority": "Priority",
        "promised_ship_by": "Ship By",
        "order_status": "Status",
        "customer_name": "Customer",
        "next_action": "Next Action",
    })
    display_queue["Next Action"] = display_queue["Next Action"].map(
        lambda value: action_labels.get(value, value)
    )

    st.dataframe(
        display_queue,
        use_container_width=True,
        hide_index=True,
    )

    st.divider()

    # --------------------------------------------------------
    # ORDER SELECTION
    # --------------------------------------------------------
    default_order = (
        visible_queue.iloc[0]["order_id"]
        if not visible_queue.empty
        else queue_df.iloc[0]["order_id"]
    )
    order_options = visible_queue["order_id"].tolist()
    if not order_options:
        order_options = queue_df["order_id"].tolist()

    selected_order_id = st.selectbox(
        "Select Order to Work",
        order_options,
        index=(
            order_options.index(default_order)
            if default_order in order_options else 0
        ),
        key="fulfillment_control_order",
    )

    selected_order = orders_df[
        orders_df["order_id"].astype(str) == str(selected_order_id)
    ]
    if selected_order.empty:
        st.error("The selected order could not be loaded.")
        return

    order = selected_order.iloc[0]

    try:
        next_action = get_next_fulfillment_action(str(selected_order_id))
        stock_result = check_order_stock(str(selected_order_id))
    except Exception as exc:
        st.error(f"Unable to calculate live order state: {exc}")
        return

    # --------------------------------------------------------
    # ORDER HEADER
    # --------------------------------------------------------
    st.subheader(f"Order {selected_order_id}")

    h1, h2, h3, h4, h5 = st.columns(5)
    h1.metric("Priority", str(order["priority"]))
    h2.metric("Status", str(order["order_status"]))
    h3.metric("Next Action", action_labels.get(next_action, next_action))
    h4.metric("Total Items", int(order["total_items"] or 0))
    h5.metric("Order Value", f"₹{float(order['order_value'] or 0):,.2f}")

    customer_col, shipping_col = st.columns(2)
    with customer_col:
        st.markdown("**Customer**")
        st.write(str(order["customer_name"]))
        st.write(f"Channel: {order['channel']}  |  Type: {order['customer_type']}")
    with shipping_col:
        st.markdown("**Shipping**")
        st.write(
            f"{order['shipping_city']}, {order['shipping_state']} - {order['pincode']}"
        )
        st.write(f"Ship by: {order['promised_ship_by']}  |  Delivery: {order['promised_delivery_date']}")

    if pd.notna(order.get("priority_reason")) and str(order.get("priority_reason")) not in {"", "nan", "None"}:
        st.info(f"Priority reason: {order['priority_reason']}")

    # --------------------------------------------------------
    # COMPLETE ITEM-LEVEL VIEW
    # --------------------------------------------------------
    st.subheader("Order Items & Live Inventory Allocation")

    item_df = load_order_items(str(selected_order_id))
    stock_items = pd.DataFrame(stock_result.get("items", []))

    if item_df.empty:
        st.warning("This order has no order-item records.")
    else:
        item_view = item_df.copy()
        if not stock_items.empty:
            stock_view = stock_items.rename(columns={
                "sku": "sku",
            })[
                [
                    "sku",
                    "picked_quantity",
                    "reserved_quantity",
                    "remaining_requirement",
                    "wh01_available",
                    "other_warehouse_available",
                    "transfer_required",
                    "shortage",
                    "status",
                ]
            ]
            item_view = item_view.merge(stock_view, on="sku", how="left")

        item_view = item_view.rename(columns={
            "line_item_id": "Line",
            "sku": "SKU",
            "product_name": "Product",
            "variant": "Variant",
            "quantity": "Required",
            "picked_quantity": "Picked",
            "reserved_quantity": "Reserved",
            "remaining_requirement": "Remaining",
            "wh01_available": "WH01 Available",
            "other_warehouse_available": "Other WH Available",
            "transfer_required": "Transfer Required",
            "shortage": "Shortage",
            "status": "Allocation Status",
        })
        st.dataframe(
            item_view,
            use_container_width=True,
            hide_index=True,
        )

    # --------------------------------------------------------
    # ACTION EXPLANATION
    # --------------------------------------------------------
    action_messages = {
        "TRANSFER": "Stock exists outside WH01. Create the physical transfer request first. Inventory is not changed until the warehouse completes the transfer.",
        "TRANSFER_PENDING": "A transfer is already in progress for the required order/SKU. Complete it through Inventory Actions when the stock physically reaches WH01.",
        "SHORTAGE": "Available network stock cannot cover the remaining demand. Raise/handle replenishment through Supply Planning; do not reserve nonexistent stock.",
        "RESERVE": "All remaining order demand can be covered by WH01. Reserve the required quantities before picking.",
        "PICK": "The order is fully reserved. Pick the reserved quantities from WH01.",
        "MARK_PICKED": "All physical item quantities have been picked. Record the Picked fulfillment event before packing.",
        "PACK": "Physical picking is complete. The next workflow step is packing.",
        "STAGE": "Packing is complete. Move the package to the staging/courier handover area.",
        "SHIP": "The order is staged. Confirm courier handover to mark it Shipped.",
        "COMPLETED": "This order has completed the fulfillment workflow.",
    }
    if next_action in action_messages:
        if next_action in {"SHORTAGE"}:
            st.error(action_messages[next_action])
        elif next_action in {"TRANSFER", "TRANSFER_PENDING"}:
            st.warning(action_messages[next_action])
        elif next_action in {"RESERVE", "PICK"}:
            st.info(action_messages[next_action])
        elif next_action == "MARK_PICKED":
            st.success(action_messages[next_action])
        else:
            st.success(action_messages[next_action])

    # --------------------------------------------------------
    # INVENTORY-RESOLUTION ACTIONS
    # --------------------------------------------------------
    if next_action == "TRANSFER":
        st.subheader("1. Create Transfer Request")
        transfer_candidates = [
            item for item in stock_result.get("items", [])
            if int(item.get("transfer_required", 0)) > 0
        ]

        if transfer_candidates:
            labels = {
                f"{item['sku']} — {item['product_name']} — transfer {item['transfer_required']} unit(s)"
                : item
                for item in transfer_candidates
            }
            transfer_label = st.selectbox(
                "Item requiring transfer",
                list(labels.keys()),
                key="fulfillment_transfer_item",
            )
            transfer_item = labels[transfer_label]
            sku = str(transfer_item["sku"])
            required_transfer = int(transfer_item["transfer_required"])

            # Choose the warehouse with the most usable stock. The backend
            # still validates the final quantity before creating the request.
            conn = get_connection()
            source_rows = pd.read_sql_query(
                """
                SELECT warehouse_id, MAX(system_quantity - reserved_quantity, 0) AS available_quantity
                FROM inventory
                WHERE sku = ?
                  AND warehouse_id <> 'WH01'
                GROUP BY warehouse_id
                HAVING MAX(system_quantity - reserved_quantity, 0) > 0
                ORDER BY available_quantity DESC
                """,
                conn,
                params=(sku,),
            )
            conn.close()

            if source_rows.empty:
                st.error("No source warehouse currently has transferable stock. Recalculate the order after inventory changes.")
            else:
                source_options = source_rows["warehouse_id"].astype(str).tolist()
                source_warehouse = st.selectbox(
                    "Source Warehouse",
                    source_options,
                    key="fulfillment_transfer_source",
                )
                source_available = int(
                    source_rows.loc[
                        source_rows["warehouse_id"].astype(str) == source_warehouse,
                        "available_quantity",
                    ].iloc[0]
                )
                transfer_quantity = st.number_input(
                    "Transfer Quantity",
                    min_value=1,
                    max_value=max(1, min(source_available, required_transfer)),
                    value=max(1, min(source_available, required_transfer)),
                    step=1,
                    key="fulfillment_transfer_quantity",
                )
                transfer_notes = st.text_input(
                    "Transfer Notes",
                    value=f"Fulfillment requirement for order {selected_order_id}",
                    key="fulfillment_transfer_notes",
                )

                if st.button(
                    "Create Transfer Request",
                    type="primary",
                    key="fulfillment_create_transfer",
                ):
                    try:
                        result = request_stock_transfer(
                            sku=sku,
                            from_warehouse_id=source_warehouse,
                            to_warehouse_id="WH01",
                            quantity=int(transfer_quantity),
                            reference_order_id=str(selected_order_id),
                            notes=transfer_notes or None,
                        )
                        st.success(
                            f"Transfer request {result['transfer_id']} created for {result['quantity']} unit(s)."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unable to create transfer request: {exc}")

        st.caption("Next: Inventory Actions → complete the physical transfer. The order will automatically recalculate after stock reaches WH01.")

    elif next_action == "TRANSFER_PENDING":
        st.subheader("Transfer In Progress")
        st.info("No reservation or pick is allowed until the required stock physically reaches WH01.")
        pending_transfers = load_transfers()
        pending_transfers = pending_transfers[
            (pending_transfers["reference_order_id"].astype(str) == str(selected_order_id))
            & pending_transfers["transfer_status"].isin(["REQUESTED", "IN_TRANSIT"])
        ]
        if not pending_transfers.empty:
            st.dataframe(
                pending_transfers[
                    [
                        "transfer_id",
                        "sku",
                        "product_name",
                        "from_warehouse",
                        "to_warehouse",
                        "quantity",
                        "transfer_status",
                        "requested_at",
                    ]
                ].rename(columns={
                    "transfer_id": "Transfer ID",
                    "sku": "SKU",
                    "product_name": "Product",
                    "from_warehouse": "From",
                    "to_warehouse": "To",
                    "quantity": "Quantity",
                    "transfer_status": "Status",
                    "requested_at": "Requested At",
                }),
                use_container_width=True,
                hide_index=True,
            )
        st.caption("Open Inventory Actions and complete the physical transfer. Then return here and the queue will recalculate.")

    elif next_action == "SHORTAGE":
        st.subheader("Replenishment Required")
        shortage_items = [
            item for item in stock_result.get("items", [])
            if int(item.get("shortage", 0)) > 0
        ]
        if shortage_items:
            shortage_view = pd.DataFrame(shortage_items)[
                ["sku", "product_name", "remaining_requirement", "other_warehouse_available", "shortage"]
            ].rename(columns={
                "sku": "SKU",
                "product_name": "Product",
                "remaining_requirement": "Remaining Requirement",
                "other_warehouse_available": "Other WH Available",
                "shortage": "Network Shortage",
            })
            st.dataframe(shortage_view, use_container_width=True, hide_index=True)
        st.caption("Use Supply Planning / Supply Requests to procure or receive the missing stock. Once inventory is received, this page recalculates automatically.")

    elif next_action == "RESERVE":
        st.subheader("1. Reserve Required Stock")
        reserve_items = [
            item for item in stock_result.get("items", [])
            if int(item.get("remaining_requirement", 0)) > 0
        ]
        if not reserve_items:
            st.success("Nothing remains to reserve.")
        else:
            reserve_view = pd.DataFrame(reserve_items)[
                ["sku", "product_name", "remaining_requirement", "wh01_available"]
            ].rename(columns={
                "sku": "SKU",
                "product_name": "Product",
                "remaining_requirement": "To Reserve",
                "wh01_available": "WH01 Available",
            })
            st.dataframe(reserve_view, use_container_width=True, hide_index=True)

            reserve_confirmation = st.checkbox(
                "I confirm that the displayed WH01 quantities are physically available for this order.",
                key="fulfillment_reserve_confirmation",
            )
            if st.button(
                "Reserve All Required Stock",
                type="primary",
                disabled=not reserve_confirmation,
                key="fulfillment_reserve_all",
            ):
                try:
                    reservation_items = [
                        {
                            "sku": str(item["sku"]),
                            "quantity": int(item["remaining_requirement"]),
                        }
                        for item in reserve_items
                        if int(item["remaining_requirement"]) > 0
                    ]
                    result = reserve_order_stock(
                        order_id=str(selected_order_id),
                        items=reservation_items,
                        warehouse_id="WH01",
                        notes=f"Fulfillment reservation for order {selected_order_id}",
                    )
                    st.success(
                        f"Reserved {int(result['quantity_reserved'])} unit(s) for order {selected_order_id}."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        f"Reservation failed. Because the reservation is atomic, no reservation was committed: {exc}"
                    )

    elif next_action == "PICK":
        st.subheader("2. Pick Reserved Stock")
        pick_items = [
            item for item in stock_result.get("items", [])
            if int(item.get("remaining_requirement", 0)) > 0
        ]
        if pick_items:
            pick_view = pd.DataFrame(pick_items)[
                ["sku", "product_name", "remaining_requirement", "reserved_quantity"]
            ].rename(columns={
                "sku": "SKU",
                "product_name": "Product",
                "remaining_requirement": "To Pick",
                "reserved_quantity": "Reserved",
            })
            st.dataframe(pick_view, use_container_width=True, hide_index=True)

            pick_confirmation = st.checkbox(
                "I confirm that the reserved physical items have been picked from WH01.",
                key="fulfillment_pick_confirmation_live",
            )
            if st.button(
                "Pick All Reserved Stock",
                type="primary",
                disabled=not pick_confirmation,
                key="fulfillment_pick_all",
            ):
                try:
                    pick_items_payload = [
                        {
                            "sku": str(item["sku"]),
                            "quantity": min(
                                int(item["remaining_requirement"]),
                                int(item["reserved_quantity"]),
                            ),
                        }
                        for item in pick_items
                        if min(
                            int(item["remaining_requirement"]),
                            int(item["reserved_quantity"]),
                        ) > 0
                    ]
                    result = pick_order_stock(
                        order_id=str(selected_order_id),
                        items=pick_items_payload,
                        warehouse_id="WH01",
                        notes=f"Physical pick for order {selected_order_id}",
                    )
                    st.success(
                        f"Picked {int(result['quantity_picked'])} unit(s)."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        f"Picking failed. Because the pick is atomic, no pick was committed: {exc}"
                    )
        else:
            st.info("All item quantities are already picked/resolved. The order can be marked Picked.")

    elif next_action == "MARK_PICKED":
        st.subheader("3. Confirm Order Picked")
        st.info(
            "All required item quantities have a completed physical PICK transaction. "
            "This step records the fulfillment event and changes the order status to Picked; it does not change inventory again."
        )
        if st.button(
            "Mark Order Picked",
            type="primary",
            key="fulfillment_mark_order_picked_live",
        ):
            try:
                result = mark_order_picked(
                    order_id=str(selected_order_id),
                    notes=f"Order {selected_order_id} physically picked.",
                )
                st.success(f"Order {result['order_id']} is now Picked.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to mark order as Picked: {exc}")

    # --------------------------------------------------------
    # DOWNSTREAM EXECUTION
    # --------------------------------------------------------
    elif next_action in {"PACK", "STAGE", "SHIP"}:
        st.subheader(f"Next Step: {action_labels[next_action]}")

        notes = st.text_area(
            "Operational Notes",
            placeholder="Optional notes for this fulfillment step",
            key=f"fulfillment_{next_action.lower()}_notes_live",
        )

        if next_action == "PACK":
            if st.button("Mark Packed", type="primary", key="fulfillment_pack_live"):
                try:
                    result = mark_order_packed(str(selected_order_id), notes=notes or None)
                    st.success(f"Order {result['order_id']} is now Packed.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to mark order as Packed: {exc}")

        elif next_action == "STAGE":
            if st.button("Mark Staged", type="primary", key="fulfillment_stage_live"):
                try:
                    result = mark_order_staged(str(selected_order_id), notes=notes or None)
                    st.success(f"Order {result['order_id']} is now Staged.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to mark order as Staged: {exc}")

        elif next_action == "SHIP":
            confirmation = st.checkbox(
                "I confirm that the package has been handed over to the courier.",
                key="fulfillment_ship_confirmation_live",
            )
            if st.button(
                "Mark Shipped",
                type="primary",
                disabled=not confirmation,
                key="fulfillment_ship_live",
            ):
                try:
                    result = mark_order_shipped(str(selected_order_id), notes=notes or None)
                    st.success(f"Order {result['order_id']} is now Shipped.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Unable to mark order as Shipped: {exc}")

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------
    with st.expander("Fulfillment History", expanded=False):
        try:
            history = get_order_fulfillment_history(str(selected_order_id))
            events = history.get("events", [])
            if events:
                history_df = pd.DataFrame(events).rename(columns={
                    "event_id": "Event ID",
                    "event_type": "Event",
                    "event_status": "Status",
                    "notes": "Notes",
                    "event_time": "Event Time",
                })
                st.dataframe(history_df, use_container_width=True, hide_index=True)
            else:
                st.info("No fulfillment events recorded yet.")
        except Exception as exc:
            st.error(f"Unable to load fulfillment history: {exc}")

    st.caption(
        "Live state is recalculated from SQLite after every transaction. Fulfillment Operations does not directly edit inventory; inventory transactions and order-status events remain separate and auditable."
    )



# ============================================================
# ORDER FLOW / CENTRAL ORDER OPERATIONS
# ============================================================

def _load_order_flow_data(order_id):
    """Load one order and every operational record needed by Order Flow."""
    connection = get_connection()
    try:
        order = pd.read_sql_query(
            """
            SELECT
                order_id,
                marketplace_order_id,
                order_status,
                priority,
                priority_reason,
                customer_name,
                customer_type,
                channel,
                shipping_city,
                shipping_state,
                pincode,
                promised_ship_by,
                promised_delivery_date,
                payment_status,
                total_items,
                order_value
            FROM orders
            WHERE order_id = ?
            """,
            connection,
            params=(str(order_id),),
        )

        transactions = pd.read_sql_query(
            """
            SELECT
                transaction_id,
                transaction_time,
                sku,
                warehouse_id,
                transaction_type,
                quantity,
                reference_id,
                notes
            FROM inventory_transactions
            WHERE reference_id = ?
            ORDER BY transaction_time ASC, transaction_id ASC
            """,
            connection,
            params=(str(order_id),),
        )

        transfers = pd.read_sql_query(
            """
            SELECT
                transfer_id,
                sku,
                from_warehouse_id AS source_warehouse_id,
                to_warehouse_id AS destination_warehouse_id,
                quantity,
                transfer_status AS status,
                requested_at,
                completed_at,
                reference_order_id,
                notes
            FROM stock_transfers
            WHERE reference_order_id = ?
            ORDER BY requested_at ASC, transfer_id ASC
            """,
            connection,
            params=(str(order_id),),
        )
    finally:
        connection.close()

    return order, transactions, transfers


def _order_flow_stage(order_status, next_action, transactions, transfers):
    """Return a concise current stage and reason for the selected order."""
    status = str(order_status or "")
    action = str(next_action or "")

    active_transfer = (
        not transfers.empty
        and transfers["status"].astype(str).isin(["REQUESTED", "IN_TRANSIT"]).any()
    )

    if status in {"Completed", "Delivered"}:
        return "COMPLETED", "The order has completed the fulfillment lifecycle."
    if status == "Shipped":
        return "SHIPPED", "The order has been shipped."
    if status == "Staged":
        return "STAGED", "The order is staged and ready to ship."
    if status == "Packed":
        return "PACKED", "The order is packed and ready to stage."
    if status == "Picked":
        return "PICKED", "The physical pick is complete and the order is ready to pack."
    if action == "TRANSFER_PENDING" or active_transfer:
        return "TRANSFER", "A warehouse transfer is in progress. Complete it from Transfer Queue."
    if action == "TRANSFER":
        return "TRANSFER", "WH01 cannot satisfy the remaining demand from its current stock; transfer stock into WH01 first."
    if action == "SHORTAGE":
        return "SHORTAGE", "The network does not currently contain enough stock. Supply Planning is required."
    if action == "RESERVE":
        return "RESERVE", "Required WH01 stock is available and can be reserved for this order."
    if action in {"PICK", "MARK_PICKED"}:
        return "PICK", "The order has order-specific reserved stock ready for physical picking."
    if action == "PACK":
        return "PACK", "Physical picking is complete; the order can be packed."
    if action == "STAGE":
        return "STAGE", "Packing is complete; the order can be staged."
    if action == "SHIP":
        return "SHIP", "The order is staged and ready to ship."

    return status.upper() or "REVIEW", f"Current order status: {status or 'Unknown'}."


def _order_flow_step_state(step, next_action, order_status, transfers):
    """Return DONE/CURRENT/WAITING/NOT REQUIRED for a lifecycle tile."""
    status = str(order_status or "")
    action = str(next_action or "")
    transfer_pending = (
        not transfers.empty
        and transfers["status"].astype(str).isin(["REQUESTED", "IN_TRANSIT"]).any()
    )
    transfer_completed = (
        not transfers.empty
        and transfers["status"].astype(str).eq("COMPLETED").any()
    )

    completed_statuses = {
        "Picked": {"TRANSFER", "RESERVE", "PICK"},
        "Packed": {"TRANSFER", "RESERVE", "PICK", "PACK"},
        "Staged": {"TRANSFER", "RESERVE", "PICK", "PACK", "STAGE"},
        "Shipped": {"TRANSFER", "RESERVE", "PICK", "PACK", "STAGE", "SHIP"},
        "Delivered": {"TRANSFER", "RESERVE", "PICK", "PACK", "STAGE", "SHIP"},
        "Completed": {"TRANSFER", "RESERVE", "PICK", "PACK", "STAGE", "SHIP"},
    }

    if step == "TRANSFER":
        if transfer_pending or action in {"TRANSFER", "TRANSFER_PENDING"}:
            return "CURRENT" if action == "TRANSFER" else "IN PROGRESS"
        if transfer_completed:
            return "DONE"
        return "NOT REQUIRED"

    if step in completed_statuses.get(status, set()):
        return "DONE"

    current_map = {
        "RESERVE": {"RESERVE"},
        "PICK": {"PICK", "MARK_PICKED"},
        "PACK": {"PACK"},
        "STAGE": {"STAGE"},
        "SHIP": {"SHIP"},
    }
    if action in current_map.get(step, set()):
        return "CURRENT"

    if action in {"TRANSFER", "TRANSFER_PENDING", "SHORTAGE"}:
        return "WAITING"
    return "WAITING"


def _get_order_flow_transfer_candidates(order_id):
    """Resolve the live transfer requirement into concrete source warehouses."""
    stock = check_order_stock(order_id)
    connection = get_connection()
    try:
        candidates = []
        for item in stock.get("items", []):
            required = int(item.get("transfer_required", 0) or 0)
            if required <= 0:
                continue

            sku = str(item["sku"])
            pending = connection.execute(
                """
                SELECT COALESCE(SUM(quantity), 0)
                FROM stock_transfers
                WHERE reference_order_id = ?
                  AND sku = ?
                  AND to_warehouse_id = 'WH01'
                  AND transfer_status IN ('REQUESTED', 'IN_TRANSIT')
                """,
                (str(order_id), sku),
            ).fetchone()[0] or 0
            outstanding = max(required - int(pending), 0)
            if outstanding <= 0:
                continue

            source_rows = connection.execute(
                """
                SELECT
                    warehouse_id,
                    MAX(COALESCE(system_quantity, 0) - COALESCE(reserved_quantity, 0), 0) AS available_quantity
                FROM inventory
                WHERE sku = ?
                  AND warehouse_id <> 'WH01'
                GROUP BY warehouse_id
                HAVING available_quantity > 0
                ORDER BY available_quantity DESC, warehouse_id
                """,
                (sku,),
            ).fetchall()

            remaining = outstanding
            for warehouse_id, available_quantity in source_rows:
                if remaining <= 0:
                    break
                qty = min(remaining, int(available_quantity or 0))
                if qty <= 0:
                    continue
                candidates.append({
                    "sku": sku,
                    "product_name": item.get("product_name", ""),
                    "from_warehouse": str(warehouse_id),
                    "to_warehouse": "WH01",
                    "quantity": qty,
                })
                remaining -= qty

            if remaining > 0:
                raise ValueError(
                    f"Live allocation requires {outstanding} unit(s) of {sku}, "
                    "but the currently available source stock cannot satisfy the transfer request."
                )
    finally:
        connection.close()
    return candidates


def _execute_order_flow_action(order_id, action):
    """Execute exactly one live lifecycle action after re-reading SQLite state.

    This function is deliberately defensive: the UI may have been rendered
    seconds before another operator changed the same order. Every action
    therefore recalculates the order state before mutating anything.
    """
    live_action = get_next_fulfillment_action(order_id)

    if action == "PICK" and live_action == "MARK_PICKED":
        mark_order_picked(
            str(order_id),
            notes=f"Order Picked from Order Flow for {order_id}",
        )
        return "Order marked Picked."

    if live_action != action:
        raise ValueError(
            f"Order {order_id} is no longer ready for {action}. "
            f"Its current next action is {live_action}."
        )

    if action == "TRANSFER":
        candidates = _get_order_flow_transfer_candidates(order_id)
        if not candidates:
            raise ValueError("No new transfer is required; the live order state has changed.")

        result = request_order_transfers(
            order_id=str(order_id),
            transfers=[
                {
                    "sku": item["sku"],
                    "from_warehouse_id": item["from_warehouse"],
                    "to_warehouse_id": "WH01",
                    "quantity": int(item["quantity"]),
                }
                for item in candidates
            ],
            notes=f"Transfer requested from Order Flow for {order_id}",
        )
        return (
            "Transfer request created: "
            + ", ".join(
                f"#{item['transfer_id']} {item['sku']} x{item['quantity']}"
                for item in result["transfers"]
            )
            + ". Inventory has not moved yet."
        )

    if action == "RESERVE":
        stock = check_order_stock(order_id)
        if stock.get("overall_status") != "AVAILABLE":
            raise ValueError(
                f"The order is no longer fully reservable. Current inventory state is {stock.get('overall_status')}."
            )

        items = []
        for item in stock.get("items", []):
            remaining = int(item.get("remaining_requirement", 0) or 0)
            reserved = int(item.get("reserved_quantity", 0) or 0)
            quantity = max(remaining - reserved, 0)
            if quantity > 0:
                items.append({"sku": str(item["sku"]), "quantity": quantity})

        if not items:
            raise ValueError("No additional order-specific reservation is required.")

        result = reserve_order_stock(
            order_id=str(order_id),
            items=items,
            warehouse_id="WH01",
            notes=f"Reservation from Order Flow for {order_id}",
        )
        return f"Reserved {int(result['quantity_reserved'])} unit(s) for {order_id}."

    if action == "PICK":
        stock = check_order_stock(order_id)
        items = []
        for item in stock.get("items", []):
            quantity = int(item.get("reserved_quantity", 0) or 0)
            remaining = int(item.get("remaining_requirement", 0) or 0)
            if quantity > 0:
                items.append({
                    "sku": str(item["sku"]),
                    "quantity": min(quantity, remaining),
                })
        items = [item for item in items if item["quantity"] > 0]
        if not items:
            raise ValueError(
                "No order-specific reserved quantity is currently available to pick. "
                "The live state has changed."
            )

        result = pick_order_stock(
            order_id=str(order_id),
            items=items,
            warehouse_id="WH01",
            notes=f"Physical pick from Order Flow for {order_id}",
        )

        # Picking is a physical inventory transaction. Once the complete
        # physical pick has succeeded, immediately record the operational
        # Picked state so the lifecycle moves directly to Pack.
        mark_order_picked(
            str(order_id),
            notes=f"Physical pick completed from Order Flow for {order_id}",
        )
        return f"Picked {int(result['quantity_picked'])} unit(s) and marked {order_id} Picked."

    operation_map = {
        "PACK": (mark_order_packed, "Packed"),
        "STAGE": (mark_order_staged, "Staged"),
        "SHIP": (mark_order_shipped, "Shipped"),
    }
    if action in operation_map:
        operation, status_label = operation_map[action]
        operation(
            str(order_id),
            notes=f"{status_label} from Order Flow for {order_id}",
        )
        return f"Order {order_id} marked {status_label}."

    if action == "MARK_PICKED":
        mark_order_picked(
            str(order_id),
            notes=f"Order Picked from Order Flow for {order_id}",
        )
        return f"Order {order_id} marked Picked."

    raise ValueError(f"Action '{action}' is not executable from Order Flow.")


def _render_order_flow_lifecycle(order_id, order, next_action, transfers):
    """Render the lifecycle itself as the only operational button surface."""
    steps = [
        ("TRANSFER", "Transfer"),
        ("RESERVE", "Reserve"),
        ("PICK", "Pick"),
        ("PACK", "Pack"),
        ("STAGE", "Stage"),
        ("SHIP", "Ship"),
    ]
    state_text = {
        "DONE": "Completed",
        "CURRENT": "Current — Click to execute",
        "IN PROGRESS": "In progress",
        "WAITING": "Waiting",
        "NOT REQUIRED": "Not required",
    }

    st.subheader("Fulfillment Lifecycle")
    st.caption(
        "The lifecycle is the control surface. Only the current executable step is clickable. "
        "Transfer is an exception gate; its physical completion is confirmed in Transfer Queue. "
        "After each successful action, the live state is re-read from SQLite."
    )

    cols = st.columns(len(steps))
    for col, (key, label) in zip(cols, steps):
        state = _order_flow_step_state(
            key,
            next_action,
            order["order_status"],
            transfers,
        )
        with col:
            if state == "CURRENT":
                clicked = st.button(
                    f"{label}\nCurrent",
                    type="primary",
                    use_container_width=True,
                    key=f"order_flow_lifecycle_{key}_{order_id}",
                )
                if clicked:
                    try:
                        message = _execute_order_flow_action(order_id, next_action)
                        st.session_state["order_flow_action_message"] = message
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{label} failed: {exc}")
            elif state == "DONE":
                st.success(f"{label}\nCompleted")
            elif state == "IN PROGRESS":
                st.warning(f"{label}\nIn progress")
            elif state == "NOT REQUIRED":
                st.info(f"{label}\nNot required")
            else:
                st.button(
                    f"{label}\nWaiting",
                    disabled=True,
                    use_container_width=True,
                    key=f"order_flow_waiting_{key}_{order_id}",
                )


def show_order_flow_page():
    """Central order-centric fulfillment workspace.

    This page intentionally combines tracking and the operational actions that
    belong to an individual order. It is the preferred UI for Reserve, Pick,
    Pack, Stage and Ship. Inventory Actions, Transfer Queue and Supply Planning
    remain separate because they operate on warehouse-wide work rather than a
    single order.
    """
    st.title("Order Flow")
    st.caption(
        "Select an order, see its live inventory position, and execute the next valid step directly from the lifecycle. "
        "Inventory, transfers and fulfillment status are read from SQLite; the UI does not maintain a separate operational state."
    )

    orders_df = load_orders()
    if orders_df.empty:
        st.info("No orders are available.")
        return

    # --------------------------------------------------------
    # FILTERS + ORDER SELECTOR
    # --------------------------------------------------------
    f1, f2, f3 = st.columns(3)
    with f1:
        status_options = ["All"] + sorted(orders_df["order_status"].dropna().astype(str).unique().tolist())
        status_filter = st.selectbox("Order Status", status_options, key="order_flow_status_filter")
    with f2:
        priority_options = ["All"] + sorted(orders_df["priority"].dropna().astype(str).unique().tolist())
        priority_filter = st.selectbox("Priority", priority_options, key="order_flow_priority_filter")
    with f3:
        search_filter = st.text_input(
            "Search Order ID",
            placeholder="e.g. MP-10035",
            key="order_flow_search",
        ).strip()

    filtered = orders_df.copy()
    if status_filter != "All":
        filtered = filtered[filtered["order_status"].astype(str) == status_filter]
    if priority_filter != "All":
        filtered = filtered[filtered["priority"].astype(str) == priority_filter]
    if search_filter:
        filtered = filtered[filtered["order_id"].astype(str).str.contains(search_filter, case=False, na=False)]

    if filtered.empty:
        st.warning("No orders match the selected filters.")
        return

    tracking_rows = []
    for _, row in filtered.iterrows():
        oid = str(row["order_id"])
        try:
            action = get_next_fulfillment_action(oid)
        except Exception:
            action = "ERROR"
        tracking_rows.append({
            "Order ID": oid,
            "Priority": row.get("priority", ""),
            "Status": row.get("order_status", ""),
            "Next Action": {
                "TRANSFER": "Transfer Required",
                "TRANSFER_PENDING": "Transfer In Progress",
                "SHORTAGE": "Shortage",
                "RESERVE": "Reserve",
                "PICK": "Pick",
                "MARK_PICKED": "Pick Confirmation",
                "PACK": "Pack",
                "STAGE": "Stage",
                "SHIP": "Ship",
                "COMPLETED": "Completed",
                "ERROR": "Review",
            }.get(action, action),
            "Ship By": row.get("promised_ship_by", ""),
        })

    st.subheader("Live Order Tracking")
    st.dataframe(pd.DataFrame(tracking_rows), use_container_width=True, hide_index=True)

    all_order_ids = orders_df["order_id"].astype(str).tolist()
    filtered_order_ids = filtered["order_id"].astype(str).tolist()
    remembered = st.session_state.get("order_flow_selected_order")
    default_index = filtered_order_ids.index(remembered) if remembered in filtered_order_ids else 0

    s1, s2 = st.columns([1.2, 1])
    with s1:
        selected_from_list = st.selectbox(
            "Order ID — select or type to search",
            filtered_order_ids,
            index=default_index,
            key="order_flow_selected_order",
        )
    with s2:
        typed = st.text_input(
            "Or type Order ID",
            placeholder="e.g. MP-10035",
            key="order_flow_typed_order_id",
        ).strip()

    selected_order_id = typed or str(selected_from_list)
    if selected_order_id not in set(all_order_ids):
        st.error(f"Order ID '{selected_order_id}' was not found.")
        return

    order_data, transactions, transfers = _load_order_flow_data(selected_order_id)
    if order_data.empty:
        st.error("Selected order could not be loaded.")
        return
    order = order_data.iloc[0]

    try:
        next_action = get_next_fulfillment_action(selected_order_id)
    except Exception as exc:
        next_action = "ERROR"
        st.error(f"Unable to calculate the live next action: {exc}")

    current_stage, explanation = _order_flow_stage(
        order["order_status"], next_action, transactions, transfers
    )

    action_message = st.session_state.pop("order_flow_action_message", None)
    if action_message:
        st.success(action_message)

    st.divider()
    st.subheader(f"Order {selected_order_id}")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Status", str(order["order_status"]))
    m2.metric("Current Stage", current_stage)
    m3.metric("Next Action", str(next_action).replace("_", " "))
    m4.metric("Priority", str(order["priority"]))
    m5.metric("Order Value", f"₹{float(order['order_value'] or 0):,.2f}")

    st.info(explanation)

    # --------------------------------------------------------
    # LIVE ITEM-LEVEL INVENTORY STATE
    # --------------------------------------------------------
    st.subheader("Order Items & Live Inventory Allocation")
    try:
        stock = check_order_stock(selected_order_id)
        item_rows = stock.get("items", [])
        if item_rows:
            item_view = pd.DataFrame(item_rows).rename(columns={
                "sku": "SKU",
                "product_name": "Product",
                "required_quantity": "Required",
                "picked_quantity": "Picked",
                "reserved_quantity": "Reserved",
                "remaining_requirement": "Remaining",
                "wh01_available": "WH01 Available",
                "other_warehouse_available": "Other WH Available",
                "transfer_required": "Transfer Required",
                "shortage": "Shortage",
                "status": "Allocation Status",
            })
            display_cols = [
                "SKU", "Product", "Required", "Picked", "Reserved", "Remaining",
                "WH01 Available", "Other WH Available", "Transfer Required", "Shortage", "Allocation Status",
            ]
            st.dataframe(item_view[display_cols], use_container_width=True, hide_index=True)

            if stock.get("overall_status") == "SHORTAGE":
                st.error("One or more items have a genuine network shortage. Use Supply Planning to raise/fulfill supply.")
            elif stock.get("overall_status") == "TRANSFER_REQUIRED":
                st.warning("One or more items require warehouse transfer into WH01 before reservation.")
            elif next_action == "PICK":
                st.success("All required quantities are reserved for this order and ready for physical picking.")
            elif next_action == "RESERVE":
                st.success("Required WH01 stock is available for reservation.")
        else:
            st.info("No active inventory-demand items remain for this order.")
    except Exception as exc:
        st.error(f"Unable to calculate live inventory allocation: {exc}")

    # --------------------------------------------------------
    # LIFECYCLE — THE ONLY ACTION BUTTONS
    # --------------------------------------------------------
    _render_order_flow_lifecycle(
        selected_order_id,
        order,
        next_action,
        transfers,
    )

    # --------------------------------------------------------
    # ORDER / CUSTOMER CONTEXT
    # --------------------------------------------------------
    with st.expander("Order & Customer Details", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.write(f"**Customer:** {order.get('customer_name', '')}")
            st.write(f"**Customer Type:** {order.get('customer_type', '')}")
            st.write(f"**Channel:** {order.get('channel', '')}")
            st.write(f"**Payment:** {order.get('payment_status', '')}")
        with c2:
            st.write(f"**Ship To:** {order.get('shipping_city', '')}, {order.get('shipping_state', '')} - {order.get('pincode', '')}")
            st.write(f"**Promised Ship By:** {order.get('promised_ship_by', '')}")
            st.write(f"**Promised Delivery:** {order.get('promised_delivery_date', '')}")
            if pd.notna(order.get("priority_reason")):
                st.write(f"**Priority Reason:** {order.get('priority_reason')}")

    # --------------------------------------------------------
    # TRANSFER / INVENTORY / FULFILLMENT HISTORY
    # --------------------------------------------------------
    st.subheader("Order Activity")
    activity_rows = []

    for _, row in transfers.iterrows():
        activity_rows.append({
            "Time": row.get("completed_at") or row.get("requested_at"),
            "Type": "TRANSFER",
            "Reference": f"TR-{row.get('transfer_id')}",
            "SKU": row.get("sku"),
            "Details": f"{row.get('source_warehouse_id')} → {row.get('destination_warehouse_id')} | Qty {row.get('quantity')} | {row.get('status')}",
        })

    for _, row in transactions.iterrows():
        if str(row.get("transaction_type")) in {"RESERVE", "PICK", "RELEASE"}:
            activity_rows.append({
                "Time": row.get("transaction_time"),
                "Type": str(row.get("transaction_type")),
                "Reference": row.get("reference_id"),
                "SKU": row.get("sku"),
                "Details": f"{row.get('warehouse_id')} | Qty {row.get('quantity')} | {row.get('notes') or ''}",
            })

    try:
        history = get_order_fulfillment_history(selected_order_id)
        for event in history.get("events", []):
            activity_rows.append({
                "Time": event.get("event_time"),
                "Type": event.get("event_type"),
                "Reference": f"EVENT-{event.get('event_id')}",
                "SKU": "",
                "Details": f"{event.get('event_status') or ''} | {event.get('notes') or ''}",
            })
    except Exception as exc:
        st.error(f"Unable to load fulfillment history: {exc}")

    if activity_rows:
        activity_df = pd.DataFrame(activity_rows)
        activity_df["Time"] = pd.to_datetime(activity_df["Time"], errors="coerce")
        activity_df = activity_df.sort_values("Time", ascending=False, na_position="last")
        st.dataframe(activity_df, use_container_width=True, hide_index=True)
    else:
        st.info("No order activity has been recorded yet.")

    if not transfers.empty:
        st.subheader("Transfer Status")
        transfer_view = transfers.rename(columns={
            "transfer_id": "Transfer ID",
            "sku": "SKU",
            "source_warehouse_id": "From",
            "destination_warehouse_id": "To",
            "quantity": "Quantity",
            "status": "Status",
            "requested_at": "Requested At",
            "completed_at": "Completed At",
        })
        st.dataframe(
            transfer_view[[
                "Transfer ID", "SKU", "From", "To", "Quantity", "Status", "Requested At", "Completed At"
            ]],
            use_container_width=True,
            hide_index=True,
        )

    if next_action == "TRANSFER_PENDING":
        st.info("This order is waiting for physical transfer completion. Complete the transfer in Transfer Queue; this page will update automatically on refresh.")
    elif next_action == "SHORTAGE":
        st.warning("This order is blocked by a network shortage. Use Supply Planning to create or manage the replenishment request.")
    elif next_action == "COMPLETED":
        st.success("Fulfillment lifecycle completed.")


# ============================================================
# CONTROL CENTER
# ============================================================

def show_control_center_page():

    st.title(
        "Control Center"
    )

    st.caption(
        "Exception-oriented operational view for live orders, inventory, transfers, and fulfillment bottlenecks. Metrics and queues are recalculated from the current SQLite state on page load."
    )

    orders_df = load_orders()
    inventory_df = load_inventory()
    transfers_df = load_transfers()

    if orders_df.empty:
        st.warning(
            "No orders are available."
        )
        return

    # --------------------------------------------------------
    # BUILD OPERATIONAL QUEUES
    # --------------------------------------------------------

    inventory_demand_active_df = get_active_orders_df(orders_df)
    fulfillment_active_df = get_fulfillment_active_orders_df(orders_df)

    action_required_df = get_action_required_orders(
        inventory_demand_active_df
    )

    today = datetime.now().date()

    overdue_df = fulfillment_active_df.copy()
    overdue_dates = pd.to_datetime(
        overdue_df["promised_ship_by"],
        errors="coerce"
    )
    overdue_df = overdue_df[
        overdue_dates.dt.date < today
    ].copy()

    priority_df = fulfillment_active_df[
        fulfillment_active_df["priority"].isin(
            [
                "Critical",
                "High"
            ]
        )
    ].copy()

    transfer_required_df = action_required_df[
        action_required_df["next_action"] == "TRANSFER"
    ].copy()

    shortage_df = action_required_df[
        action_required_df["next_action"] == "SHORTAGE"
    ].copy()

    reserve_df = action_required_df[
        action_required_df["next_action"] == "RESERVE"
    ].copy()

    pick_df = action_required_df[
        action_required_df["next_action"] == "PICK"
    ].copy()

    active_transfers_df = transfers_df[
        transfers_df["transfer_status"].isin(
            [
                "REQUESTED",
                "IN_TRANSIT"
            ]
        )
    ].copy()

    # --------------------------------------------------------
    # TOP METRICS
    # --------------------------------------------------------

    st.subheader(
        "Operational Attention"
    )

    col1, col2, col3, col4, col5 = st.columns(5)

    col1.metric(
        "Inventory Action Required",
        len(action_required_df),
    )

    col2.metric(
        "Overdue",
        len(overdue_df),
    )

    col3.metric(
        "Critical / High",
        len(priority_df),
    )

    col4.metric(
        "Orders Requiring Transfer",
        len(transfer_required_df),
    )

    col5.metric(
        "Orders With Shortage",
        len(shortage_df),
    )

    st.divider()

    # --------------------------------------------------------
    # ACTION QUEUE
    # --------------------------------------------------------

    st.subheader(
        "Fulfillment Action Queue"
    )

    if action_required_df.empty:

        st.success(
            "No fulfillment actions are currently flagged."
        )

    else:

        queue_df = action_required_df.merge(
            orders_df[
                [
                    "order_id",
                    "priority",
                    "promised_ship_by",
                    "promised_delivery_date",
                    "order_status",
                    "customer_name",
                    "shipping_city",
                ]
            ],
            on="order_id",
            how="left",
        )

        queue_df = queue_df.rename(
            columns={
                "order_id": "Order ID",
                "next_action": "Next Action",
                "priority": "Priority",
                "promised_ship_by": "Ship By",
                "promised_delivery_date": "Delivery Date",
                "order_status": "Current Status",
                "customer_name": "Customer",
                "shipping_city": "City",
            }
        )

        st.dataframe(
            queue_df,
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # --------------------------------------------------------
    # OVERDUE / PRIORITY ORDERS
    # --------------------------------------------------------

    left, right = st.columns(2)

    with left:

        st.subheader(
            "Overdue Ship-By Orders"
        )

        if overdue_df.empty:

            st.success(
                "No orders are past their promised ship-by date."
            )

        else:

            display_df = overdue_df[
                [
                    "order_id",
                    "priority",
                    "promised_ship_by",
                    "order_status",
                    "customer_name",
                ]
            ].copy()

            display_df = display_df.rename(
                columns={
                    "order_id": "Order ID",
                    "priority": "Priority",
                    "promised_ship_by": "Ship By",
                    "order_status": "Status",
                    "customer_name": "Customer",
                }
            )

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )

    with right:

        st.subheader(
            "Critical / High Priority"
        )

        if priority_df.empty:

            st.info(
                "No Critical or High priority orders."
            )

        else:

            display_df = priority_df[
                [
                    "order_id",
                    "priority",
                    "promised_ship_by",
                    "order_status",
                    "customer_name",
                ]
            ].copy()

            display_df = display_df.rename(
                columns={
                    "order_id": "Order ID",
                    "priority": "Priority",
                    "promised_ship_by": "Ship By",
                    "order_status": "Status",
                    "customer_name": "Customer",
                }
            )

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # --------------------------------------------------------
    # INVENTORY-RELATED BOTTLENECKS
    # --------------------------------------------------------

    st.subheader(
        "Inventory Bottlenecks"
    )

    inventory_left, inventory_right = st.columns(2)

    with inventory_left:

        st.markdown(
            "### Transfer Required"
        )

        if transfer_required_df.empty:

            st.success(
                "No orders currently require a stock transfer."
            )

        else:

            display_df = transfer_required_df.merge(
                orders_df[
                    [
                        "order_id",
                        "priority",
                        "promised_ship_by",
                        "order_status",
                    ]
                ],
                on="order_id",
                how="left",
            )

            display_df = display_df.rename(
                columns={
                    "order_id": "Order ID",
                    "next_action": "Action",
                    "priority": "Priority",
                    "promised_ship_by": "Ship By",
                    "order_status": "Status",
                }
            )

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )

    with inventory_right:

        st.markdown(
            "### Stock Shortage"
        )

        if shortage_df.empty:

            st.success(
                "No orders currently have a detected stock shortage."
            )

        else:

            display_df = shortage_df.merge(
                orders_df[
                    [
                        "order_id",
                        "priority",
                        "promised_ship_by",
                        "order_status",
                    ]
                ],
                on="order_id",
                how="left",
            )

            display_df = display_df.rename(
                columns={
                    "order_id": "Order ID",
                    "next_action": "Action",
                    "priority": "Priority",
                    "promised_ship_by": "Ship By",
                    "order_status": "Status",
                }
            )

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # --------------------------------------------------------
    # RESERVATION / PICKING QUEUES
    # --------------------------------------------------------

    col1, col2 = st.columns(2)

    with col1:

        st.subheader(
            "Orders Awaiting Reservation"
        )

        if reserve_df.empty:

            st.success(
                "No orders are currently waiting for reservation."
            )

        else:

            st.dataframe(
                reserve_df,
                use_container_width=True,
                hide_index=True,
            )

    with col2:

        st.subheader(
            "Orders Awaiting Pick"
        )

        if pick_df.empty:

            st.success(
                "No orders are currently waiting for picking."
            )

        else:

            st.dataframe(
                pick_df,
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # --------------------------------------------------------
    # TRANSFERS
    # --------------------------------------------------------

    st.subheader(
        "Stock Transfers"
    )

    if transfers_df.empty:

        st.info(
            "No stock transfers have been recorded."
        )

    else:

        transfer_view = transfers_df.copy()

        transfer_view["Transfer State"] = transfer_view[
            "transfer_status"
        ]

        transfer_view = transfer_view[
            [
                "transfer_id",
                "sku",
                "product_name",
                "from_warehouse",
                "to_warehouse",
                "quantity",
                "Transfer State",
                "reference_order_id",
                "requested_at",
                "completed_at",
            ]
        ]

        transfer_view = transfer_view.rename(
            columns={
                "transfer_id": "Transfer ID",
                "sku": "SKU",
                "product_name": "Product",
                "from_warehouse": "From",
                "to_warehouse": "To",
                "quantity": "Quantity",
                "reference_order_id": "Reference Order",
                "requested_at": "Requested At",
                "completed_at": "Completed At",
            }
        )

        st.dataframe(
            transfer_view,
            use_container_width=True,
            hide_index=True,
        )

        if not active_transfers_df.empty:

            st.warning(
                f"{len(active_transfers_df)} transfer(s) are currently active."
            )

        else:

            st.info(
                "There are no currently active transfers."
            )

    st.divider()

    st.caption(
        "This page is read-only. Operational changes are performed through Inventory Actions and Fulfillment Operations."
    )


# ============================================================
# IMPORT CENTER
# ============================================================

def show_marketplace_order_batch_import():

    st.subheader(
        "Marketplace Order Batch"
    )

    st.write(
        "Use this workflow for the daily marketplace order feed. "
        "Each uploaded batch is added to the existing order history. "
        "New orders are inserted and an order already in SQLite is refreshed by order_id. "
        "For an existing order, the uploaded line-item set replaces that order's previous line items so corrected marketplace feeds do not leave stale items behind. "
        "Orders and their matching items are committed together as one atomic batch."
    )

    st.info(
        "After the batch is imported, the Dashboard, Orders, Supply Planning and Fulfillment Operations pages "
        "read the live SQLite database again. Open demand and inventory requirements therefore recalculate from "
        "the cumulative order history and current fulfillment status. If any part of the batch fails, both orders "
        "and order items are rolled back together."
    )

    st.subheader(
        "1. Upload Marketplace Orders"
    )

    orders_file = st.file_uploader(
        "Choose marketplace orders CSV",
        type=["csv"],
        key="marketplace_orders_batch_upload"
    )

    st.subheader(
        "2. Upload Marketplace Order Items"
    )

    order_items_file = st.file_uploader(
        "Choose marketplace order items CSV",
        type=["csv"],
        key="marketplace_order_items_batch_upload"
    )

    if orders_file is None or order_items_file is None:

        st.info(
            "Upload both the marketplace orders CSV and the matching order items CSV to continue."
        )

        return

    try:

        orders_df = pd.read_csv(
            orders_file
        )

        order_items_df = pd.read_csv(
            order_items_file
        )

    except Exception as e:

        st.error(
            f"Unable to read marketplace CSV files: {e}"
        )

        return

    st.success(
        f"Orders file loaded: {orders_file.name}"
    )

    st.success(
        f"Order items file loaded: {order_items_file.name}"
    )

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Order Rows",
        len(orders_df)
    )

    col2.metric(
        "Order Item Rows",
        len(order_items_df)
    )

    existing_orders = set()

    try:

        conn = get_connection()

        existing_order_df = pd.read_sql_query(
            "SELECT order_id FROM orders",
            conn
        )

        conn.close()

        existing_orders = set(
            existing_order_df["order_id"].astype(str).tolist()
        )

    except Exception as e:

        st.error(
            f"Unable to inspect existing orders: {e}"
        )

        return

    uploaded_order_ids = set(
        orders_df["order_id"].astype(str).tolist()
    ) if "order_id" in orders_df.columns else set()

    new_order_count = len(
        uploaded_order_ids - existing_orders
    )

    existing_order_count = len(
        uploaded_order_ids & existing_orders
    )

    col3.metric(
        "New Orders in Batch",
        new_order_count
    )

    st.caption(
        f"{existing_order_count} uploaded order(s) already exist in SQLite and will be updated by order_id if re-sent. "
        f"No existing orders will be deleted."
    )

    st.subheader(
        "3. Validate Batch"
    )

    orders_validation = validate_orders_dataframe(
        orders_df
    )

    order_items_validation = validate_order_items_dataframe(
        order_items_df
    )

    validation_errors = []

    validation_warnings = []

    validation_errors.extend(
        orders_validation.get("errors", [])
    )

    validation_errors.extend(
        order_items_validation.get("errors", [])
    )

    validation_warnings.extend(
        orders_validation.get("warnings", [])
    )

    validation_warnings.extend(
        order_items_validation.get("warnings", [])
    )

    if not orders_df.empty and "order_id" in orders_df.columns:

        item_order_ids = set(
            order_items_df["order_id"].astype(str).tolist()
        ) if "order_id" in order_items_df.columns else set()

        valid_item_order_ids = (
            uploaded_order_ids | existing_orders
        )

        orphan_item_orders = sorted(
            item_order_ids - valid_item_order_ids
        )

        if orphan_item_orders:

            validation_errors.append(
                "Order items reference order_id values that are neither in the uploaded orders batch "
                "nor already present in SQLite: "
                + ", ".join(orphan_item_orders[:20])
            )

    if validation_errors:

        st.error(
            "Marketplace batch validation failed."
        )

        for error in validation_errors:

            st.write(
                f"• {error}"
            )

    else:

        st.success(
            "Marketplace batch validation passed."
        )

    if validation_warnings:

        st.warning(
            "Warnings:"
        )

        for warning in validation_warnings:

            st.write(
                f"• {warning}"
            )

    st.subheader(
        "4. Preview"
    )

    col1, col2 = st.columns(2)

    with col1:

        st.write("**Marketplace Orders**")

        st.dataframe(
            orders_df.head(20),
            use_container_width=True,
            hide_index=True
        )

    with col2:

        st.write("**Marketplace Order Items**")

        st.dataframe(
            order_items_df.head(20),
            use_container_width=True,
            hide_index=True
        )


    st.divider()

    with st.expander("Reset Marketplace Order Data", expanded=False):
        st.warning(
            "This restores the baseline marketplace orders and order items and "
            "clears order-dependent operational records, including reservations, "
            "picks, fulfillment events, transfers and supply requests."
        )
        confirm_reset = st.checkbox(
            "I understand this will reset marketplace operational data.",
            key="confirm_marketplace_reset",
        )
        if confirm_reset:
            phrase = st.text_input(
                "Type RESET ORDERS to confirm",
                key="reset_orders_phrase",
            )
            if st.button(
                "Reset Marketplace Orders & Items",
                type="secondary",
                key="reset_marketplace_orders_button",
                disabled=(phrase.strip().upper() != "RESET ORDERS"),
            ):
                try:
                    result = clear_marketplace_operational_data()
                    reconcile_supply_requests_after_core_reset()
                    st.success(
                        f"Marketplace data reset completed. Fresh dataset ready. "
                        f"{result['orders']} orders and {result['order_items']} order items."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Marketplace reset failed: {e}")

    st.subheader(
        "5. Add Batch to Live Order History"
    )

    if validation_errors:

        st.warning(
            "Import is disabled because the marketplace batch failed validation."
        )

        return

    confirm_batch_import = st.checkbox(
        "I have reviewed the validation results and approve adding this marketplace batch to the existing order history.",
        key="confirm_marketplace_batch_import"
    )

    if confirm_batch_import:

        if st.button(
            "Import Marketplace Order Batch",
            type="primary",
            key="marketplace_batch_import_button"
        ):

            try:

                batch_result = import_marketplace_order_batch(
                    orders_df,
                    order_items_df,
                )

                synchronize_supply_requests_with_live_state()

                st.session_state["marketplace_batch_import_message"] = (
                    f"Marketplace batch imported successfully: "
                    f"{new_order_count} new order(s), "
                    f"{existing_order_count} existing order(s) refreshed, "
                    f"{len(order_items_df)} order item row(s) processed. "
                    "The live fulfillment calculations have been refreshed from the cumulative order history."
                )

                st.rerun()

            except Exception as e:

                st.error(
                    f"Marketplace batch import failed: {e}"
                )


def show_import_center():

    st.title(
        "Import Center"
    )

    st.caption(
        "Upload operational data, validate it and safely update the fulfillment database."
    )

    pending_message = st.session_state.pop(
        "marketplace_batch_import_message",
        None
    )

    if pending_message:

        st.success(
            pending_message
        )

    st.info(
        "Import behavior: Orders are cumulative. Each new marketplace batch is added to the existing order history; "
        "an order_id already in SQLite is refreshed. Order items are NOT cumulative: for every order_id in the "
        "uploaded batch, its previous line-item set is replaced by the uploaded line-item set. Inventory CSV "
        "uploads are also NOT cumulative; they are treated as target-state reconciliation against live inventory."
    )

    # --------------------------------------------------------
    # CURRENT DATABASE COUNTS
    # --------------------------------------------------------

    st.subheader(
        "Current Database"
    )

    counts = get_database_counts()

    col1, col2, col3, col4, col5 = (
        st.columns(5)
    )

    col1.metric(
        "Orders",
        counts["orders"]
    )

    col2.metric(
        "Order Items",
        counts["order_items"]
    )

    col3.metric(
        "Products",
        counts["products"]
    )

    col4.metric(
        "Warehouses",
        counts["warehouses"]
    )

    col5.metric(
        "Inventory",
        counts["inventory"]
    )

    st.divider()

    # --------------------------------------------------------
    # CORE DATA RESET
    # --------------------------------------------------------

    st.subheader(
        "Core Data Reset"
    )

    st.write(
        "Use these controls to permanently clear the selected operational data. "
        "After reset, upload a new CSV to establish a fresh dataset. Products and warehouses are not reset."
    )

    reset_col1, reset_col2 = st.columns(2)

    with reset_col1:

        st.markdown(
            "**Reset Orders & Items (Start Fresh)**"
        )

        st.caption(
            "Completely clears all current orders and order items, plus "
            "order-dependent events, transfers, inventory transactions and supply requests. "
            "Physical inventory rows are preserved, but reservations are cleared. "
            "The next Orders CSV starts fresh."
        )

        confirm_orders_reset = st.checkbox(
            "I understand that all current orders and order items will be permanently cleared.",
            key="confirm_orders_items_reset",
        )

        if confirm_orders_reset:

            if st.button(
                "Reset Orders & Items",
                type="secondary",
                key="reset_orders_items_button",
            ):

                try:

                    result = reset_orders_and_items()
                    reconcile_supply_requests_after_core_reset()

                    st.success(
                        "Marketplace data cleared successfully. "
                        f"Removed {result['orders']} orders and {result['order_items']} order items. "
                        "The next Orders CSV will start fresh."
                    )

                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Orders & items reset failed: {e}"
                    )

    with reset_col2:

        st.markdown(
            "**Reset Inventory (Start Fresh)**"
        )

        st.caption(
            "Completely clears all current inventory rows and inventory-dependent transactions, "
            "transfers and supply requests. Products, warehouses, orders and order items remain. "
            "The next Inventory CSV starts fresh."
        )

        confirm_inventory_reset = st.checkbox(
            "I understand that all current inventory data will be permanently cleared.",
            key="confirm_inventory_reset",
        )

        if confirm_inventory_reset:

            if st.button(
                "Reset Inventory",
                type="secondary",
                key="reset_inventory_button",
            ):

                try:

                    result = reset_inventory_to_baseline()
                    reconcile_supply_requests_after_core_reset()

                    st.success(
                        "Inventory cleared successfully. "
                        f"Removed {result['inventory']} inventory rows. "
                        "The next Inventory CSV will start fresh."
                    )

                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Inventory reset failed: {e}"
                    )

    st.divider()


    # --------------------------------------------------------
    # WORKFLOW TYPE
    # --------------------------------------------------------

    workflow = st.radio(
        "Import Workflow",
        [
            "Marketplace Order Batch",
            "Master / Reference Data",
            "Inventory Reconciliation"
        ],
        horizontal=True
    )

    # ========================================================
    # MARKETPLACE ORDER BATCH
    # ========================================================

    if workflow == "Marketplace Order Batch":

        show_marketplace_order_batch_import()

    # ========================================================
    # MASTER / REFERENCE DATA
    # ========================================================

    elif workflow == "Master / Reference Data":

        st.subheader(
            "1. Select Data Type"
        )

        data_type_labels = {
            "Products": "products",
            "Warehouses": "warehouses",
        }

        selected_label = st.selectbox(
            "Data Type",
            list(
                data_type_labels.keys()
            )
        )

        data_type = (
            data_type_labels[
                selected_label
            ]
        )


        with st.expander(f"Reset {selected_label} Dataset", expanded=False):
            st.warning(
                "This permanently deletes the current records for this master "
                "dataset. The reset is blocked if live operational records depend on it."
            )
            confirm_master = st.checkbox(
                f"I understand that all current {selected_label.lower()} records will be deleted.",
                key=f"confirm_master_reset_{data_type}",
            )
            if confirm_master:
                phrase = st.text_input(
                    f"Type RESET {selected_label.upper()} to confirm",
                    key=f"reset_master_phrase_{data_type}",
                )
                if st.button(
                    f"Clear All {selected_label}",
                    type="secondary",
                    key=f"clear_master_{data_type}",
                    disabled=(phrase.strip().upper() != f"RESET {selected_label.upper()}"),
                ):
                    try:
                        affected = clear_master_dataset(data_type)
                        st.success(
                            f"{selected_label} reset completed. Deleted {affected} record(s)."
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"{selected_label} reset failed: {e}")

        st.subheader(
            "2. Upload CSV"
        )

        uploaded_file = st.file_uploader(
            "Choose a CSV file",
            type=["csv"],
            key="master_upload"
        )

        if uploaded_file is None:

            st.info(
                "Upload a CSV file to begin validation."
            )

            return

        try:

            uploaded_df = pd.read_csv(
                uploaded_file
            )

        except Exception as e:

            st.error(
                f"Unable to read CSV file: {e}"
            )

            return

        st.success(
            f"File loaded successfully: "
            f"{uploaded_file.name}"
        )

        col1, col2 = st.columns(2)

        col1.metric(
            "Rows",
            len(uploaded_df)
        )

        col2.metric(
            "Columns",
            len(uploaded_df.columns)
        )

        st.subheader(
            "3. Validate File"
        )

        if data_type == "products":

            validation = (
                validate_products_dataframe(
                    uploaded_df
                )
            )

        elif data_type == "warehouses":

            validation = (
                validate_warehouses_dataframe(
                    uploaded_df
                )
            )

        else:

            validation = {
                "valid": False,
                "errors": [
                    "Unsupported data type."
                ],
                "warnings": []
            }

        if validation["valid"]:

            st.success(
                "Validation passed."
            )

        else:

            st.error(
                "Validation failed."
            )

        if validation.get("errors"):

            st.error(
                "Errors found:"
            )

            for error in validation[
                "errors"
            ]:

                st.write(
                    f"• {error}"
                )

        if validation.get("warnings"):

            st.warning(
                "Warnings:"
            )

            for warning in validation[
                "warnings"
            ]:

                st.write(
                    f"• {warning}"
                )

        st.subheader(
            "4. Preview"
        )

        st.dataframe(
            uploaded_df.head(20),
            use_container_width=True,
            hide_index=True
        )

        st.subheader(
            "5. Import"
        )

        if not validation["valid"]:

            st.warning(
                "Import is disabled because validation failed."
            )

            return

        confirm_import = st.checkbox(
            "I have reviewed the validation results and preview.",
            key="confirm_master_import"
        )

        if confirm_import:

            if st.button(
                f"Import {selected_label}",
                type="primary",
                key="master_import_button"
            ):

                try:

                    affected_rows = (
                        import_dataframe(
                            uploaded_df,
                            data_type
                        )
                    )

                    st.success(
                        f"Import completed successfully. "
                        f"Processed {affected_rows} row(s)."
                    )

                    st.success(
                        "The live database has been refreshed. Other pages will recalculate from the updated SQLite state on their next render."
                    )
                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Import failed: {e}"
                    )

    # ========================================================
    # INVENTORY RECONCILIATION
    # ========================================================

    else:

        show_inventory_csv_reconciliation()





def show_inventory_csv_reconciliation():
    """Reconcile an uploaded inventory snapshot against live SQLite inventory.

    This is intentionally a reconciliation workflow, not a stock-receipt workflow.
    It validates the CSV, previews differences, blocks reductions below reserved
    quantities, and applies approved differences through the inventory transaction
    layer so every other page sees the same committed live state.
    """


    st.subheader(
        "Inventory Reconciliation"
    )

    st.write(
        "Use this workflow for an external inventory snapshot or physical stock count "
        "that needs to be compared against the live SQLite inventory. "
        "This is a reconciliation workflow, not a stock-receiving workflow."
    )

    st.warning(
        "Inventory Upload does not add uploaded quantities to current stock. "
        "The system compares the uploaded stock position with live SQLite inventory, "
        "shows the differences, and changes inventory only after explicit approval. "
        "For newly received physical stock, use Inventory → Warehouse Operations → Receive Stock."
    )


    with st.expander("Reset Inventory Dataset", expanded=False):
        st.warning(
            "This replaces the current inventory quantities with the baseline "
            "inventory snapshot. Products and Warehouses are not deleted. "
            "Inventory history is retained for auditability."
        )
        confirm_inventory = st.checkbox(
            "I understand that all current inventory data will be permanently cleared.",
            key="confirm_inventory_csv_reset",
        )
        if confirm_inventory:
            phrase = st.text_input(
                "Type RESET INVENTORY to confirm",
                key="reset_inventory_csv_phrase",
            )
            if st.button(
                "Reset Inventory Dataset",
                type="secondary",
                key="reset_inventory_csv_button",
                disabled=(phrase.strip().upper() != "RESET INVENTORY"),
            ):
                try:
                    result = reset_inventory_to_baseline()
                    reconcile_supply_requests_after_core_reset()
                    st.success(
                        f"Inventory cleared successfully. Removed {result['inventory']} inventory rows. The next Inventory CSV will start fresh."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Inventory reset failed: {e}")

    st.subheader(
        "1. Upload Inventory CSV"
    )

    inventory_file = st.file_uploader(
        "Choose inventory CSV",
        type=["csv"],
        key="inventory_upload"
    )

    if inventory_file is None:

        st.info(
            "Upload an inventory CSV to begin reconciliation."
        )

        return

    try:

        inventory_df = pd.read_csv(
            inventory_file
        )

    except Exception as e:

        st.error(
            f"Unable to read inventory CSV: {e}"
        )

        return

    st.success(
        f"File loaded successfully: "
        f"{inventory_file.name}"
    )

    col1, col2 = st.columns(2)

    col1.metric(
        "Uploaded Rows",
        len(inventory_df)
    )

    col2.metric(
        "Uploaded Columns",
        len(inventory_df.columns)
    )

    st.subheader(
        "2. Validate Inventory"
    )

    validation = (
        validate_inventory_reconciliation(
            inventory_df
        )
    )

    if validation["valid"]:

        st.success(
            "Inventory reconciliation validation passed."
        )

    else:

        st.error(
            "Inventory reconciliation validation failed."
        )

    if validation.get("errors"):

        st.error(
            "Errors:"
        )

        for error in validation[
            "errors"
        ]:

            st.write(
                f"• {error}"
            )

    if validation.get("warnings"):

        st.warning(
            "Warnings:"
        )

        for warning in validation[
            "warnings"
        ]:

            st.write(
                f"• {warning}"
            )

    if not validation["valid"]:

        return

    # ----------------------------------------------------
    # COMPARISON
    # ----------------------------------------------------

    st.subheader(
        "3. Compare With Live Inventory"
    )

    try:

        comparison = (
            preview_inventory_reconciliation(
                inventory_df
            )
        )

    except Exception as e:

        st.error(
            f"Unable to create inventory reconciliation preview: {e}"
        )

        return

    # Normalize the reconciliation delta defensively. Earlier helper versions
    # may return "difference", "change_quantity", or only the quantity columns.
    if "change_quantity" not in comparison.columns:
        if "difference" in comparison.columns:
            comparison["change_quantity"] = comparison["difference"]
        elif {"uploaded_quantity", "current_quantity"}.issubset(comparison.columns):
            comparison["change_quantity"] = (
                pd.to_numeric(comparison["uploaded_quantity"], errors="coerce").fillna(0)
                - pd.to_numeric(comparison["current_quantity"], errors="coerce").fillna(0)
            )
        else:
            comparison["change_quantity"] = 0

    comparison["change_quantity"] = pd.to_numeric(
        comparison["change_quantity"], errors="coerce"
    ).fillna(0)

    if "blocked" not in comparison.columns:
        if "available_after_adjustment" in comparison.columns:
            comparison["blocked"] = (
                pd.to_numeric(comparison["available_after_adjustment"], errors="coerce")
                < 0
            ).fillna(False)
        else:
            comparison["blocked"] = False

    st.dataframe(
        comparison,
        use_container_width=True,
        hide_index=True
    )

    changed_rows = comparison[
        comparison["change_quantity"] != 0
    ].copy()

    increases = changed_rows[
        changed_rows["change_quantity"] > 0
    ].copy()

    decreases = changed_rows[
        changed_rows["change_quantity"] < 0
    ].copy()

    blocked = comparison[
        comparison["blocked"] == True
    ].copy()

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Rows Compared",
        len(comparison)
    )

    col2.metric(
        "Changes",
        len(changed_rows)
    )

    col3.metric(
        "Quantity Increase Rows",
        len(increases)
    )

    col4.metric(
        "Quantity Decrease Rows",
        len(decreases)
    )

    if not blocked.empty:

        st.error(
            f"{len(blocked)} row(s) would reduce inventory "
            "below reserved quantity."
        )

        st.dataframe(
            blocked,
            use_container_width=True,
            hide_index=True
        )

        st.warning(
            "These rows cannot be applied until the uploaded "
            "quantity is corrected."
        )

        return

    if changed_rows.empty:

        st.success(
            "No inventory changes are required."
        )

        return

    # ----------------------------------------------------
    # APPROVAL
    # ----------------------------------------------------

    st.subheader(
        "4. Apply Approved Adjustments"
    )

    st.warning(
        "The next action will change SQLite inventory "
        "and create ADJUSTMENT transactions."
    )

    confirm_reconciliation = st.checkbox(
        "I have reviewed the differences and approve these inventory adjustments.",
        key="confirm_inventory_reconciliation"
    )

    if confirm_reconciliation:

        if st.button(
            "Apply Inventory Adjustments",
            type="primary",
            key="apply_inventory_reconciliation"
        ):

            try:

                result = (
                    apply_inventory_reconciliation(
                        comparison,
                        reference_id=(
                            "CSV_RECONCILIATION_"
                            + inventory_file.name
                        )
                    )
                )

                st.success(
                    "Inventory reconciliation completed."
                )

                col1, col2 = st.columns(2)

                col1.metric(
                    "Updated",
                    result["updated"]
                )

                col2.metric(
                    "Skipped",
                    result["skipped"]
                )

                if result[
                    "details"
                ]:

                    details_df = pd.DataFrame(
                        result[
                            "details"
                        ]
                    )

                    st.dataframe(
                        details_df,
                        use_container_width=True,
                        hide_index=True
                    )

                synchronize_supply_requests_with_live_state()

                st.info(
                    "Inventory transactions were recorded for the applied adjustments. Live inventory, planning, alerts and supply-request status will be recalculated on the refreshed page."
                )
                st.rerun()

            except Exception as e:

                st.error(
                    f"Inventory reconciliation failed: {e}"
                )

# ============================================================
# CONSOLIDATED FINAL NAVIGATION PAGES
# ============================================================

def show_inventory_page():
    """Live inventory hub plus all warehouse-owned inventory workflows."""
    _show_inventory_overview_page()

    st.divider()
    with st.expander("Warehouse Operations", expanded=False):
        show_inventory_actions_page(
            allowed_actions=["Receive Stock", "Stock Count"]
        )

    st.divider()
    with st.expander("Inventory CSV Reconciliation", expanded=False):
        st.caption(
            "Upload an external inventory snapshot, compare it with the live SQLite "
            "position, review the differences, and explicitly approve any adjustments. "
            "This does not silently overwrite or add quantities."
        )
        show_inventory_csv_reconciliation()


def show_supply_planning_page():
    """Live shortage/replenishment planning plus supply lifecycle."""
    _show_supply_planning_overview_page()

    st.divider()
    with st.expander("Supply Request Management", expanded=False):
        show_supply_requests_page()


def show_live_refresh_control():
    """Explicit live-data refresh for multi-operator usage."""
    if st.sidebar.button("Refresh Live Data", key="global_live_refresh"):
        st.rerun()

    st.sidebar.caption(
        "Operational pages read SQLite on each run. "
        "Successful actions rerun the page so related views use the new state."
    )


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title(
    "Fulfillment Control Tower"
)

requested_page = st.session_state.pop(
    "requested_navigation_page",
    None
)

if requested_page is not None:
    st.session_state["navigation_page"] = requested_page

elif "navigation_page" not in st.session_state:
    st.session_state["navigation_page"] = "Dashboard"

page = st.sidebar.radio(
    "Navigation",
    [
        "Dashboard",
        "Order Flow",
        "Inventory",
        "Inventory Actions",
        "Transfer Queue",
        "Supply Planning",
        "Inventory History",
        "Import Center",
    ],
    key="navigation_page",
)

st.sidebar.divider()

st.sidebar.caption(
    "XYZ E-commerce Fulfillment Operations"
)


# ============================================================
# PAGE ROUTING
# ============================================================

show_live_refresh_control()

if page == "Dashboard":

    show_dashboard()

elif page == "Order Flow":

    show_order_flow_page()

elif page == "Inventory":

    show_inventory_page()

elif page == "Inventory Actions":

    show_inventory_actions_page()

elif page == "Transfer Queue":

    show_transfer_queue_page()

elif page == "Supply Planning":

    show_supply_planning_page()

elif page == "Inventory History":

    show_inventory_history_page()

elif page == "Import Center":

    show_import_center()


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "Fulfillment Control Tower | Operational Analytics & Workflow Management"
)