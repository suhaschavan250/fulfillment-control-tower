import sqlite3
import pandas as pd


# --------------------------------------------------
# 1. Read CSV files
# --------------------------------------------------

warehouses_df = pd.read_csv("data/warehouses.csv")
products_df = pd.read_csv("data/product_sku_master.csv")
inventory_df = pd.read_csv("data/inventory.csv")
orders_df = pd.read_csv("data/marketplace_orders_300_final.csv")
order_items_df = pd.read_csv("data/marketplace_order_items_300.csv")


# --------------------------------------------------
# 2. Connect to SQLite database
# --------------------------------------------------

connection = sqlite3.connect("fulfillment.db")
cursor = connection.cursor()


# --------------------------------------------------
# 3. Create Warehouses table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id TEXT PRIMARY KEY,
    warehouse_name TEXT NOT NULL,
    warehouse_type TEXT NOT NULL,
    location TEXT,
    capacity_status TEXT,
    active INTEGER
)
""")


# --------------------------------------------------
# 4. Create Products table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT,
    brand TEXT,
    variant TEXT,
    size TEXT,
    color TEXT,
    unit_price REAL,
    unit_weight_kg REAL,
    active INTEGER
)
""")


# --------------------------------------------------
# 5. Create Inventory table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS inventory (
    inventory_id TEXT PRIMARY KEY,
    warehouse_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    bin_location TEXT,
    system_quantity INTEGER NOT NULL,
    reserved_quantity INTEGER NOT NULL,
    available_quantity INTEGER NOT NULL,
    last_verified_at TEXT,
    inventory_status TEXT,
    scenario TEXT,
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id),
    FOREIGN KEY (sku) REFERENCES products(sku)
)
""")


# --------------------------------------------------
# 6. Create Inventory Transactions table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS inventory_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    reference_id TEXT,
    notes TEXT,
    transaction_time TEXT NOT NULL,
    FOREIGN KEY (inventory_id) REFERENCES inventory(inventory_id),
    FOREIGN KEY (sku) REFERENCES products(sku),
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id)
)
""")


# --------------------------------------------------
# 7. Create Fulfillment Events table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS fulfillment_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_status TEXT,
    notes TEXT,
    event_time TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
)
""")


# --------------------------------------------------
# 8. Create Stock Transfers table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS stock_transfers (
    transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    from_warehouse_id TEXT NOT NULL,
    to_warehouse_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    transfer_status TEXT NOT NULL,
    reference_order_id TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    notes TEXT,
    FOREIGN KEY (sku) REFERENCES products(sku),
    FOREIGN KEY (from_warehouse_id) REFERENCES warehouses(warehouse_id),
    FOREIGN KEY (to_warehouse_id) REFERENCES warehouses(warehouse_id),
    FOREIGN KEY (reference_order_id) REFERENCES orders(order_id)
)
""")


# --------------------------------------------------
# 9. Create Orders table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    marketplace_order_id TEXT,
    order_date TEXT NOT NULL,
    channel TEXT,
    customer_name TEXT,
    shipping_city TEXT,
    shipping_state TEXT,
    pincode TEXT,
    priority TEXT,
    priority_reason TEXT,
    promised_ship_by TEXT,
    promised_delivery_date TEXT,
    payment_status TEXT,
    order_status TEXT,
    total_items INTEGER,
    order_value REAL,
    customer_type TEXT
)
""")


# --------------------------------------------------
# 10. Create Order Items table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS order_items (
    line_item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    product_name TEXT,
    variant TEXT,
    quantity INTEGER NOT NULL,
    unit_price REAL,
    line_total REAL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (sku) REFERENCES products(sku)
)
""")


# --------------------------------------------------
# 11. Create Exceptions table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    order_id TEXT,
    sku TEXT,
    warehouse_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_notes TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (sku) REFERENCES products(sku),
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(warehouse_id)
)
""")


# --------------------------------------------------
# 12. Insert Warehouses safely
# --------------------------------------------------

for _, row in warehouses_df.iterrows():

    cursor.execute("""
        INSERT OR IGNORE INTO warehouses (
            warehouse_id,
            warehouse_name,
            warehouse_type,
            location,
            capacity_status,
            active
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        row["warehouse_id"],
        row["warehouse_name"],
        row["warehouse_type"],
        row["location"],
        row["capacity_status"],
        1 if str(row["active"]).strip().lower()
        in ["true", "yes", "1"] else 0
    ))


# --------------------------------------------------
# 13. Insert Products safely
# --------------------------------------------------

