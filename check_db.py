import sqlite3
import pandas as pd

for db in ['trading_data.db', 'testnet_logs.db', 'forensics.db', 'trades.db']:
    try:
        conn = sqlite3.connect('E:/Level 3 Term 2/Ai/Tradingbot/' + db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cur.fetchall()
        print(f"\n{db} tables: {tables}")
        for t in tables:
            tname = t[0]
            df = pd.read_sql_query(f"SELECT * FROM {tname} LIMIT 5", conn)
            print(f"Table {tname}:")
            print(df.head())
    except Exception as e:
        pass
