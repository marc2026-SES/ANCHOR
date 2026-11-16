"""Select a deployment role before importing configuration-dependent modules."""
import argparse
import os


def main():
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--role", choices=("dev", "ship", "shore"))
    args, remaining = selector.parse_known_args()
    if args.role:
        os.environ["ANCHOR_ROLE"] = args.role
    from .cli import main as run
    run(remaining)


if __name__ == "__main__":
    main()
