import sqlite3
from pathlib import Path

import pandas as pd


DB_PATH = "fulfillment.db"


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    return sqlite3.connect(DB_PATH)


# ============================================================
# EXPECTED CSV SCHEMAS
# ============================================================

ORDERS_COLUMNS = [
    "order_id",
    "marketplace_order_id",
    "order_date",
    "channel",
    "customer_name",
    "shipping_city",
    "shipping_state",
    "pincode",
    "priority",
    "priority_reason",
    "promised_ship_by",
    "promised_delivery_date",
    "payment_status",
    "order_status",
    "total_items",
    "order_value",
    "customer_type",
]


ORDER_ITEMS_COLUMNS = [
    "order_id",
    "line_item_id",
    "sku",
    "product_name",
    "variant",
    "quantity",
    "unit_price",
    "line_total",
]


PRODUCT_COLUMNS = [
    "sku",
    "product_id",
    "product_name",
    "category",
    "brand",
    "variant",
    "size",
    "color",
    "unit_price",
    "unit_weight_kg",
    "active",
]


WAREHOUSE_COLUMNS = [
    "warehouse_id",
    "warehouse_name",
    "warehouse_type",
    "location",
    "capacity_status",
    "active",
]


INVENTORY_COLUMNS = [
    "inventory_id",
    "warehouse_id",
    "sku",
    "bin_location",
    "system_quantity",
    "reserved_quantity",
    "available_quantity",
    "last_verified_at",
    "inventory_status",
    "scenario",
]


# ============================================================
# FILE VALIDATION / LOADING
# ============================================================

def validate_file_exists(file_path):

    path = Path(file_path)

    if not path.exists():

        return {
            "valid": False,
            "errors": [
                f"File not found: {file_path}"
            ],
        }

    if not path.is_file():

        return {
            "valid": False,
            "errors": [
                f"Path is not a file: {file_path}"
            ],
        }

    return {
        "valid": True,
        "errors": [],
    }


def load_csv(file_path):

    result = validate_file_exists(
        file_path
    )

    if not result["valid"]:

        raise FileNotFoundError(
            result["errors"][0]
        )

    return pd.read_csv(
        file_path
    )


def validate_required_columns(
    df,
    required_columns
):

    actual_columns = set(
        df.columns
    )

    missing_columns = [
        column
        for column in required_columns
        if column not in actual_columns
    ]

    if missing_columns:

        return {
            "valid": False,
            "errors": [
                "Missing required columns: "
                + ", ".join(
                    missing_columns
                )
            ],
        }

    return {
        "valid": True,
        "errors": [],
    }


def validate_duplicate_values(
    df,
    column_name
):

    if column_name not in df.columns:

        return {
            "valid": False,
            "errors": [
                f"Column not found: {column_name}"
            ],
        }

    duplicates = (
        df[
            df[column_name].duplicated(
                keep=False
            )
        ][column_name]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    if duplicates:

        return {
            "valid": False,
            "errors": [
                f"Duplicate {column_name} values found: "
                + ", ".join(
                    duplicates[:20]
                )
            ],
            "duplicate_count": len(
                duplicates
            ),
        }

    return {
        "valid": True,
        "errors": [],
        "duplicate_count": 0,
    }


# ============================================================
# ORDERS VALIDATION
# ============================================================

def validate_orders_dataframe(df):

    errors = []
    warnings = []

    result = validate_required_columns(
        df,
        ORDERS_COLUMNS
    )

    if not result["valid"]:

        errors.extend(
            result["errors"]
        )

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "row_count": len(df),
        }

    duplicate_result = (
        validate_duplicate_values(
            df,
            "order_id"
        )
    )

    if not duplicate_result["valid"]:

        errors.extend(
            duplicate_result["errors"]
        )

    required_text_columns = [
        "order_id",
        "customer_name",
        "channel",
        "shipping_city",
        "shipping_state",
        "pincode",
    ]

    for column in required_text_columns:

        missing_count = (
            df[column].isna().sum()
        )

        if missing_count > 0:

            errors.append(
                f"{column} contains "
                f"{missing_count} missing value(s)."
            )

    for column in [
        "order_date",
        "promised_ship_by",
        "promised_delivery_date",
    ]:

        parsed_dates = pd.to_datetime(
            df[column],
            errors="coerce"
        )

        invalid_count = (
            parsed_dates.isna().sum()
        )

        if invalid_count > 0:

            errors.append(
                f"{column} contains "
                f"{invalid_count} invalid date(s)."
            )

    valid_priorities = {
        "Normal",
        "High",
        "Critical",
    }

    invalid_priorities = (
        set(
            df["priority"]
            .dropna()
            .unique()
        )
        - valid_priorities
    )

    if invalid_priorities:

        errors.append(
            "Invalid priority values: "
            + ", ".join(
                map(
                    str,
                    invalid_priorities
                )
            )
        )

    if df["payment_status"].isna().sum() > 0:

        warnings.append(
            "Some orders do not have a payment status."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "row_count": len(df),
    }


