import argparse
import logging
from agent.server import make_server


def main():
    parser = argparse.ArgumentParser(description="Future War participant HTTP server")
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error("port must be 1..65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with make_server(args.port) as server:
        logging.info("listening on 0.0.0.0:%s", args.port)
        server.serve_forever()


if __name__ == "__main__":
    main()
