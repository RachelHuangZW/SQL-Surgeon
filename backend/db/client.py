import psycopg2
from psycopg2.extras import RealDictCursor
import time

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

    
    def benchmark_in_sandbox(self, table_name: str, original_sql: str, suggested_ddl: str):
        conn = psycopg2.connect(self.dsn)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        schema_name = f"surgeon_tmp_{int(time.time())}"

        try:
            cursor.execute(f"CREATE SCHEMA {schema_name};")
            cursor.execute(f"CREATE TABLE {schema_name}.{table_name} (LIKE public.{table_name} INCLUDING ALL);")
            cursor.execute(f"INSERT INTO {schema_name}.{table_name} SELECT * FROM public.{table_name} LIMIT 100000;")
            cursor.execute(f"SET search_path TO {schema_name};")

            if suggested_ddl:
                cursor.execute(suggested_ddl)
            
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {original_sql}")
            result = cursor.fetchone()
            new_plan = result["QUERY PLAN"]
            
            return new_plan

        except Exception as e:
            conn.rollback()
            print(f"Error creating benchmark schema: {e}")
            raise e

        finally:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;")
            conn.commit()
            cursor.close()
            conn.close()   




