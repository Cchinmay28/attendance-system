"""
Simple Supabase inspection script.
Usage:
  - Add SUPABASE_URL and SUPABASE_KEY to your .env (or set env vars)
  - Run: ./.venv/Scripts/python.exe scripts/check_supabase.py

The script prints row counts and a sample of rows for key tables.
"""
import os
import sys
from pprint import pprint

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from supabase import create_client
except Exception:
    create_client = None

if load_dotenv:
    load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_ANON_KEY') or os.getenv('SUPABASE_KEY')

if not SUPABASE_URL or not SUPABASE_KEY or not create_client:
    print('Missing configuration or supabase client.\n')
    print('Ensure:')
    print('- You have installed dependencies from requirements.txt')
    print('- SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) are set in .env or the environment')
    print('- The supabase Python package is installed')
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

TABLES = ['employees','offices','attendance','denied_attempts']

def count_table(table):
    try:
        resp = sb.table(table).select('id', count='exact').limit(1).execute()
        # PostgREST count is present under resp.count when using select('*') with count
        # Fallback: fetch a limited sample and return length if count not available
        if getattr(resp, 'count', None) is not None:
            return resp.count
        # Try a simple select to count rows
        resp2 = sb.rpc('count_rows', {'table_name': table}).execute() if hasattr(sb, 'rpc') else None
    except Exception:
        resp = sb.table(table).select('*').limit(100).execute()
        return len(resp.data or [])
    return len(resp.data or [])

def sample_rows(table, n=5):
    try:
        resp = sb.table(table).select('*').limit(n).execute()
        return resp.data or []
    except Exception as e:
        print(f'Error fetching sample for {table}: {e}')
        return []

if __name__ == '__main__':
    print('Supabase URL:', SUPABASE_URL)
    print('Inspecting tables:')
    for t in TABLES:
        print('\n---', t, '---')
        try:
            rows = sample_rows(t, 5)
            print('Sample rows (up to 5):')
            pprint(rows)
            # Try to get count via a head request
            try:
                count = sb.table(t).select('id', count='exact').limit(1).execute().count
                print('Row count (exact):', count)
            except Exception:
                print('Row count: (use SQL in Supabase SQL editor for exact counts)')
        except Exception as e:
            print('Failed to inspect', t, '->', e)
    print('\nDone.')