# ============================================================
# ORDER ITEMS VALIDATION
# ============================================================

def validate_order_items_dataframe(df):

    errors = []
    warnings = []

    result = validate_required_columns(
        df,
        ORDER_ITEMS_COLUMNS
    )

    if not result["valid"]:

        errors.extend(
            result["errors"]
        )

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "row_count": len(df),
        }

    duplicate_result = (
        validate_duplicate_values(
            df,
            "line_item_id"
        )
    )

    if not duplicate_result["valid"]:

        errors.extend(
            duplicate_result["errors"]
        )

    if df["order_id"].isna().sum() > 0:

        errors.append(
            "Some order items have missing order_id."
        )

    if df["sku"].isna().sum() > 0:

        errors.append(
            "Some order items have missing SKU."
        )

    quantity_values = pd.to_numeric(
        df["quantity"],
        errors="coerce"
    )

    invalid_quantity = (
        quantity_values.isna()
        |
        (quantity_values <= 0)
    ).sum()

    if invalid_quantity > 0:

        errors.append(
            f"{invalid_quantity} order item(s) "
            "have invalid quantities."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "row_count": len(df),
    }


# ============================================================
# PRODUCT VALIDATION
# ============================================================

def validate_products_dataframe(df):

    errors = []
    warnings = []

    result = validate_required_columns(
        df,
        PRODUCT_COLUMNS
    )

    if not result["valid"]:

        errors.extend(
            result["errors"]
        )

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "row_count": len(df),
        }

    duplicate_result = (
        validate_duplicate_values(
            df,
            "sku"
        )
    )

    if not duplicate_result["valid"]:

        errors.extend(
            duplicate_result["errors"]
        )

    if df["sku"].isna().sum() > 0:

        errors.append(
            "Some products have missing SKU."
        )

    if df["product_name"].isna().sum() > 0:

        errors.append(
            "Some products have missing product_name."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "row_count": len(df),
    }


# ============================================================
# WAREHOUSE VALIDATION
# ============================================================

def validate_warehouses_dataframe(df):

    errors = []
    warnings = []

    result = validate_required_columns(
        df,
        WAREHOUSE_COLUMNS
    )

    if not result["valid"]:

        errors.extend(
            result["errors"]
        )

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "row_count": len(df),
        }

    duplicate_result = (
        validate_duplicate_values(
            df,
            "warehouse_id"
        )
    )

    if not duplicate_result["valid"]:

        errors.extend(
            duplicate_result["errors"]
        )

    if df["warehouse_id"].isna().sum() > 0:

        errors.append(
            "Some warehouses have missing warehouse_id."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "row_count": len(df),
    }


# ============================================================
# INVENTORY FILE VALIDATION
# ============================================================

def validate_inventory_dataframe(df):

    errors = []
    warnings = []

    result = validate_required_columns(
        df,
        INVENTORY_COLUMNS
    )

    if not result["valid"]:

        errors.extend(
            result["errors"]
        )

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "row_count": len(df),
        }

    duplicate_result = (
        validate_duplicate_values(
            df,
            "inventory_id"
        )
    )

    if not duplicate_result["valid"]:

        errors.extend(
            duplicate_result["errors"]
        )

    numeric_columns = [
        "system_quantity",
        "reserved_quantity",
        "available_quantity",
    ]

    numeric_data = {}

    for column in numeric_columns:

        values = pd.to_numeric(
            df[column],
            errors="coerce"
        )

        numeric_data[column] = values

        invalid_count = values.isna().sum()

        if invalid_count > 0:

            errors.append(
                f"{column} contains "
                f"{invalid_count} invalid value(s)."
            )

        if (values < 0).any():

            errors.append(
                f"{column} contains negative quantities."
            )

    if all(
        column in df.columns
        for column in numeric_columns
    ):

        expected_available = (
            numeric_data["system_quantity"]
            -
            numeric_data["reserved_quantity"]
        )

        mismatch_count = (
            expected_available
            != numeric_data[
                "available_quantity"
            ]
        ).sum()

        if mismatch_count > 0:

            errors.append(
                f"{mismatch_count} inventory row(s) "
                "have available_quantity inconsistent "
                "with system_quantity - reserved_quantity."
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "row_count": len(df),
    }


