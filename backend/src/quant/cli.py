"""Command-line admin tasks: `python -m quant.cli <command>`."""

import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import func, select

from quant.config import get_settings
from quant.db import get_sessionmaker
from quant.models import Role, User
from quant.security import MIN_PASSWORD_LENGTH, hash_password
from quant.seed import load_instruments, seed_instruments


def create_admin(email: str, name: str) -> None:
    password = os.environ.get("QT_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    with get_sessionmaker()() as session:
        if session.scalar(select(User).where(func.lower(User.email) == email.lower())):
            sys.exit(f"user {email} already exists")
        session.add(
            User(
                email=email.lower(),
                display_name=name,
                password_hash=hash_password(password),
                role=Role.ADMIN,
            )
        )
        session.commit()
    print(f"admin {email.lower()} created")


def check_tr_csv(path: Path) -> int:
    """Acceptance check for real exports on the owner's machine (PRD §9a).

    Prints only counts, line numbers and pass/fail: never names, ISINs or amounts, so the output
    is safe to paste into an issue or a chat.
    """
    from quant.importers import tr_csv

    text = path.read_text(encoding="utf-8")
    try:
        result = tr_csv.parse(text)
    except tr_csv.ImportFormatError as exc:
        print(f"FAIL format: {exc}")
        return 1
    summary = result.summary()
    print(f"rows parsed: {summary['rows']}, skipped: {summary['skipped']}")
    print(f"date range: {summary['first_date']} .. {summary['last_date']}")
    for kind, count in summary["by_kind"].items():
        print(f"  {kind:<17}{count:>7}")
    print(
        f"instruments: {summary['instruments']}, savings plan executions: "
        f"{summary['savings_plan_executions']}"
    )
    for warning in result.warnings:
        print(f"warning: {warning}")
    leaks = tr_csv.privacy_leaks(text, result)
    checks = {
        "no rows skipped": result.skipped == 0,
        "no unknown types": summary["by_kind"].get("other", 0) == 0,
        "no personal data in parsed rows": leaks == 0,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="quant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", help="create the owner/admin account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)

    p = sub.add_parser("seed-instruments", help="load the instrument seed list")
    p.add_argument("--file", default=None)

    p = sub.add_parser(
        "check-tr-csv",
        help="dry-run a TR transaction export: counts and pass/fail only, nothing is stored",
    )
    p.add_argument("path")

    sub.add_parser("ingest-prices", help="run the EOD price job now")
    sub.add_parser("ingest-fx", help="run the ECB FX job now")
    sub.add_parser("worker", help="run the scheduler")

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        create_admin(args.email, args.name)
    elif args.command == "seed-instruments":
        path = Path(args.file or get_settings().instruments_file)
        with get_sessionmaker()() as session:
            print(f"seeded {seed_instruments(session, load_instruments(path))} instruments")
    elif args.command == "check-tr-csv":
        sys.exit(check_tr_csv(Path(args.path)))
    elif args.command in ("ingest-prices", "ingest-fx", "worker"):
        from quant import worker

        {"ingest-prices": worker.run_prices, "ingest-fx": worker.run_fx, "worker": worker.main}[
            args.command
        ]()


if __name__ == "__main__":
    main()
