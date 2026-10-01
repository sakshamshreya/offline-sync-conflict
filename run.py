"""Start the Offline Sync Conflict API."""

import argparse

from offline_sync.engine import SyncEngine
from offline_sync.http_api import create_server
from offline_sync.storage import SQLiteStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Offline Sync Conflict API demo.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000).")
    parser.add_argument(
        "--db",
        default="./sync.sqlite3",
        help="SQLite database file (default: ./sync.sqlite3).",
    )
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    engine = SyncEngine(SQLiteStore(args.db))
    server = create_server(args.host, args.port, engine)
    print(f"Offline Sync Conflict API listening at http://{args.host}:{args.port}")
    print(f"SQLite database: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