# ============================================================
# ORDER ITEM DATABASE REFERENCE CHECK
# ============================================================

def validate_order_items_against_database(
    df
):

    conn = get_connection()

    orders_db = pd.read_sql_query(
        "SELECT order_id FROM orders",
        conn
    )

    products_db = pd.read_sql_query(
        "SELECT sku FROM products",
        conn
    )

    conn.close()

    order_ids = set(
        orders_db[
            "order_id"
        ].astype(str)
    )

    skus = set(
        products_db[
            "sku"
        ].astype(str)
    )

    uploaded_order_ids = set(
        df[
            "order_id"
        ]
        .dropna()
        .astype(str)
    )

    uploaded_skus = set(
        df[
            "sku"
        ]
        .dropna()
        .astype(str)
    )

    missing_orders = sorted(
        uploaded_order_ids
        - order_ids
    )

    missing_skus = sorted(
        uploaded_skus
        - skus
    )

    warnings = []

    if missing_orders:

        warnings.append(
            f"{len(missing_orders)} order ID(s) "
            "are not currently present in the database."
        )

    if missing_skus:

        warnings.append(
            f"{len(missing_skus)} SKU(s) "
            "are not currently present in the product master."
        )

    return {
        "valid": True,
        "warnings": warnings,
        "missing_orders": missing_orders,
        "missing_skus": missing_skus,
    }


# ============================================================
# INVENTORY RECONCILIATION
# ============================================================

