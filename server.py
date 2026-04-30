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
        "Run a Xero financial report. The CLI exposes a fixed set of report "
        "names — pass one of them, plus optional date params depending on the "
        "report. Common reports: BalanceSheet, ProfitAndLoss, TrialBalance, "
        "AgedReceivables, AgedPayables, BankSummary.\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  report: Report name (case-insensitive; CLI normalises)\n"
        "  date: Effective date (YYYY-MM-DD) for point-in-time reports\n"
        "  from_date / to_date: Period bounds (YYYY-MM-DD) for P&L-style reports\n"
        "  contact_id: For aged-receivables/payables-by-contact"
    )
)
async def reports_run(
    profile: str | None = None,
    report: str | None = None,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    contact_id: str | None = None,
) -> str:
    if not report:
        return json.dumps({"_error": "must provide a `report` name"})
    args = ["reports", report.lower()]
    if date:
        args.extend(["--date", date])
    if from_date:
        args.extend(["--from-date", from_date])
    if to_date:
        args.extend(["--to-date", to_date])
    if contact_id:
        args.extend(["--contact-id", contact_id])
    return json.dumps(await _xero(args, profile=profile), indent=2)


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
