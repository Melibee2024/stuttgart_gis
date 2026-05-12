from psycopg2 import connect
from psycopg2.extras import DictCursor
# This imports your dictionary
from .local_config_database import DB_HFT

def database_connector(database_info_dic):
    """Establishes a connection to the PostGIS database."""
    try:
        connection = connect(
            host=database_info_dic['HOST'],
            port=database_info_dic['PORT'],
            dbname=database_info_dic['NAME'],
            user=database_info_dic['USER'],
            password=database_info_dic['PASS']
        )
        return connection
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def get_building_count():
    """Example function to test the connection."""
    conn = database_connector(DB_HFT)
    if conn:
        cur = conn.cursor(cursor_factory=DictCursor)
        # Using your actual schema from the proposal
        cur.execute("SELECT count(*) FROM stuttgart_2d.ax_gebaeude;")
        count = cur.fetchone()[0]
        conn.close()
        return count
    return 0