import argparse
import logging
from agent.server import make_server
from agent.telemetry import StreamJournal
import sys


def main():
    parser = argparse.ArgumentParser(description="Future War participant HTTP server")
    parser.add_argument("port", type=int)
    parser.add_argument("--map-every", type=int, default=1)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error("port must be 1..65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    journal = StreamJournal(map_every=args.map_every)
    try:
        with make_server(args.port, journal=journal) as server:
            logging.info("listening on 0.0.0.0:%s", args.port)
            server.serve_forever()
    finally:
        if journal:
            journal.close()


if __name__ == "__main__":
    main()
