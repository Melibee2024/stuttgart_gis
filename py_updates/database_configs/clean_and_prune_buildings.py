import sys
import os

# 1. Move the path adjustment to the ABSOLUTE TOP
# This tells Python to look in the parent 'planung' folder first
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# 2. Import psycopg2 module
from psycopg2 import sql

# 3. Import your custom modules
from config_func import database_connector
from local_config_database import DB_HFT

TABLES_TO_CLEAN = ["ap_pto", "ax_flurstueck", "ax_gebaeude", "ax_gebaeudefunktion"]
SOURCE_SCHEMA = "stuttgart_2d"
TARGET_SCHEMA = "stuttgart_processed"


def get_active_columns(cursor, schema, table):
    """Finds columns that are NOT entirely NULL."""
    cursor.execute("""
                   SELECT column_name
                   FROM information_schema.columns
                   WHERE table_schema = %s
                     AND table_name = %s;
                   """, (schema, table))

    all_columns = [row[0] for row in cursor.fetchall()]
    active_columns = []

    print(f"Analyzing columns for {schema}.{table}...")
    for col in all_columns:
        # Check if the column has any non-null values
        query = sql.SQL("SELECT 1 FROM {}.{} WHERE {} IS NOT NULL LIMIT 1;").format(
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.Identifier(col)
        )
        cursor.execute(query)
        if cursor.fetchone():
            active_columns.append(col)

    return active_columns


def clean_and_copy_tables():
    # Use your centralized connection function
    print("Connecting to database using project environment configuration...")
    conn = database_connector(DB_HFT)

    if not conn:
        print("CRITICAL: Could not connect to the database. Check your .env file details.")
        return

    cursor = conn.cursor()

    try:
        for table in TABLES_TO_CLEAN:
            # 1. Identify non-empty columns
            active_cols = get_active_columns(cursor, SOURCE_SCHEMA, table)
            if not active_cols:
                print(f"Skipping {table}: No active data found.")
                continue

            # 2. Build the SELECT elements, fixing geometry if present
            select_elements = []
            for col in active_cols:
                # Check for standard geometry naming conventions
                if col in ['wkb_geometry', 'geom', 'geometry']:
                    if table == "ax_gebaeude":
                        select_elements.append(f"ST_Multi(ST_MakeValid({col}))::geometry(MultiPolygon, 25832) AS {col}")
                    else:
                        select_elements.append(f"ST_MakeValid({col}) AS {col}")
                else:
                    select_elements.append(f'"{col}"')

            select_clause = ", ".join(select_elements)
            target_table = f"{table}_clean"

            # 3. Drop target table if it exists to allow clean overwrites
            cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}.{};").format(
                sql.Identifier(TARGET_SCHEMA),
                sql.Identifier(target_table)
            ))

            # 4. Create the new pruned physical table
            create_query = f"""
                CREATE TABLE {TARGET_SCHEMA}.{target_table} AS 
                SELECT {select_clause} 
                FROM {SOURCE_SCHEMA}.{table};
            """
            print(f"Creating pruned table: {TARGET_SCHEMA}.{target_table}")
            cursor.execute(create_query)

        conn.commit()
        print("\nSuccess! All active tables cleaned, validated, and pruned into 'stuttgart_processed'.")

    except Exception as e:
        conn.rollback()
        print(f"An error occurred during processing: {e}")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    clean_and_copy_tables()