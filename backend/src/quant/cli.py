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


def check_holdings(csv_path: Path, crypto_pdf: Path | None) -> int:
    """Acceptance check on the owner's machine (PRD §9a): rebuild holdings from the transaction
    export and compare them with the crypto statement. Nothing is stored. Prints only counts and
    pass/fail, never names, ISINs or amounts, so the output is safe to share.
    """
    from collections import Counter

    from quant.importers import tr_crypto_pdf, tr_csv
    from quant.portfolio.positions import compute_positions, open_positions
    from quant.portfolio.reconcile import StatementLine, reconcile, summarise

    try:
        parsed = tr_csv.parse(csv_path.read_text(encoding="utf-8"))
    except tr_csv.ImportFormatError as exc:
        print(f"FAIL transactions: {exc}")
        return 1
    positions = open_positions(compute_positions(parsed.transactions))
    by_class = Counter(p.asset_class or "UNKNOWN" for p in positions)
    print(f"open positions rebuilt from {len(parsed.transactions)} transactions: {len(positions)}")
    for cls, count in sorted(by_class.items()):
        print(f"  {cls:<14}{count:>5}")
    touched = sum(p.corporate_action_touched for p in positions)
    print(f"positions touched by a corporate action: {touched}")
    ok = parsed.skipped == 0
    print(f"{'PASS' if ok else 'FAIL'} transactions parsed without skipped rows")

    if crypto_pdf is not None:
        try:
            statement = tr_crypto_pdf.parse_text(
                tr_crypto_pdf.extract_text(crypto_pdf.read_bytes())
            )
        except tr_crypto_pdf.StatementFormatError as exc:
            print(f"FAIL crypto statement: {exc}")
            return 1
        lines = [StatementLine(name=ln.name, quantity=ln.quantity) for ln in statement.lines]
        counts = summarise(reconcile(positions, lines, tr_crypto_pdf.ASSET_CLASSES))
        print(f"crypto statement of {statement.as_of}: {len(lines)} positions")
        for status, count in counts.items():
            print(f"  {status:<20}{count:>3}")
        clean = counts["match"] == len(lines) and not (
            counts["quantity_mismatch"]
            or counts["missing_in_history"]
            or counts["not_on_statement"]
        )
        print(f"{'PASS' if clean else 'FAIL'} crypto quantities match the statement")
        ok = ok and clean
    return 0 if ok else 1


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

    p = sub.add_parser(
        "check-holdings",
        help="dry-run: rebuild holdings from a TR export and compare with the crypto statement",
    )
    p.add_argument("csv")
    p.add_argument("--crypto-pdf", default=None)

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
    elif args.command == "check-holdings":
        crypto = Path(args.crypto_pdf) if args.crypto_pdf else None
        sys.exit(check_holdings(Path(args.csv), crypto))
    elif args.command == "check-tr-csv":
        sys.exit(check_tr_csv(Path(args.path)))
    elif args.command in ("ingest-prices", "ingest-fx", "worker"):
        from quant import worker

        {"ingest-prices": worker.run_prices, "ingest-fx": worker.run_fx, "worker": worker.main}[
            args.command
        ]()


if __name__ == "__main__":
    main()
