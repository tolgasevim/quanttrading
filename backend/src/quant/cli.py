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


def check_holdings(csv_path: Path, crypto_pdf: Path | None, depot_pdf: Path | None = None) -> int:
    """Acceptance check on the owner's machine (PRD §9a): rebuild holdings from the transaction
    export and compare them with the crypto statement and the securities statement (Depotauszug).
    Nothing is stored. Prints only counts and
    pass/fail, never names, ISINs or amounts, so the output is safe to share.
    """
    from collections import Counter

    from quant.importers import tr_crypto_pdf, tr_csv, tr_depot_pdf
    from quant.portfolio.lots import (
        FLAG_COST_UNKNOWN,
        FLAG_INCOMPLETE_HISTORY,
        FLAG_PRICE_DERIVED,
        build_lots,
        position_costs,
    )
    from quant.portfolio.positions import DUST, compute_positions, open_positions
    from quant.portfolio.reconcile import (
        StatementLine,
        all_but,
        classes,
        find_by_name,
        merge_lines,
        reconcile,
        summarise,
    )
    from quant.portfolio.service import COST_TOLERANCE, COST_TOLERANCE_PER_DERIVED_LOT

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

    book = build_lots(parsed.transactions)
    from_lots = book.open_quantities()
    from_positions = {p.isin: p.quantity for p in positions}
    agree = from_lots.keys() == from_positions.keys() and all(
        abs(from_lots[i] - q) <= DUST for i, q in from_positions.items()
    )
    print(
        f"{'PASS' if agree else 'FAIL'} cost-basis lots agree with the positions on every quantity"
    )
    gaps = sum(FLAG_INCOMPLETE_HISTORY in d.flags for d in book.disposals)
    verdict = "PASS" if gaps == 0 else "FAIL"
    print(f"{verdict} every sale is covered by earlier purchases ({gaps} are not)")
    costs = position_costs(book)
    print(
        f"disposals: {len(book.disposals)}; positions needing a cost from you: "
        f"{sum(FLAG_COST_UNKNOWN in c.flags for c in costs.values())}; positions valued at a "
        f"receipt price: {sum(FLAG_PRICE_DERIVED in c.flags for c in costs.values())}; "
        f"corporate-action cash that fits no action: {len(book.unattributed)}"
    )
    ok = ok and agree and gaps == 0

    # A statement that fails to read is a FAIL, and the other statement is still checked.
    statement = None
    if crypto_pdf is not None:
        try:
            statement = tr_crypto_pdf.parse_text(
                tr_crypto_pdf.extract_text(crypto_pdf.read_bytes())
            )
        except tr_crypto_pdf.StatementFormatError as exc:
            print(f"FAIL crypto statement: {exc}")
            ok = False
    if statement is not None:
        lines = [StatementLine(name=ln.name, quantity=ln.quantity) for ln in statement.lines]
        # Compare with the history up to the statement's own date, not up to today.
        until = statement.as_of.isoformat()
        as_of_positions = open_positions(
            compute_positions([t for t in parsed.transactions if t.date <= until])
        )
        counts = summarise(reconcile(as_of_positions, lines, classes(*tr_crypto_pdf.ASSET_CLASSES)))
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

        # Purchase value (Kaufwert) against the lots as of the statement date.
        as_of_rows = [t for t in parsed.transactions if t.date <= until]
        as_of_book = build_lots(as_of_rows)
        as_of_costs = position_costs(as_of_book)
        checked = matched_cost = 0
        for line, source in zip(lines, statement.lines, strict=True):
            position = find_by_name(as_of_positions, line.name)
            if position is None or position.isin not in as_of_costs:
                continue
            derived = sum(
                FLAG_PRICE_DERIVED in lot.flags for lot in as_of_book.lots.get(position.isin, [])
            )
            tolerance = COST_TOLERANCE + COST_TOLERANCE_PER_DERIVED_LOT * derived
            checked += 1
            matched_cost += abs(as_of_costs[position.isin].cost - source.cost_eur) <= tolerance
        cost_ok = checked == len(lines) and matched_cost == checked
        print(f"cost basis matching the statement's purchase value: {matched_cost} of {len(lines)}")
        print(f"{'PASS' if cost_ok else 'FAIL'} crypto cost basis matches the statement")
        ok = ok and cost_ok

    depot = None
    if depot_pdf is not None:
        from quant.importers.statement_pdf import StatementFormatError, extract_text

        try:
            depot = tr_depot_pdf.parse_text(extract_text(depot_pdf.read_bytes()))
        except StatementFormatError as exc:
            print(f"FAIL securities statement: {exc}")
            ok = False
    if depot is not None:
        statement_lines = merge_lines(
            [StatementLine(ln.name, ln.quantity, ln.isin, ln.value_eur) for ln in depot.lines]
        )
        until = depot.as_of.isoformat()
        depot_positions = open_positions(
            compute_positions([t for t in parsed.transactions if t.date <= until])
        )
        counts = summarise(reconcile(depot_positions, statement_lines, all_but("CRYPTO")))
        priced = sum(ln.price_eur is not None for ln in depot.lines)
        with_country = sum(ln.custody is not None for ln in depot.lines)
        print(
            f"securities statement of {depot.as_of}: {len(depot.lines)} rows, "
            f"{len(statement_lines)} positions, {priced} with a usable price, "
            f"{with_country} with a custody country"
        )
        for status, count in counts.items():
            print(f"  {status:<20}{count:>3}")
        clean = counts["match"] == len(statement_lines) and not (
            counts["quantity_mismatch"]
            or counts["missing_in_history"]
            or counts["not_on_statement"]
        )
        print(f"{'PASS' if clean else 'FAIL'} securities quantities match the statement")
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
        help="dry-run: rebuild holdings from a TR export and compare with the statements",
    )
    p.add_argument("csv")
    p.add_argument("--crypto-pdf", default=None)
    p.add_argument("--depot-pdf", default=None)

    sub.add_parser("map-isins", help="map held ISINs to tickers now (FR-12)")
    sub.add_parser("ingest-prices", help="run the EOD price job now")
    sub.add_parser("create-alerts", help="create the daily-move and failed-job alerts now")
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
        depot = Path(args.depot_pdf) if args.depot_pdf else None
        sys.exit(check_holdings(Path(args.csv), crypto, depot))
    elif args.command == "check-tr-csv":
        sys.exit(check_tr_csv(Path(args.path)))
    elif args.command in ("map-isins", "ingest-prices", "ingest-fx", "create-alerts", "worker"):
        from quant import worker

        {
            "map-isins": worker.run_mapping,
            "ingest-prices": worker.run_prices,
            "ingest-fx": worker.run_fx,
            "create-alerts": worker.run_alerts,
            "worker": worker.main,
        }[args.command]()


if __name__ == "__main__":
    main()
