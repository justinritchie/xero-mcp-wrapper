#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp>=2.5.0",
# ]
# ///
"""
xero-mcp-wrapper — a thin MCP server that shells out to Xero's official
`xero` command-line tool (https://github.com/XeroAPI/xero-command-line).

Why this exists:
- The CLI uses PKCE OAuth — no client secret in the MCP config.
- Multi-organisation support is built in via named profiles; each tool takes
  an optional `profile` arg, defaulting to the CLI's default profile.
- The CLI is officially maintained by Xero, so the data model and endpoints
  stay current. This wrapper is the small adapter layer that turns each
  command into an MCP tool.

Pre-requisites on the host machine:
- `npm install -g @xeroapi/xero-command-line`  (puts `xero` on PATH)
- One or more profiles configured + logged in:
      xero profile add ets --client-id <client-id>
      xero login -p ets
- `xero org details -p ets --json` returns valid data.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from typing import Any

from fastmcp import FastMCP


XERO_BIN = os.environ.get("XERO_BIN") or shutil.which("xero") or "/opt/homebrew/bin/xero"
DEFAULT_PROFILE = os.environ.get("XERO_PROFILE")  # picked up by the CLI itself, but explicit override allowed

mcp = FastMCP(name="xero-cli")


async def _xero(args: list[str], profile: str | None = None, stdin_data: str | None = None) -> Any:
    """Run `xero <args> [-p <profile>] --json` and return the parsed result.

    On non-zero exit, returns an error string with the captured stderr —
    we let the model see the error rather than raising, so it can decide
    to retry or report back.
    """
    full = [XERO_BIN, *args]
    effective_profile = profile or DEFAULT_PROFILE
    if effective_profile:
        full.extend(["-p", effective_profile])
    full.append("--json")

    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin_data else None,
    )
    stdout_b, stderr_b = await proc.communicate(input=stdin_data.encode() if stdin_data else None)

    if proc.returncode != 0:
        return {
            "_error": f"xero CLI exited {proc.returncode}",
            "_stderr": stderr_b.decode("utf-8", errors="replace")[:2000],
            "_command": " ".join(full),
        }

    if not stdout_b.strip():
        return {"_ok": True, "_stderr": stderr_b.decode("utf-8", errors="replace")[:1000]}

    try:
        return json.loads(stdout_b)
    except json.JSONDecodeError:
        return {"_raw_output": stdout_b.decode("utf-8", errors="replace")[:5000]}


# ---------------------------------------------------------------------------
# Org / profile / auth — read-only inspection
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Get details for the Xero organisation associated with the active "
        "profile (legal name, tax registration number, base currency, "
        "financial year end, etc.). Use this to confirm which org the CLI "
        "is talking to before any other operation. Args:\n"
        "  profile: optional Xero profile name (default profile if omitted)"
    )
)
async def org_details(profile: str | None = None) -> str:
    return json.dumps(await _xero(["org", "details"], profile=profile), indent=2)


@mcp.tool(
    description=(
        "List the configured Xero CLI profiles on this machine. Each maps "
        "to a Xero OAuth app and (after login) one organisation. Useful "
        "for discovering valid profile names to pass to other tools."
    )
)
async def profiles_list() -> str:
    # `xero profile list` doesn't talk to the API so it doesn't need --json,
    # but the CLI still supports it. Don't pass a profile flag here.
    proc = await asyncio.create_subprocess_exec(
        XERO_BIN, "profile", "list", "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        return json.dumps({"_error": err.decode()[:2000]})
    try:
        return json.dumps(json.loads(out), indent=2)
    except Exception:
        return out.decode()[:5000]


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "List contacts in Xero. Optionally filter by free-text search and paginate.\n"
        "Args:\n"
        "  profile: Xero profile name (default if omitted)\n"
        "  search: Free-text search term matching name/email/etc.\n"
        "  page: 1-based page index (Xero returns 100 contacts per page)"
    )
)
async def contacts_list(profile: str | None = None, search: str | None = None, page: int | None = None) -> str:
    args = ["contacts", "list"]
    if search:
        args.extend(["--search", search])
    if page:
        args.extend(["--page", str(page)])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Create a new contact in Xero. Pass either inline name/email/phone OR "
        "a `data` dict matching the Xero contact schema (which gets passed via "
        "the CLI's --file flag using a temp JSON file). Inline form is the "
        "common case; use `data` for complex contacts with addresses, "
        "multiple phones, or persons.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  name: Contact display name (required if `data` not provided)\n"
        "  email: Primary email\n"
        "  phone: Primary phone\n"
        "  data: Optional dict matching Xero's CreateContact schema; overrides "
        "        the inline name/email/phone if both are present"
    )
)
async def contacts_create(
    profile: str | None = None,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    data: dict | None = None,
) -> str:
    if data:
        return json.dumps(await _file_action(["contacts", "create"], data, profile=profile), indent=2)
    if not name:
        return json.dumps({"_error": "must provide either `name` or `data`"})
    args = ["contacts", "create", "--name", name]
    if email:
        args.extend(["--email", email])
    if phone:
        args.extend(["--phone", phone])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Update an existing contact. Either pass `contact_id` + inline fields, "
        "or pass `data` containing the full update payload (must include the "
        "ContactID inside).\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  contact_id: Xero ContactID UUID (required if `data` not provided)\n"
        "  name, email, phone: Optional updated values\n"
        "  data: Optional dict matching Xero's UpdateContact schema"
    )
)
async def contacts_update(
    profile: str | None = None,
    contact_id: str | None = None,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    data: dict | None = None,
) -> str:
    if data:
        return json.dumps(await _file_action(["contacts", "update"], data, profile=profile), indent=2)
    if not contact_id:
        return json.dumps({"_error": "must provide either `contact_id` or `data`"})
    args = ["contacts", "update", "--contact-id", contact_id]
    if name:
        args.extend(["--name", name])
    if email:
        args.extend(["--email", email])
    if phone:
        args.extend(["--phone", phone])
    return json.dumps(await _xero(args, profile=profile), indent=2)


# ---------------------------------------------------------------------------
# Accounts / chart of accounts
# ---------------------------------------------------------------------------

@mcp.tool(description="List all accounts in the Chart of Accounts. Returns code, name, type, and tax type for each.")
async def accounts_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["accounts", "list"], profile=profile), indent=2)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "List invoices. Optionally filter by status or contact, paginate.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  status: Filter by invoice status (DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED, DELETED)\n"
        "  contact_id: Filter by ContactID\n"
        "  page: 1-based page index"
    )
)
async def invoices_list(
    profile: str | None = None,
    status: str | None = None,
    contact_id: str | None = None,
    page: int | None = None,
) -> str:
    args = ["invoices", "list"]
    if status:
        args.extend(["--status", status])
    if contact_id:
        args.extend(["--contact-id", contact_id])
    if page:
        args.extend(["--page", str(page)])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Create an invoice. Always use `data` (a dict matching Xero's CreateInvoice "
        "schema) — invoices have line items so the inline-flag form is impractical."
    )
)
async def invoices_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the invoice payload"})
    return json.dumps(await _file_action(["invoices", "create"], data, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Update an invoice. Pass `data` containing the full update payload "
        "including the InvoiceID."
    )
)
async def invoices_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["invoices", "update"], data, profile=profile), indent=2)


# ---------------------------------------------------------------------------
# Quotes / payments / items / bank-transactions / reports
# ---------------------------------------------------------------------------

@mcp.tool(description="List quotes (proposals). Optional filter by status.")
async def quotes_list(profile: str | None = None, status: str | None = None) -> str:
    args = ["quotes", "list"]
    if status:
        args.extend(["--status", status])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(description="List payments recorded against invoices.")
async def payments_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["payments", "list"], profile=profile), indent=2)


@mcp.tool(description="List inventory items / products.")
async def items_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["items", "list"], profile=profile), indent=2)


@mcp.tool(description="List bank transactions (Spend/Receive money against bank accounts).")
async def bank_transactions_list(profile: str | None = None, page: int | None = None) -> str:
    args = ["bank-transactions", "list"]
    if page:
        args.extend(["--page", str(page)])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Generate a Balance Sheet report. Point-in-time view of assets, "
        "liabilities and equity as of a given date.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  date: Report date (YYYY-MM-DD); defaults to today\n"
        "  periods: Number of comparison periods (e.g., 3 to compare to last 3)\n"
        "  timeframe: MONTH | QUARTER | YEAR — how comparison periods are spaced\n"
        "  payments_only: True to include only accounts with payments\n"
        "  standard_layout: True for Xero's standard layout instead of custom\n"
        "  tracking_option_id_1 / tracking_option_id_2: Filter by tracking option(s)"
    )
)
async def reports_balance_sheet(
    profile: str | None = None,
    date: str | None = None,
    periods: int | None = None,
    timeframe: str | None = None,
    payments_only: bool = False,
    standard_layout: bool = False,
    tracking_option_id_1: str | None = None,
    tracking_option_id_2: str | None = None,
) -> str:
    args = ["reports", "balance-sheet"]
    if date: args.extend(["--date", date])
    if periods: args.extend(["--periods", str(periods)])
    if timeframe: args.extend(["--timeframe", timeframe])
    if payments_only: args.append("--payments-only")
    if standard_layout: args.append("--standard-layout")
    if tracking_option_id_1: args.extend(["--tracking-option-id-1", tracking_option_id_1])
    if tracking_option_id_2: args.extend(["--tracking-option-id-2", tracking_option_id_2])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Generate a Profit & Loss report (income statement) for a date range.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  from_date: Period start (YYYY-MM-DD)\n"
        "  to_date: Period end (YYYY-MM-DD)\n"
        "  periods: Number of comparison periods\n"
        "  timeframe: MONTH | QUARTER | YEAR — comparison period spacing\n"
        "  payments_only: True to include only accounts with payments\n"
        "  standard_layout: True for Xero's standard layout"
    )
)
async def reports_profit_and_loss(
    profile: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    periods: int | None = None,
    timeframe: str | None = None,
    payments_only: bool = False,
    standard_layout: bool = False,
) -> str:
    args = ["reports", "profit-and-loss"]
    if from_date: args.extend(["--from", from_date])
    if to_date: args.extend(["--to", to_date])
    if periods: args.extend(["--periods", str(periods)])
    if timeframe: args.extend(["--timeframe", timeframe])
    if payments_only: args.append("--payments-only")
    if standard_layout: args.append("--standard-layout")
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Generate a Trial Balance report — every account's debit/credit balance "
        "as of a given date.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  date: Report date (YYYY-MM-DD); defaults to today\n"
        "  payments_only: True to include only accounts with payments"
    )
)
async def reports_trial_balance(
    profile: str | None = None,
    date: str | None = None,
    payments_only: bool = False,
) -> str:
    args = ["reports", "trial-balance"]
    if date: args.extend(["--date", date])
    if payments_only: args.append("--payments-only")
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Generate an Aged Receivables report for a specific contact — invoices "
        "this contact owes you, bucketed by age.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  contact_id: Xero ContactID (REQUIRED)\n"
        "  report_date: Date the report is run as of (YYYY-MM-DD)\n"
        "  from_date / to_date: Filter to invoices in this date range"
    )
)
async def reports_aged_receivables(
    profile: str | None = None,
    contact_id: str | None = None,
    report_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    if not contact_id:
        return json.dumps({"_error": "contact_id is required for aged-receivables"})
    args = ["reports", "aged-receivables", "--contact-id", contact_id]
    if report_date: args.extend(["--report-date", report_date])
    if from_date: args.extend(["--from-date", from_date])
    if to_date: args.extend(["--to-date", to_date])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Generate an Aged Payables report for a specific contact — bills you "
        "owe this contact, bucketed by age.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  contact_id: Xero ContactID (REQUIRED)\n"
        "  report_date: Date the report is run as of (YYYY-MM-DD)\n"
        "  from_date / to_date: Filter to bills in this date range"
    )
)
async def reports_aged_payables(
    profile: str | None = None,
    contact_id: str | None = None,
    report_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    if not contact_id:
        return json.dumps({"_error": "contact_id is required for aged-payables"})
    args = ["reports", "aged-payables", "--contact-id", contact_id]
    if report_date: args.extend(["--report-date", report_date])
    if from_date: args.extend(["--from-date", from_date])
    if to_date: args.extend(["--to-date", to_date])
    return json.dumps(await _xero(args, profile=profile), indent=2)


# ---------------------------------------------------------------------------
# Contact groups, credit notes, manual journals, tax rates, currencies,
# tracking, and the create/update variants of items / payments / bank
# transactions / quotes / accounts. All shells out to the same `xero <group>
# <action> --json` pattern; complex create/update payloads go via _file_action.
# ---------------------------------------------------------------------------

@mcp.tool(description="List contact groups (Xero's way of bucketing contacts for batch ops).")
async def contact_groups_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["contact-groups", "list"], profile=profile), indent=2)


@mcp.tool(description="List credit notes (refunds / overpayments owed back to customers or by you to suppliers).")
async def credit_notes_list(profile: str | None = None, page: int | None = None) -> str:
    args = ["credit-notes", "list"]
    if page: args.extend(["--page", str(page)])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(description="Create a credit note. Pass `data` matching Xero's CreateCreditNote schema (must include line items).")
async def credit_notes_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the credit-note payload"})
    return json.dumps(await _file_action(["credit-notes", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft credit note. Pass full update payload as `data`, must include CreditNoteID.")
async def credit_notes_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["credit-notes", "update"], data, profile=profile), indent=2)


@mcp.tool(description="List manual journals. Optional `modified_after` (YYYY-MM-DD) and pagination.")
async def manual_journals_list(profile: str | None = None, modified_after: str | None = None, page: int | None = None) -> str:
    args = ["manual-journals", "list"]
    if modified_after: args.extend(["--modified-after", modified_after])
    if page: args.extend(["--page", str(page)])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(description="Create a manual journal. Pass `data` with narration + at least 2 balanced journal lines.")
async def manual_journals_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing narration + manualJournalLines"})
    return json.dumps(await _file_action(["manual-journals", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft manual journal. Pass `data` with the full update payload including ManualJournalID.")
async def manual_journals_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["manual-journals", "update"], data, profile=profile), indent=2)


@mcp.tool(description="List tax rates configured in Xero (GST, HST, no-tax, etc.).")
async def tax_rates_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["tax-rates", "list"], profile=profile), indent=2)


@mcp.tool(description="List currencies enabled in this Xero org.")
async def currencies_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["currencies", "list"], profile=profile), indent=2)


@mcp.tool(description="List tracking categories (Xero's way of tagging transactions for departmental/regional reporting).")
async def tracking_categories_list(profile: str | None = None) -> str:
    return json.dumps(await _xero(["tracking", "categories", "list"], profile=profile), indent=2)


@mcp.tool(
    description=(
        "List tracking options for a specific tracking category.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  tracking_category_id: TrackingCategoryID (REQUIRED)"
    )
)
async def tracking_options_list(profile: str | None = None, tracking_category_id: str | None = None) -> str:
    if not tracking_category_id:
        return json.dumps({"_error": "tracking_category_id is required"})
    return json.dumps(
        await _xero(["tracking", "options", "list", "--tracking-category-id", tracking_category_id], profile=profile),
        indent=2,
    )


@mcp.tool(
    description=(
        "Record a payment against an existing invoice.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  invoice_id: Xero InvoiceID (REQUIRED)\n"
        "  account_id: Bank/clearing account ID the payment is FROM (REQUIRED)\n"
        "  amount: Payment amount, positive number (REQUIRED)\n"
        "  date: Payment date (YYYY-MM-DD); defaults to today\n"
        "  reference: Optional reference text shown on the payment"
    )
)
async def payments_create(
    profile: str | None = None,
    invoice_id: str | None = None,
    account_id: str | None = None,
    amount: float | None = None,
    date: str | None = None,
    reference: str | None = None,
) -> str:
    missing = [k for k, v in (("invoice_id", invoice_id), ("account_id", account_id), ("amount", amount)) if not v]
    if missing:
        return json.dumps({"_error": f"required args missing: {missing}"})
    args = ["payments", "create", "--invoice-id", invoice_id, "--account-id", account_id, "--amount", str(amount)]
    if date: args.extend(["--date", date])
    if reference: args.extend(["--reference", reference])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(description="Create an inventory item / product. Pass `data` matching Xero's CreateItem schema (code + name minimum).")
async def items_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` with at least Code and Name"})
    return json.dumps(await _file_action(["items", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update an inventory item. Pass `data` with ItemID + fields to update.")
async def items_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["items", "update"], data, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Create a bank transaction (Spend Money or Receive Money against a bank account).\n"
        "Pass `data` matching Xero's CreateBankTransaction schema (Type, BankAccount, Contact, LineItems).\n"
        "Type values: SPEND, RECEIVE, SPEND-TRANSFER, RECEIVE-TRANSFER, SPEND-PREPAYMENT, "
        "RECEIVE-PREPAYMENT, SPEND-OVERPAYMENT, RECEIVE-OVERPAYMENT."
    )
)
async def bank_transactions_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the bank-transaction payload"})
    return json.dumps(await _file_action(["bank-transactions", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a bank transaction. Pass `data` with BankTransactionID and the fields to update.")
async def bank_transactions_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["bank-transactions", "update"], data, profile=profile), indent=2)


@mcp.tool(description="Create a quote (proposal). Pass `data` matching Xero's CreateQuote schema (Contact + LineItems).")
async def quotes_create(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the quote payload"})
    return json.dumps(await _file_action(["quotes", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft quote. Pass `data` with QuoteID and fields to update.")
async def quotes_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["quotes", "update"], data, profile=profile), indent=2)


@mcp.tool(description="Update an account in the chart of accounts. Pass `data` with AccountID and fields to update.")
async def accounts_update(profile: str | None = None, data: dict | None = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["accounts", "update"], data, profile=profile), indent=2)


# ---------------------------------------------------------------------------
# Internals — pass JSON bodies via the CLI's --file flag using a temp file
# ---------------------------------------------------------------------------

async def _file_action(base_args: list[str], data: dict, profile: str | None = None) -> Any:
    """Some create/update commands take complex JSON bodies. The CLI accepts
    them via --file <path>. We dump `data` to a temp file, invoke the CLI,
    then clean up."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(data, tmp)
        tmp_path = tmp.name
    try:
        result = await _xero([*base_args, "--file", tmp_path], profile=profile)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