def validate_inventory_reconciliation(
    df
):
    """
    Read-only.

    Validates the uploaded inventory file specifically
    for reconciliation against the live SQLite inventory.
    """

    errors = []
    warnings = []

    result = validate_inventory_dataframe(
        df
    )

    errors.extend(
        result["errors"]
    )

    warnings.extend(
        result["warnings"]
    )

    if errors:

        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
        }

    conn = get_connection()

    current_inventory = pd.read_sql_query(
        """
        SELECT
            inventory_id,
            warehouse_id,
            sku,
            bin_location,
            system_quantity,
            reserved_quantity,
            available_quantity
        FROM inventory
        """,
        conn
    )

    products = pd.read_sql_query(
        """
        SELECT sku
        FROM products
        """,
        conn
    )

    warehouses = pd.read_sql_query(
        """
        SELECT warehouse_id
        FROM warehouses
        WHERE active = 1
        """,
        conn
    )

    conn.close()

    current_keys = set(
        zip(
            current_inventory[
                "warehouse_id"
            ].astype(str),
            current_inventory[
                "sku"
            ].astype(str)
        )
    )

    uploaded_keys = set(
        zip(
            df[
                "warehouse_id"
            ].astype(str),
            df[
                "sku"
            ].astype(str)
        )
    )

    valid_skus = set(
        products[
            "sku"
        ].astype(str)
    )

    valid_warehouses = set(
        warehouses[
            "warehouse_id"
        ].astype(str)
    )

    unknown_skus = sorted(
        set(
            df[
                "sku"
            ].astype(str)
        )
        - valid_skus
    )

    unknown_warehouses = sorted(
        set(
            df[
                "warehouse_id"
            ].astype(str)
        )
        - valid_warehouses
    )

    if unknown_skus:

        errors.append(
            "Unknown SKU(s): "
            + ", ".join(
                unknown_skus[:20]
            )
        )

    if unknown_warehouses:

        errors.append(
            "Unknown or inactive warehouse(s): "
            + ", ".join(
                unknown_warehouses[:20]
            )
        )

    new_inventory_locations = (
        uploaded_keys
        - current_keys
    )

    if new_inventory_locations:

        warnings.append(
            f"{len(new_inventory_locations)} "
            "warehouse/SKU combination(s) "
            "do not currently exist in SQLite."
        )

    duplicate_pairs = (
        df.groupby(
            [
                "warehouse_id",
                "sku"
            ]
        )
        .size()
        .reset_index(
            name="row_count"
        )
    )

    duplicate_pairs = duplicate_pairs[
        duplicate_pairs[
            "row_count"
        ] > 1
    ]

    if not duplicate_pairs.empty:

        errors.append(
            "Duplicate warehouse/SKU combinations "
            "found in uploaded inventory file."
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def preview_inventory_reconciliation(
    df
):
    """
    Read-only.

    Compares uploaded physical/system quantities
    with the current SQLite quantity.

    Returns a DataFrame showing differences.
    """

    validation = (
        validate_inventory_reconciliation(
            df
        )
    )

    if not validation["valid"]:

        raise ValueError(
            "Inventory reconciliation validation failed: "
            + " | ".join(
                validation["errors"]
            )
        )

    conn = get_connection()

    current_inventory = pd.read_sql_query(
        """
        SELECT
            inventory_id,
            warehouse_id,
            sku,
            bin_location,
            system_quantity AS current_quantity,
            reserved_quantity,
            available_quantity
        FROM inventory
        """,
        conn
    )

    products = pd.read_sql_query(
        """
        SELECT
            sku,
            product_name
        FROM products
        """,
        conn
    )

    conn.close()

    uploaded = df[
        [
            "warehouse_id",
            "sku",
            "system_quantity",
            "bin_location",
            "last_verified_at",
        ]
    ].copy()

    uploaded = uploaded.rename(
        columns={
            "system_quantity":
                "uploaded_quantity",
            "bin_location":
                "uploaded_bin_location",
            "last_verified_at":
                "uploaded_verified_at",
        }
    )

    comparison = uploaded.merge(
        current_inventory,
        on=[
            "warehouse_id",
            "sku"
        ],
        how="left"
    )

    comparison = comparison.merge(
        products,
        on="sku",
        how="left"
    )

    comparison[
        "current_quantity"
    ] = pd.to_numeric(
        comparison[
            "current_quantity"
        ],
        errors="coerce"
    )

    comparison[
        "uploaded_quantity"
    ] = pd.to_numeric(
        comparison[
            "uploaded_quantity"
        ],
        errors="coerce"
    )

    comparison[
        "difference"
    ] = (
        comparison[
            "uploaded_quantity"
        ]
        -
        comparison[
            "current_quantity"
        ]
    )

    comparison["difference"] = comparison["difference"].fillna(0)

    comparison[
        "difference"
    ] = comparison[
        "difference"
    ].fillna(
        comparison[
            "uploaded_quantity"
        ]
    )

    # Canonical reconciliation delta used by the UI and approval workflow.
    # Keep "difference" for backward compatibility with existing code, while
    # exposing "change_quantity" as the explicit operational name.
    comparison["change_quantity"] = comparison["difference"]

    comparison[
        "status"
    ] = "NO CHANGE"

    comparison.loc[
        comparison[
            "current_quantity"
        ].isna(),
        "status"
    ] = "NEW LOCATION"

    comparison.loc[
        (
            comparison[
                "current_quantity"
            ].notna()
        )
        &
        (
            comparison[
                "difference"
            ] > 0
        ),
        "status"
    ] = "INCREASE"

    comparison.loc[
        (
            comparison[
                "current_quantity"
            ].notna()
        )
        &
        (
            comparison[
                "difference"
            ] < 0
        ),
        "status"
    ] = "DECREASE"

    comparison[
        "reserved_quantity"
    ] = comparison[
        "reserved_quantity"
    ].fillna(0)

    comparison[
        "available_after_adjustment"
    ] = (
        comparison[
            "uploaded_quantity"
        ]
        -
        comparison[
            "reserved_quantity"
        ]
    )

    comparison.loc[
        comparison[
            "available_after_adjustment"
        ] < 0,
        "status"
    ] = "BLOCKED — BELOW RESERVED"

    display_columns = [
        "warehouse_id",
        "sku",
        "product_name",
        "current_quantity",
        "uploaded_quantity",
        "difference",
        "change_quantity",
        "reserved_quantity",
        "available_after_adjustment",
        "status",
    ]

    if "change_quantity" not in comparison.columns:
        if "difference" in comparison.columns:
            comparison["change_quantity"] = comparison["difference"]
        elif {"uploaded_quantity", "current_quantity"}.issubset(comparison.columns):
            comparison["change_quantity"] = (
                pd.to_numeric(comparison["uploaded_quantity"], errors="coerce").fillna(0)
                - pd.to_numeric(comparison["current_quantity"], errors="coerce").fillna(0)
            )
        else:
            comparison["change_quantity"] = pd.Series(dtype="int64")

    return comparison[
        display_columns
    ]


def apply_inventory_reconciliation(
    reconciliation_df,
    reference_id="INVENTORY_RECONCILIATION"
):
    """
    MUTATING OPERATION.

    Applies approved inventory quantity differences.

    Rules:
    - Existing inventory is adjusted.
    - Reserved quantity is never changed.
    - Available quantity is recalculated.
    - Every adjustment creates an inventory transaction.
    - Quantity cannot be adjusted below reserved quantity.
    - Unknown warehouse/SKU locations are not automatically created.
    """

    if reconciliation_df.empty:

        return {
            "updated": 0,
            "skipped": 0,
            "details": [],
        }

    conn = get_connection()

    cursor = conn.cursor()

    updated = 0
    skipped = 0

    details = []

    try:

        for _, row in reconciliation_df.iterrows():

            warehouse_id = str(
                row["warehouse_id"]
            )

            sku = str(
                row["sku"]
            )

            uploaded_quantity = int(
                row["uploaded_quantity"]
            )

            current = cursor.execute(
                """
                SELECT
                    inventory_id,
                    system_quantity,
                    reserved_quantity
                FROM inventory
                WHERE warehouse_id = ?
                  AND sku = ?
                """,
                (
                    warehouse_id,
                    sku,
                )
            ).fetchone()

            if current is None:
                # A true Inventory reset leaves the inventory table empty.
                # A validated CSV is allowed to establish new SKU/warehouse
                # inventory rows, but only for an existing active warehouse
                # and an existing product SKU.
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
                    skipped += 1
                    details.append(
                        {
                            "warehouse_id": warehouse_id,
                            "sku": sku,
                            "status": "SKIPPED — WAREHOUSE NOT FOUND OR INACTIVE",
                        }
                    )
                    continue

                product = cursor.execute(
                    """
                    SELECT sku
                    FROM products
                    WHERE sku = ?
                    """,
                    (sku,),
                ).fetchone()

                if product is None:
                    skipped += 1
                    details.append(
                        {
                            "warehouse_id": warehouse_id,
                            "sku": sku,
                            "status": "SKIPPED — SKU NOT FOUND",
                        }
                    )
                    continue

                if uploaded_quantity < 0:
                    raise ValueError(
                        f"Inventory quantity cannot be negative for {sku} "
                        f"in {warehouse_id}."
                    )

                # Create the inventory location as part of the same atomic
                # reconciliation transaction.
                bin_location = row.get("uploaded_bin_location")
                if pd.isna(bin_location):
                    bin_location = None

                available_quantity = uploaded_quantity
                inventory_status = (
                    "Available" if uploaded_quantity > 0 else "Shortage"
                )

                cursor.execute(
                    """
                    INSERT INTO inventory (
                        warehouse_id,
                        sku,
                        bin_location,
                        system_quantity,
                        reserved_quantity,
                        available_quantity,
                        inventory_status,
                        last_verified_at
                    )
                    VALUES (?, ?, ?, ?, 0, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        warehouse_id,
                        sku,
                        bin_location,
                        uploaded_quantity,
                        available_quantity,
                        inventory_status,
                    ),
                )

                inventory_id = cursor.lastrowid

                if uploaded_quantity != 0:
                    cursor.execute(
                        """
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
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            inventory_id,
                            sku,
                            warehouse_id,
                            "ADJUSTMENT",
                            uploaded_quantity,
                            reference_id,
                            "Initial inventory load after reset",
                        ),
                    )

                updated += 1
                details.append(
                    {
                        "warehouse_id": warehouse_id,
                        "sku": sku,
                        "old_quantity": 0,
                        "new_quantity": uploaded_quantity,
                        "difference": uploaded_quantity,
                        "status": "CREATED",
                    }
                )

                continue

            inventory_id = current[0]

            current_quantity = int(
                current[1]
            )

            reserved_quantity = int(
                current[2]
            )

            difference = (
                uploaded_quantity
                -
                current_quantity
            )

            if difference == 0:

                details.append(
                    {
                        "warehouse_id":
                            warehouse_id,
                        "sku":
                            sku,
                        "status":
                            "NO CHANGE",
                    }
                )

                continue

            if uploaded_quantity < reserved_quantity:

                raise ValueError(
                    f"Cannot adjust {sku} in "
                    f"{warehouse_id} to "
                    f"{uploaded_quantity}. "
                    f"Reserved quantity is "
                    f"{reserved_quantity}."
                )

            new_available = (
                uploaded_quantity
                -
                reserved_quantity
            )

            cursor.execute(
                """
                UPDATE inventory
                SET
                    system_quantity = ?,
                    available_quantity = ?,
                    inventory_status = ?,
                    last_verified_at = CURRENT_TIMESTAMP
                WHERE inventory_id = ?
                """,
                (
                    uploaded_quantity,
                    new_available,
                    "Available" if new_available > 0 else "Reserved" if reserved_quantity > 0 else "Shortage",
                    inventory_id,
                )
            )

            cursor.execute(
                """
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
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    inventory_id,
                    sku,
                    warehouse_id,
                    "ADJUSTMENT",
                    difference,
                    reference_id,
                    "Inventory reconciliation adjustment",
                )
            )

            updated += 1

            details.append(
                {
                    "warehouse_id":
                        warehouse_id,
                    "sku":
                        sku,
                    "old_quantity":
                        current_quantity,
                    "new_quantity":
                        uploaded_quantity,
                    "difference":
                        difference,
                    "status":
                        "UPDATED",
                }
            )

        conn.commit()

    except Exception:

        conn.rollback()

        conn.close()

        raise

    conn.close()

    return {
        "updated": updated,
        "skipped": skipped,
        "details": details,
    }


# ============================================================
# DATABASE REFERENCE COUNTS
# ============================================================

def get_database_counts():

    conn = get_connection()

    tables = [
        "orders",
        "order_items",
        "products",
        "warehouses",
        "inventory",
    ]

    counts = {}

    for table in tables:

        query = f"""
            SELECT COUNT(*) AS count
            FROM {table}
        """

        result = pd.read_sql_query(
            query,
            conn
        )

        counts[table] = int(
            result.iloc[0]["count"]
        )

    conn.close()

    return counts


# ============================================================
# STANDARD IMPORT FUNCTIONS
# ============================================================

def import_orders(df):

    validation = validate_orders_dataframe(
        df
    )

    if not validation["valid"]:

        raise ValueError(
            "Validation failed: "
            + " | ".join(
                validation["errors"]
            )
        )

    conn = get_connection()

    cursor = conn.cursor()

    query = """
        INSERT INTO orders (
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
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id)
        DO UPDATE SET
            marketplace_order_id = excluded.marketplace_order_id,
            order_date = excluded.order_date,
            channel = excluded.channel,
            customer_name = excluded.customer_name,
            shipping_city = excluded.shipping_city,
            shipping_state = excluded.shipping_state,
            pincode = excluded.pincode,
            priority = excluded.priority,
            priority_reason = excluded.priority_reason,
            promised_ship_by = excluded.promised_ship_by,
            promised_delivery_date = excluded.promised_delivery_date,
            payment_status = excluded.payment_status,
            order_status = excluded.order_status,
            total_items = excluded.total_items,
            order_value = excluded.order_value,
            customer_type = excluded.customer_type
    """

    records = []

    for _, row in df.iterrows():

        records.append(
            (
                row["order_id"],
                row["marketplace_order_id"],
                row["order_date"],
                row["channel"],
                row["customer_name"],
                row["shipping_city"],
                row["shipping_state"],
                row["pincode"],
                row["priority"],
                row["priority_reason"],
                row["promised_ship_by"],
                row["promised_delivery_date"],
                row["payment_status"],
                row["order_status"],
                int(row["total_items"]),
                float(row["order_value"]),
                row["customer_type"],
            )
        )

    cursor.executemany(
        query,
        records
    )

    conn.commit()

    affected_rows = cursor.rowcount

    conn.close()

    return affected_rows


def import_order_items(df):
    """Replace the order_items table with the uploaded snapshot.

    Order history is intentionally cumulative, but order-item data is treated
    as a snapshot. A successful upload therefore replaces the complete
    order_items table rather than appending to it.

    The operation is blocked when operational transaction/event data exists,
    because deleting/replacing item rows while reservations, picks or
    fulfillment events are active could make the live workflow inconsistent.
    The preferred daily workflow is the atomic Marketplace Order Batch import,
    which refreshes the item set for each uploaded order safely.
    """
    validation = validate_order_items_dataframe(df)

    if not validation["valid"]:
        raise ValueError(
            "Validation failed: "
            + " | ".join(validation["errors"])
        )

    conn = get_connection()

    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # Every item in a standalone snapshot must belong to an existing order
        # and a known SKU. Unlike the old upsert behavior, we do not allow an
        # item file to silently create an incomplete operational dataset.
        order_ids = {
            str(row[0])
            for row in conn.execute("SELECT order_id FROM orders").fetchall()
        }
        product_skus = {
            str(row[0])
            for row in conn.execute("SELECT sku FROM products").fetchall()
        }

        uploaded_order_ids = set(
            df["order_id"].dropna().astype(str)
        )
        uploaded_skus = set(
            df["sku"].dropna().astype(str)
        )

        missing_orders = sorted(uploaded_order_ids - order_ids)
        missing_skus = sorted(uploaded_skus - product_skus)

        if missing_orders:
            raise ValueError(
                "Order item snapshot contains order_id values that do not exist "
                "in the orders table: "
                + ", ".join(missing_orders[:20])
            )

        if missing_skus:
            raise ValueError(
                "Order item snapshot contains SKU values that do not exist "
                "in the products table: "
                + ", ".join(missing_skus[:20])
            )

        # Do not destroy the relationship between operational history and the
        # item snapshot.
        protected_tables = [
            "inventory_transactions",
            "fulfillment_events",
            "stock_transfers",
        ]

        active_operational_records = []
        for table in protected_tables:
            try:
                count = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                count = 0

            if count:
                active_operational_records.append(
                    f"{table}: {count}"
                )

        if active_operational_records:
            raise ValueError(
                "Order item snapshot cannot replace the current order_items "
                "table while operational history exists ("
                + ", ".join(active_operational_records)
                + "). Use Marketplace Order Batch for live order updates, "
                  "or reset the operational test state first."
            )

        records = [
            (
                row["line_item_id"],
                row["order_id"],
                row["sku"],
                row["product_name"],
                row["variant"],
                int(row["quantity"]),
                float(row["unit_price"]),
                float(row["line_total"]),
            )
            for _, row in df.iterrows()
        ]

        conn.execute("BEGIN")

        # Non-cumulative snapshot behavior: old item rows are removed before
        # the uploaded item set is inserted.
        conn.execute("DELETE FROM order_items")

        conn.executemany(
            """
            INSERT INTO order_items (
                line_item_id,
                order_id,
                sku,
                product_name,
                variant,
                quantity,
                unit_price,
                line_total
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )

        conn.commit()

        return len(records)

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def import_products(df):

    validation = (
        validate_products_dataframe(
            df
        )
    )

    if not validation["valid"]:

        raise ValueError(
            "Validation failed: "
            + " | ".join(
                validation["errors"]
            )
        )

    conn = get_connection()

    cursor = conn.cursor()

    query = """
        INSERT INTO products (
            sku,
            product_id,
            product_name,
            category,
            brand,
            variant,
            size,
            color,
            unit_price,
            unit_weight_kg,
            active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku)
        DO UPDATE SET
            product_id = excluded.product_id,
            product_name = excluded.product_name,
            category = excluded.category,
            brand = excluded.brand,
            variant = excluded.variant,
            size = excluded.size,
            color = excluded.color,
            unit_price = excluded.unit_price,
            unit_weight_kg = excluded.unit_weight_kg,
            active = excluded.active
    """

    records = []

    for _, row in df.iterrows():

        active_value = row["active"]

        if isinstance(
            active_value,
            str
        ):

            active_value = (
                1
                if active_value.strip().lower()
                in [
                    "yes",
                    "true",
                    "1"
                ]
                else 0
            )

        else:

            active_value = int(
                active_value
            )

        records.append(
            (
                row["sku"],
                row["product_id"],
                row["product_name"],
                row["category"],
                row["brand"],
                row["variant"],
                row["size"],
                row["color"],
                float(row["unit_price"]),
                float(row["unit_weight_kg"]),
                active_value,
            )
        )

    cursor.executemany(
        query,
        records
    )

    conn.commit()

    affected_rows = cursor.rowcount

    conn.close()

    return affected_rows


def import_warehouses(df):

    validation = (
        validate_warehouses_dataframe(
            df
        )
    )

    if not validation["valid"]:

        raise ValueError(
            "Validation failed: "
            + " | ".join(
                validation["errors"]
            )
        )

    conn = get_connection()

    cursor = conn.cursor()

    query = """
        INSERT INTO warehouses (
            warehouse_id,
            warehouse_name,
            warehouse_type,
            location,
            capacity_status,
            active
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(warehouse_id)
        DO UPDATE SET
            warehouse_name = excluded.warehouse_name,
            warehouse_type = excluded.warehouse_type,
            location = excluded.location,
            capacity_status = excluded.capacity_status,
            active = excluded.active
    """

    records = []

    for _, row in df.iterrows():

        active_value = row["active"]

        if isinstance(
            active_value,
            str
        ):

            active_value = (
                1
                if active_value.strip().lower()
                in [
                    "yes",
                    "true",
                    "1"
                ]
                else 0
            )

        else:

            active_value = int(
                active_value
            )

        records.append(
            (
                row["warehouse_id"],
                row["warehouse_name"],
                row["warehouse_type"],
                row["location"],
                row["capacity_status"],
                active_value,
            )
        )

    cursor.executemany(
        query,
        records
    )

    conn.commit()

    affected_rows = cursor.rowcount

    conn.close()

    return affected_rows


# ============================================================
# STANDARD IMPORT DISPATCHER
# ============================================================

def import_dataframe(
    df,
    data_type
):

    if data_type == "orders":

        return import_orders(df)

    if data_type == "order_items":

        return import_order_items(df)

    if data_type == "products":

        return import_products(df)

    if data_type == "warehouses":

        return import_warehouses(df)

    raise ValueError(
        "Import is not supported for data type: "
        + str(data_type)
    )
# ============================================================
# ATOMIC MARKETPLACE ORDER BATCH IMPORT
# ============================================================

def import_marketplace_order_batch(orders_df, order_items_df):
    """Import one marketplace order batch atomically.

    New orders are inserted and existing orders are refreshed by order_id.
    For every order_id present in the uploaded order file, its complete
    order-item set is replaced by the uploaded lines. This prevents stale
    line items from surviving a corrected marketplace order feed.

    Mutating: orders and order_items are changed together in one transaction.
    If validation or any database write fails, both tables are rolled back.
    """
    orders_validation = validate_orders_dataframe(orders_df)
    items_validation = validate_order_items_dataframe(order_items_df)

    errors = []
    errors.extend(orders_validation.get("errors", []))
    errors.extend(items_validation.get("errors", []))

    if "order_id" not in orders_df.columns or "order_id" not in order_items_df.columns:
        errors.append("Both marketplace files must contain order_id.")
    else:
        uploaded_order_ids = set(orders_df["order_id"].astype(str))
        item_order_ids = set(order_items_df["order_id"].astype(str))
        orphan_ids = sorted(item_order_ids - uploaded_order_ids)
        if orphan_ids:
            errors.append(
                "Order items reference order_id values that are not present in the uploaded orders file: "
                + ", ".join(orphan_ids[:20])
            )

    # Every uploaded line item must reference a known product SKU. A marketplace
    # order should never be allowed into the operational database if its SKU
    # cannot be resolved against the product master.
    if "sku" in order_items_df.columns:
        conn = get_connection()
        try:
            known_skus = {
                str(row[0])
                for row in conn.execute("SELECT sku FROM products").fetchall()
            }
        finally:
            conn.close()
        uploaded_skus = set(order_items_df["sku"].dropna().astype(str))
        unknown_skus = sorted(uploaded_skus - known_skus)
        if unknown_skus:
            errors.append(
                "Order items reference SKU(s) not present in the product master: "
                + ", ".join(unknown_skus[:20])
            )

    if errors:
        raise ValueError("Marketplace batch validation failed: " + " | ".join(errors))

    order_ids = orders_df["order_id"].astype(str).tolist()
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("Marketplace orders file contains duplicate order_id values.")

    if "line_item_id" in order_items_df.columns:
        line_ids = order_items_df["line_item_id"].astype(str).tolist()
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("Marketplace order items file contains duplicate line_item_id values.")

    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")

        order_query = """
            INSERT INTO orders (
                order_id, marketplace_order_id, order_date, channel,
                customer_name, shipping_city, shipping_state, pincode,
                priority, priority_reason, promised_ship_by,
                promised_delivery_date, payment_status, order_status,
                total_items, order_value, customer_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                marketplace_order_id = excluded.marketplace_order_id,
                order_date = excluded.order_date,
                channel = excluded.channel,
                customer_name = excluded.customer_name,
                shipping_city = excluded.shipping_city,
                shipping_state = excluded.shipping_state,
                pincode = excluded.pincode,
                priority = excluded.priority,
                priority_reason = excluded.priority_reason,
                promised_ship_by = excluded.promised_ship_by,
                promised_delivery_date = excluded.promised_delivery_date,
                payment_status = excluded.payment_status,
                order_status = excluded.order_status,
                total_items = excluded.total_items,
                order_value = excluded.order_value,
                customer_type = excluded.customer_type
        """

        order_records = [
            (
                row["order_id"],
                row["marketplace_order_id"],
                row["order_date"],
                row["channel"],
                row["customer_name"],
                row["shipping_city"],
                row["shipping_state"],
                row["pincode"],
                row["priority"],
                row["priority_reason"],
                row["promised_ship_by"],
                row["promised_delivery_date"],
                row["payment_status"],
                row["order_status"],
                int(row["total_items"]),
                float(row["order_value"]),
                row["customer_type"],
            )
            for _, row in orders_df.iterrows()
        ]
        conn.executemany(order_query, order_records)

        # Replace the complete line-item set for every order in this batch.
        # This handles corrected marketplace feeds where an old line item is
        # no longer present in the new version of the order.
        for order_id in order_ids:
            conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))

        item_query = """
            INSERT INTO order_items (
                line_item_id, order_id, sku, product_name, variant,
                quantity, unit_price, line_total
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        item_records = [
            (
                row["line_item_id"],
                row["order_id"],
                row["sku"],
                row["product_name"],
                row["variant"],
                int(row["quantity"]),
                float(row["unit_price"]),
                float(row["line_total"]),
            )
            for _, row in order_items_df.iterrows()
        ]
        conn.executemany(item_query, item_records)

        conn.commit()
        return {
            "orders_processed": len(order_records),
            "order_items_processed": len(item_records),
            "order_ids_processed": order_ids,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