for _, row in products_df.iterrows():

    cursor.execute("""
        INSERT OR IGNORE INTO products (
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
    """, (
        row["sku"],
        row["product_id"],
        row["product_name"],
        row["category"],
        row["brand"],
        row["variant"],
        row["size"],
        row["color"],
        row["unit_price"],
        row["unit_weight_kg"],
        1 if str(row["active"]).strip().lower()
        in ["true", "yes", "1"] else 0
    ))


# --------------------------------------------------
# 14. Insert Inventory safely
# --------------------------------------------------

for _, row in inventory_df.iterrows():

    cursor.execute("""
        INSERT OR IGNORE INTO inventory (
            inventory_id,
            warehouse_id,
            sku,
            bin_location,
            system_quantity,
            reserved_quantity,
            available_quantity,
            last_verified_at,
            inventory_status,
            scenario
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["inventory_id"],
        row["warehouse_id"],
        row["sku"],
        row["bin_location"],
        int(row["system_quantity"]),
        int(row["reserved_quantity"]),
        int(row["available_quantity"]),
        row["last_verified_at"],
        row["inventory_status"],
        row["scenario"]
    ))


# --------------------------------------------------
# 15. Insert Orders safely
# --------------------------------------------------

for _, row in orders_df.iterrows():

    cursor.execute("""
        INSERT OR IGNORE INTO orders (
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
    """, (
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
        row["customer_type"]
    ))


# --------------------------------------------------
# 16. Insert Order Items safely
# --------------------------------------------------

for _, row in order_items_df.iterrows():

    cursor.execute("""
        INSERT OR IGNORE INTO order_items (
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
    """, (
        row["line_item_id"],
        row["order_id"],
        row["sku"],
        row["product_name"],
        row["variant"],
        int(row["quantity"]),
        float(row["unit_price"]),
        float(row["line_total"])
    ))


# --------------------------------------------------
# 17. Save changes
# --------------------------------------------------

connection.commit()


# --------------------------------------------------
# 18. Check tables
# --------------------------------------------------

print("\nTables in database:")

print(
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
)


# --------------------------------------------------
# 19. Check record counts
# --------------------------------------------------

warehouse_count = cursor.execute(
    "SELECT COUNT(*) FROM warehouses"
).fetchone()[0]

product_count = cursor.execute(
    "SELECT COUNT(*) FROM products"
).fetchone()[0]

inventory_count = cursor.execute(
    "SELECT COUNT(*) FROM inventory"
).fetchone()[0]

order_count = cursor.execute(
    "SELECT COUNT(*) FROM orders"
).fetchone()[0]

order_item_count = cursor.execute(
    "SELECT COUNT(*) FROM order_items"
).fetchone()[0]

transaction_count = cursor.execute(
    "SELECT COUNT(*) FROM inventory_transactions"
).fetchone()[0]

fulfillment_event_count = cursor.execute(
    "SELECT COUNT(*) FROM fulfillment_events"
).fetchone()[0]

stock_transfer_count = cursor.execute(
    "SELECT COUNT(*) FROM stock_transfers"
).fetchone()[0]

exception_count = cursor.execute(
    "SELECT COUNT(*) FROM exceptions"
).fetchone()[0]


print("\nRecord counts:")
print("Warehouses:", warehouse_count)
print("Products:", product_count)
print("Inventory:", inventory_count)
print("Orders:", order_count)
print("Order Items:", order_item_count)
print("Inventory Transactions:", transaction_count)
print("Fulfillment Events:", fulfillment_event_count)
print("Stock Transfers:", stock_transfer_count)
print("Exceptions:", exception_count)


# --------------------------------------------------
# 20. Validate Order → Order Items relationship
# --------------------------------------------------

missing_orders = cursor.execute("""
    SELECT COUNT(*)
    FROM order_items oi
    LEFT JOIN orders o
        ON oi.order_id = o.order_id
    WHERE o.order_id IS NULL
""").fetchone()[0]

print(
    "Order items without matching orders:",
    missing_orders
)


# --------------------------------------------------
# 21. Validate Order Items → Products relationship
# --------------------------------------------------

missing_skus = cursor.execute("""
    SELECT COUNT(*)
    FROM order_items oi
    LEFT JOIN products p
        ON oi.sku = p.sku
    WHERE p.sku IS NULL
""").fetchone()[0]

print(
    "Order items with missing SKUs:",
    missing_skus
)


# --------------------------------------------------
# 22. Close connection
# --------------------------------------------------

connection.close()

print("\nDatabase setup completed successfully!")