import argparse
import logging
from agent.server import make_server
from agent.telemetry import MatchJournal


def main():
    parser = argparse.ArgumentParser(description="Future War participant HTTP server")
    parser.add_argument("port", type=int)
    parser.add_argument("--log-dir", default="logs", help="JSONL replay and ASCII map output directory")
    parser.add_argument("--map-every", type=int, default=1)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error("port must be 1..65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    journal = None
    try:
        journal = MatchJournal(args.log_dir, args.map_every)
    except OSError:
        logging.exception("cannot open match logs; continuing HTTP service")
    try:
        with make_server(args.port, journal=journal) as server:
            logging.info("listening on 0.0.0.0:%s", args.port)
            server.serve_forever()
    finally:
        if journal:
            journal.close()


if __name__ == "__main__":
    main()
