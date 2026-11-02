import argparse

from .db import migrate

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["migrate"])
args = parser.parse_args()
if args.command == "migrate":
    migrate()
    print("migration complete")
