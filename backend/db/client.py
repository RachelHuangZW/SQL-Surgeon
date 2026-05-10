import psycopg2
from psycopg2.extras import RealDictCursor

class DBClient:
    def __init__(self, dsn):
        # DSN -> Data Source Name
        self.dsn = dsn
    
    def execute_explain(self, sql: str):
        conn = psycopg2.connect(self.dsn)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        try:
            explain_query = f"EXPLAIN (ANALYZE, COSTS, VERBOSE, BUFFERS, FORMAT JSON) {sql}"
            cursor.execute(explain_query)
            result = cursor.fetchone()
            # EXPLAIN ANALYZE physically executes the query, so rollback to prevent
            # accidental writes if the input SQL contains CTEs or side-effecting functions.
            conn.rollback()

            return result["QUERY PLAN"]
        
        except Exception as e:
            conn.rollback()
            print(f"Error executing Explain Plan: {e}")
            raise e
        
        finally:
            cursor.close()
            conn.close()
            




