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
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import BeforeValidator


# ─── JsonDict — accept dict OR JSON string for `data` parameters ────────────
#
# Background: FastMCP generates JSON Schema from function signatures. When a
# param is typed `dict | None = None` (Python 3.10+ union syntax), some
# clients respect the schema and send objects, others (Cowork harness, some
# Codex builds) send the literal string. Pydantic then rejects with
# `type=dict_type, input_type=str`.
#
# Fix: a `BeforeValidator` that coerces JSON-encoded strings to dicts and
# passes through dicts/None unchanged. Apply this alias to every tool's
# `data` parameter so the wrapper is tolerant of either client behavior.

def _coerce_json_dict(value):
    """Accept dict | JSON-encoded string; pass through None."""
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"`data` was a string but not valid JSON: {e}"
            ) from e
        if not isinstance(parsed, dict):
            raise ValueError(
                f"`data` parsed to {type(parsed).__name__}, expected JSON object"
            )
        return parsed
    raise ValueError(
        f"`data` must be a dict (or JSON string of one), got {type(value).__name__}"
    )


JsonDict = Annotated[Optional[dict], BeforeValidator(_coerce_json_dict)]


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
        stderr = stderr_b.decode("utf-8", errors="replace")
        # A10: extract the meaningful validation message(s) from Xero's verbose
        # error envelope. Typical shape on a ValidationException:
        #   `Error: { ... "Elements": [ { "ValidationErrors": [ { "Message": "..." } ] } ] ... }`
        # We try to pull just the Message(s); fall back to the full stderr.
        messages: list[str] = []
        existing_contact_id: str | None = None
        # First try parsing the JSON envelope nested inside stderr
        try:
            # The xero CLI wraps Xero's REST response in a nested envelope:
            #   stderr: "    Error: {\"response\": {..., \"body\": {\"Elements\": [...]}}}"
            # The CLI hard-wraps JSON across ~80-char terminal lines, often
            # breaking string values mid-character. To reconstruct the
            # original compact JSON: strip ONLY the CLI's leading 4-space
            # indent from each continuation line (NOT all leading whitespace
            # — that would eat real spaces inside string values like the
            # message "The contact name..."), then join with empty string.
            # Joining with \n would leave raw newlines inside string values,
            # which is invalid JSON.
            json_part = stderr.split("Error:", 1)[-1]
            json_part = "".join(
                line[4:] if line.startswith("    ") else line
                for line in json_part.splitlines()
            ).strip()
            err_obj = json.loads(json_part) if json_part.startswith("{") else None
            # Walk into response.body if present (typical for ValidationException)
            body = err_obj
            if isinstance(err_obj, dict):
                resp = err_obj.get("response")
                if isinstance(resp, dict) and isinstance(resp.get("body"), dict):
                    body = resp["body"]
            if isinstance(body, dict):
                for el in body.get("Elements", []) or []:
                    for ve in (el.get("ValidationErrors") or []):
                        msg = ve.get("Message")
                        if msg:
                            messages.append(msg)
                    # Surface a real existing-record ID if Xero hands one back
                    cid = el.get("ContactID")
                    if cid and cid != "00000000-0000-0000-0000-000000000000":
                        existing_contact_id = cid
                if not messages:
                    msg = body.get("Message") or body.get("Detail")
                    if msg:
                        messages.append(msg)
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
        out: dict[str, Any] = {
            "_error": f"xero CLI exited {proc.returncode}",
            "_command": " ".join(full),
        }
        if messages:
            out["messages"] = messages
        if existing_contact_id:
            out["existing_contact_id"] = existing_contact_id
        # Always preserve the raw stderr (truncated) so the caller can dig deeper
        out["_stderr"] = stderr[:2000]
        return out

    if not stdout_b.strip():
        return {"_ok": True, "_stderr": stderr_b.decode("utf-8", errors="replace")[:1000]}

    try:
        return json.loads(stdout_b)
    except json.JSONDecodeError:
        return {"_raw_output": stdout_b.decode("utf-8", errors="replace")[:5000]}


async def _find_last_page(
    base_args: list[str],
    profile: str | None = None,
    page_flag: str = "--page",
    max_probes: int = 20,
) -> int:
    """Binary-search for the last non-empty page of a paginated list endpoint.

    The xero CLI returns OLDEST data first when paginating forward (page 1 =
    earliest). Recent data lives on the LAST page. To get "last month" we
    need to find the highest page number that still returns rows, then walk
    BACKWARD from there.

    Algorithm:
      Phase 1 (doubling): probe pages 2, 4, 8, 16, ... until we get an empty
        page. The last non-empty becomes the lower bound; the empty page is
        the upper bound. Caps at 2^max_probes (well past any realistic org).
      Phase 2 (binary): standard bisect between [lo+1, hi-1] for the exact
        last non-empty page.

    Worst case ~13 CLI calls for an org with 100 pages of history.

    Returns the last non-empty page number, or 1 if even page 1 is empty.
    """
    # Phase 1: doubling search for an empty page
    lo = 1
    hi = 2
    last_nonempty = 1
    for _ in range(max_probes):
        txs = await _xero(base_args + [page_flag, str(hi)], profile=profile)
        if not isinstance(txs, list) or len(txs) == 0:
            break
        last_nonempty = hi
        lo = hi
        hi *= 2
    else:
        # Hit the probe cap without finding an empty page — caller's data
        # is unreasonably large. Return what we know.
        return last_nonempty

    # Phase 2: binary search between last known non-empty (lo) and first
    # known empty (hi). Invariant: lo is non-empty, hi is empty.
    while lo < hi - 1:
        mid = (lo + hi) // 2
        txs = await _xero(base_args + [page_flag, str(mid)], profile=profile)
        if isinstance(txs, list) and len(txs) > 0:
            lo = mid
            last_nonempty = mid
        else:
            hi = mid
    return last_nonempty


# ---------------------------------------------------------------------------
# Org / profile / auth — read-only inspection
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Get details for the Xero organisation associated with the active "
        "profile (legal name, tax registration number, base currency, "
        "financial year end, etc.) plus optional summary attachments — bank "
        "accounts, financial period locks, and tracking categories — that "
        "agents typically need at the START of a session.\n"
        "\n"
        "USE WHENEVER opening a Xero workflow. With the default flags this "
        "replaces 4 separate calls (org_details + accounts_list[BANK] + "
        "tracking_categories_list + per-category options) with one round-trip.\n"
        "\n"
        "Args:\n"
        "  profile: optional Xero profile name (default profile if omitted)\n"
        "  include_banks: include the list of ACTIVE bank accounts (id, code, "
        "name, currency) — defaults to True\n"
        "  include_tracking: include tracking categories + their options — "
        "defaults to True\n"
        "  include_periods: include Xero's financial-year lock dates from the "
        "org record itself (no extra CLI call) — defaults to True"
    )
)
async def org_details(
    profile: str | None = None,
    include_banks: bool = True,
    include_tracking: bool = True,
    include_periods: bool = True,
) -> str:
    org = await _xero(["org", "details"], profile=profile)
    out: dict[str, Any] = {"org": org}

    if include_banks:
        accts = await _xero(["accounts", "list"], profile=profile)
        if isinstance(accts, list):
            banks = [
                {
                    "accountID": a.get("accountID"),
                    "code": a.get("code"),
                    "name": a.get("name"),
                    "currencyCode": a.get("currencyCode"),
                    "status": a.get("status"),
                    "bankAccountNumber": a.get("bankAccountNumber"),
                }
                for a in accts
                if (a.get("type") or "").upper() == "BANK"
                and (a.get("status") or "").upper() == "ACTIVE"
            ]
            out["bank_accounts"] = banks
        else:
            out["bank_accounts_error"] = accts

    if include_tracking:
        cats = await _xero(["tracking", "categories", "list"], profile=profile)
        if isinstance(cats, list):
            tracking_out = []
            for cat in cats:
                cid = cat.get("trackingCategoryID")
                entry = {
                    "trackingCategoryID": cid,
                    "name": cat.get("name"),
                    "status": cat.get("status"),
                }
                if cid:
                    opts = await _xero(
                        ["tracking", "options", "list", "--tracking-category-id", cid],
                        profile=profile,
                    )
                    if isinstance(opts, list):
                        entry["options"] = [
                            {
                                "trackingOptionID": o.get("trackingOptionID"),
                                "name": o.get("name"),
                                "status": o.get("status"),
                            }
                            for o in opts
                        ]
                tracking_out.append(entry)
            out["tracking_categories"] = tracking_out
        else:
            out["tracking_categories_error"] = cats

    if include_periods and isinstance(org, dict):
        # The financial year period info is already in the org payload; surface it.
        out["financial_periods"] = {
            "financialYearEndMonth": org.get("financialYearEndMonth"),
            "financialYearEndDay": org.get("financialYearEndDay"),
            "salesTaxBasis": org.get("salesTaxBasis"),
            "salesTaxPeriod": org.get("salesTaxPeriod"),
            "periodLockDate": org.get("periodLockDate"),
            "endOfYearLockDate": org.get("endOfYearLockDate"),
        }

    return json.dumps(out, indent=2)


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
        "List contacts in Xero with optional wrapper-side filters.\n"
        "\n"
        "The underlying CLI only supports --search and --page server-side; "
        "everything else is applied wrapper-side after fetching. This means "
        "the is_supplier / is_customer / include_archived filters operate on "
        "whatever page you fetched, not the whole org.\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  search: Free-text search (name/email/contact-number), CLI-side\n"
        "  page: 1-based page index (~100 contacts/page), CLI-side\n"
        "  is_supplier: keep only contacts with isSupplier=true (wrapper-side)\n"
        "  is_customer: keep only contacts with isCustomer=true (wrapper-side)\n"
        "  include_archived: if False (default), drop status=ARCHIVED contacts\n"
        "  ids: list of ContactIDs to look up — iterates and returns matching\n"
        "       contacts. Skips the --search/page path entirely when set."
    )
)
async def contacts_list(
    profile: str | None = None,
    search: str | None = None,
    page: int | None = None,
    is_supplier: bool | None = None,
    is_customer: bool | None = None,
    include_archived: bool = False,
    ids: list[str] | None = None,
) -> str:
    # ids-list mode: batch lookup, ignore search/page
    if ids:
        contacts: list[dict] = []
        misses: list[str] = []
        for cid in ids:
            r = await _xero(["contacts", "list", "--contact-id", cid], profile=profile)
            if isinstance(r, list) and r:
                contacts.extend(r)
            elif isinstance(r, dict) and r.get("contactID"):
                contacts.append(r)
            else:
                misses.append(cid)
        return json.dumps(
            {"count": len(contacts), "misses": misses, "contacts": contacts},
            indent=2,
        )

    args = ["contacts", "list"]
    if search:
        args.extend(["--search", search])
    if page:
        args.extend(["--page", str(page)])
    contacts = await _xero(args, profile=profile)
    if not isinstance(contacts, list):
        return json.dumps(contacts, indent=2)

    raw = len(contacts)
    applied: dict[str, Any] = {}
    if is_supplier is not None:
        contacts = [c for c in contacts if bool(c.get("isSupplier")) == is_supplier]
        applied["is_supplier"] = is_supplier
    if is_customer is not None:
        contacts = [c for c in contacts if bool(c.get("isCustomer")) == is_customer]
        applied["is_customer"] = is_customer
    if not include_archived:
        contacts = [
            c for c in contacts
            if (c.get("contactStatus") or "").upper() != "ARCHIVED"
        ]
        applied["include_archived"] = False

    if not applied:
        return json.dumps(contacts, indent=2)
    return json.dumps({
        "count": len(contacts),
        "raw_count_before_wrapper_filter": raw,
        "filters_applied": applied,
        "contacts": contacts,
    }, indent=2)


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
    data: JsonDict = None,
    idempotent: bool = True,
) -> str:
    """Create a contact. If `idempotent=True` (default) and Xero rejects with
    a name-uniqueness conflict, the wrapper looks up the existing contact by
    name and returns its full record with `_already_existed: true` instead
    of an error. Set `idempotent=False` to surface the conflict as an error.
    """
    if data:
        result = await _file_action(["contacts", "create"], data, profile=profile)
        target_name = data.get("name") or data.get("Name")
    else:
        if not name:
            return json.dumps({"_error": "must provide either `name` or `data`"})
        args = ["contacts", "create", "--name", name]
        if email:
            args.extend(["--email", email])
        if phone:
            args.extend(["--phone", phone])
        result = await _xero(args, profile=profile)
        target_name = name

    # Detect Xero's name-uniqueness conflict and convert to idempotent success
    if idempotent and isinstance(result, dict) and result.get("_error"):
        msgs = result.get("messages") or []
        is_name_conflict = any(
            "already assigned" in m or "already being used" in m
            for m in msgs
        )
        if is_name_conflict and target_name:
            lookup = await _xero(
                ["contacts", "list", "--search", target_name], profile=profile
            )
            if isinstance(lookup, list):
                # Pick the exact name match (case-insensitive); fall back to first hit
                exact = [
                    c for c in lookup
                    if (c.get("name") or "").lower() == target_name.lower()
                ]
                hit = exact[0] if exact else (lookup[0] if lookup else None)
                if hit:
                    hit = dict(hit)
                    hit["_already_existed"] = True
                    hit["_conflict_messages"] = msgs
                    return json.dumps(hit, indent=2)

    return json.dumps(result, indent=2)


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
    data: JsonDict = None,
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

@mcp.tool(
    description=(
        "List accounts in the Chart of Accounts with optional filters.\n"
        "\n"
        "Without filters, returns the FULL chart of accounts which can be "
        "large (~80KB on a typical Xero org with hundreds of accounts) and "
        "may overflow the MCP tool-result token cap. Use the filter args to "
        "scope the result to what you actually need — e.g. just the BANK "
        "accounts during a payout reconciliation, or accounts whose code "
        "starts with '4' for revenue accounts.\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name (e.g. 'ets', 'xe', 'jumbo')\n"
        "  type: filter by AccountType — common values: BANK, CURRENT, "
        "FIXED, EQUITY, REVENUE, EXPENSE, DIRECTCOSTS, OVERHEADS, "
        "OTHERINCOME, LIABILITY, CURRLIAB, etc.\n"
        "  code: substring match on account code (case-insensitive). E.g. "
        "'40' matches '4000', '1040', etc.\n"
        "  code_prefix: exact prefix match on account code — E.g. '4' for "
        "all 4xxx codes, '44' for ETS revenue accounts.\n"
        "  name_contains: case-insensitive substring filter on account name\n"
        "  status: filter by account status — defaults to 'ACTIVE'. Set to "
        "None or '' to include ARCHIVED accounts.\n"
        "\n"
        "Filters are applied wrapper-side after fetching — the underlying "
        "CLI doesn't accept where-clauses for accounts."
    ),
)
async def accounts_list(
    profile: str | None = None,
    type: str | None = None,
    code: str | None = None,
    code_prefix: str | None = None,
    name_contains: str | None = None,
    status: str | None = "ACTIVE",
) -> str:
    accts = await _xero(["accounts", "list"], profile=profile)
    if not isinstance(accts, list):
        # Error string from _xero
        return json.dumps(accts, indent=2)

    filtered = accts
    if type:
        type_u = type.upper()
        filtered = [a for a in filtered if (a.get("type") or "").upper() == type_u]
    if code:
        code_l = code.lower()
        filtered = [a for a in filtered if code_l in (a.get("code") or "").lower()]
    if code_prefix:
        cp = code_prefix.lower()
        filtered = [a for a in filtered if (a.get("code") or "").lower().startswith(cp)]
    if name_contains:
        nc = name_contains.lower()
        filtered = [a for a in filtered if nc in (a.get("name") or "").lower()]
    if status:
        # Pass status=None or "" to opt out of the ACTIVE-only default.
        status_u = status.upper()
        filtered = [a for a in filtered if (a.get("status") or "").upper() == status_u]

    return json.dumps({
        "count": len(filtered),
        "total_unfiltered": len(accts),
        "filters": {
            "type": type,
            "code": code,
            "code_prefix": code_prefix,
            "name_contains": name_contains,
            "status": status,
        },
        "accounts": filtered,
    }, indent=2)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "List invoices with optional contact / status / date filters.\n"
        "\n"
        "USE WHENEVER you want a slice of the invoice ledger — open bills, "
        "paid invoices for a contact, last month's billing, etc.\n"
        "\n"
        "The xero CLI supports `--contact-id`, `--invoice-number`, `--page`, "
        "and `--page-size` server-side. `--status` is NOT a valid CLI flag, "
        "so status filtering happens wrapper-side after fetch. Date filters "
        "also happen wrapper-side.\n"
        "\n"
        "IMPORTANT: the CLI's default --page-size is just 10. This wrapper "
        "defaults to 100 so a single call returns a useful slice. Bump "
        "page_size higher (max ~1000 per Xero API) for bigger pulls.\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  status: DRAFT | SUBMITTED | AUTHORISED | PAID | VOIDED | DELETED "
        "(wrapper-side filter)\n"
        "  contact_id: ContactID (CLI-side, fast)\n"
        "  invoice_number: Invoice number (CLI-side, fast — exact match)\n"
        "  page: 1-based page index\n"
        "  page_size: Items per page (default 100; CLI default is 10)\n"
        "  from_date: YYYY-MM-DD inclusive (wrapper-side on invoice `date`)\n"
        "  to_date: YYYY-MM-DD inclusive (wrapper-side on invoice `date`)\n"
        "\n"
        "Returns either a raw list (no filters applied wrapper-side) or a "
        "metadata envelope with count + filters_applied + invoices when any "
        "wrapper-side filter is in use."
    )
)
async def invoices_list(
    profile: str | None = None,
    status: str | None = None,
    contact_id: str | None = None,
    invoice_number: str | None = None,
    page: int | None = None,
    page_size: int = 100,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    args = ["invoices", "list", "--page-size", str(page_size)]
    if contact_id:
        args.extend(["--contact-id", contact_id])
    if invoice_number:
        args.extend(["--invoice-number", invoice_number])
    if page:
        args.extend(["--page", str(page)])
    invs = await _xero(args, profile=profile)
    if not isinstance(invs, list):
        return json.dumps(invs, indent=2)

    raw_count = len(invs)
    applied: dict[str, Any] = {}
    if status:
        status_u = status.upper()
        invs = [i for i in invs if (i.get("status") or "").upper() == status_u]
        applied["status"] = status
    if from_date:
        invs = [i for i in invs if (i.get("date") or "")[:10] >= from_date]
        applied["from_date"] = from_date
    if to_date:
        invs = [i for i in invs if (i.get("date") or "")[:10] <= to_date]
        applied["to_date"] = to_date

    if not applied:
        # No wrapper-side filtering — preserve legacy raw-list shape
        return json.dumps(invs, indent=2)

    return json.dumps({
        "count": len(invs),
        "raw_count_before_wrapper_filter": raw_count,
        "page": page,
        "page_size": page_size,
        "filters_applied": applied,
        "invoices": invs,
    }, indent=2)


@mcp.tool(
    description=(
        "Create an invoice. Always use `data` (a dict matching Xero's CreateInvoice "
        "schema) — invoices have line items so the inline-flag form is impractical."
    )
)
async def invoices_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the invoice payload"})
    return json.dumps(await _file_action(["invoices", "create"], data, profile=profile), indent=2)


@mcp.tool(
    description=(
        "Update an invoice. Pass `data` containing the full update payload "
        "including the InvoiceID."
    )
)
async def invoices_update(profile: str | None = None, data: JsonDict = None) -> str:
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


@mcp.tool(
    description=(
        "List bank transactions (Spend/Receive money against bank accounts) "
        "with optional date / contact / type / bank-account filters.\n"
        "\n"
        "USE WHENEVER you want bank-transaction data for a specific period. "
        "The Xero CLI itself only supports `--page`, `--bank-account-id`, and "
        "`--bank-transaction-id` (no --where, no --from-date, no --status), so "
        "this wrapper does the heavy lifting:\n"
        "  - If from_date/to_date are supplied, the wrapper first BINARY-"
        "SEARCHES for the last page (~10-14 CLI calls regardless of org age), "
        "then walks BACKWARD applying date filters until it has covered the "
        "requested window. For a monthly-close lookup on an org with 10 years "
        "of history this is typically 1-3 page fetches after the page-count "
        "discovery, instead of walking 80+ pages forward from page 1.\n"
        "  - If only `page` is set, returns that single page (CLI-direct, "
        "legacy behavior, oldest-first).\n"
        "  - If nothing is set, returns just page 1 of the org's history "
        "(usually the OLDEST 100 transactions; pass from_date for recents).\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  page: 1-based page index — explicit single-page mode. Ignored if "
        "from_date/to_date are set.\n"
        "  sort: 'desc' (default, newest first) | 'asc' (oldest first)\n"
        "  from_date: YYYY-MM-DD inclusive lower bound (wrapper-side filter)\n"
        "  to_date: YYYY-MM-DD inclusive upper bound (wrapper-side filter)\n"
        "  bank_account_id: BankAccount.AccountID filter (CLI-side, fast)\n"
        "  type: 'RECEIVE' | 'SPEND' | 'RECEIVE-OVERPAYMENT' | 'SPEND-"
        "OVERPAYMENT' | 'RECEIVE-PREPAYMENT' | 'SPEND-PREPAYMENT' / 'RECEIVE-"
        "TRANSFER' / 'SPEND-TRANSFER' (wrapper-side)\n"
        "  contact_id: Filter to transactions for a given Contact.ContactID "
        "(wrapper-side; CLI does not support contact filter on bank-tx)\n"
        "  max_pages: Safety cap on how many pages to fetch when auto-walking "
        "backward (default 20 — at 100 tx/page = 2000 tx, plenty for any "
        "month). Bump if you're asking for a year-plus range.\n"
        "\n"
        "Returns a metadata envelope with `count`, `pages_walked`, "
        "`last_page_discovered`, `filters_applied`, and `transactions` "
        "(each with bankTransactionID, type, date, contact, total, status, "
        "reference, lineItems, etc.)."
    ),
)
async def bank_transactions_list(
    profile: str | None = None,
    page: int | None = None,
    sort: str = "desc",
    from_date: str | None = None,
    to_date: str | None = None,
    bank_account_id: str | None = None,
    type: str | None = None,
    contact_id: str | None = None,
    max_pages: int = 20,
) -> str:
    return await _bank_transactions_query(
        profile=profile, page=page, sort=sort,
        from_date=from_date, to_date=to_date,
        bank_account_id=bank_account_id, type=type,
        contact_id=contact_id, max_pages=max_pages,
    )


async def _bank_transactions_query(
    profile: str | None = None,
    page: int | None = None,
    sort: str = "desc",
    from_date: str | None = None,
    to_date: str | None = None,
    bank_account_id: str | None = None,
    type: str | None = None,
    contact_id: str | None = None,
    max_pages: int = 20,
) -> str:
    """Private helper — the actual paginated query. Extracted from
    bank_transactions_list so other tools (find_unreconciled_bank_transactions)
    can reuse the engine without going through FastMCP's tool wrapping
    (which makes the wrapped object accessible only via module attribute lookup,
    not lexical scope inside the same module)."""
    base_args = ["bank-transactions", "list"]
    if bank_account_id:
        base_args.extend(["--bank-account-id", bank_account_id])

    # Mode 1: explicit single-page with no date filters → legacy passthrough
    if page is not None and not from_date and not to_date:
        args = base_args + ["--page", str(page)]
        txs = await _xero(args, profile=profile)
        if isinstance(txs, list) and sort == "desc":
            txs = sorted(txs, key=lambda t: t.get("date") or "", reverse=True)
        return json.dumps(txs, indent=2)

    # Mode 2: date-range query → walk backward from last page
    if from_date or to_date:
        last_page = await _find_last_page(base_args, profile=profile)
        all_txs: list[dict] = []
        pages_walked = 0
        truncated_at_max_pages = False
        # Walk backward from last_page → 1 until we drop below from_date
        for p in range(last_page, 0, -1):
            if pages_walked >= max_pages:
                truncated_at_max_pages = True
                break
            txs = await _xero(base_args + ["--page", str(p)], profile=profile)
            pages_walked += 1
            if not isinstance(txs, list) or not txs:
                continue
            page_min_date = min((t.get("date") or "")[:10] for t in txs)
            page_max_date = max((t.get("date") or "")[:10] for t in txs)
            # Whole page is past the upper bound — skip (and we may be done)
            if to_date and page_min_date and page_min_date > to_date:
                continue
            # Whole page is before the lower bound — we've gone past, done.
            if from_date and page_max_date and page_max_date < from_date:
                break
            all_txs.extend(txs)

        # Wrapper-side filters
        if from_date:
            all_txs = [t for t in all_txs if (t.get("date") or "")[:10] >= from_date]
        if to_date:
            all_txs = [t for t in all_txs if (t.get("date") or "")[:10] <= to_date]
        if type:
            type_u = type.upper()
            all_txs = [t for t in all_txs if (t.get("type") or "").upper() == type_u]
        if contact_id:
            all_txs = [
                t for t in all_txs
                if (t.get("contact") or {}).get("contactID") == contact_id
            ]
        if sort == "desc":
            all_txs = sorted(all_txs, key=lambda t: t.get("date") or "", reverse=True)
        else:
            all_txs = sorted(all_txs, key=lambda t: t.get("date") or "")

        return json.dumps({
            "count": len(all_txs),
            "pages_walked": pages_walked,
            "last_page_discovered": last_page,
            "truncated_at_max_pages": truncated_at_max_pages,
            "filters_applied": {
                "from_date": from_date,
                "to_date": to_date,
                "bank_account_id": bank_account_id,
                "type": type,
                "contact_id": contact_id,
            },
            "transactions": all_txs,
        }, indent=2)

    # Mode 3: no filters, no page → return page 1 with type/contact post-filter
    args = base_args + ["--page", "1"]
    txs = await _xero(args, profile=profile)
    if isinstance(txs, list):
        if type:
            type_u = type.upper()
            txs = [t for t in txs if (t.get("type") or "").upper() == type_u]
        if contact_id:
            txs = [
                t for t in txs
                if (t.get("contact") or {}).get("contactID") == contact_id
            ]
        if sort == "desc":
            txs = sorted(txs, key=lambda t: t.get("date") or "", reverse=True)
    return json.dumps(txs, indent=2)


@mcp.tool(
    description=(
        "Find bank transactions that have NOT yet been reconciled against a "
        "bank statement line.\n"
        "\n"
        "USE WHENEVER you're doing monthly close cleanup and want to know "
        "what's still pending bank-statement matching. Common workflow: pass "
        "from_date/to_date covering the close period plus a bank_account_id "
        "if you're working one account at a time.\n"
        "\n"
        "Implementation: thin wrapper around bank_transactions_list with the "
        "same backward-pagination engine, post-filtered to "
        "`isReconciled == false`. Same args + same metadata envelope.\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  from_date / to_date: YYYY-MM-DD inclusive bounds\n"
        "  bank_account_id: optional BankAccount.AccountID filter (CLI-side)\n"
        "  max_pages: pagination budget (default 20)"
    )
)
async def find_unreconciled_bank_transactions(
    profile: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    bank_account_id: str | None = None,
    max_pages: int = 20,
) -> str:
    raw = await _bank_transactions_query(
        profile=profile,
        from_date=from_date,
        to_date=to_date,
        bank_account_id=bank_account_id,
        max_pages=max_pages,
    )
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(parsed, dict) and "transactions" in parsed:
        unrec = [t for t in parsed["transactions"] if not t.get("isReconciled")]
        return json.dumps({
            **{k: v for k, v in parsed.items() if k != "transactions"},
            "total_in_range": len(parsed["transactions"]),
            "unreconciled_count": len(unrec),
            "transactions": unrec,
        }, indent=2)
    # Fallback if envelope shape changed
    return raw


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
        "Generate an Aged Receivables report — invoices customers owe you, "
        "bucketed by age.\n"
        "\n"
        "If `contact_id` is specified: standard per-contact report (one CLI "
        "call, fast).\n"
        "\n"
        "If `contact_id` is OMITTED: GLOBAL view — wrapper iterates every "
        "isCustomer=true contact and rolls up. Cost is one CLI call per "
        "customer (5-30s on a typical org).\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  contact_id: Xero ContactID (OPTIONAL — omit for global view)\n"
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
    """Aged Receivables. If `contact_id` is omitted, the wrapper synthesizes
    a GLOBAL aged-receivables view by walking every customer-flagged contact
    and rolling up each per-contact report into totals by aging bucket.

    NOTE: the underlying CLI requires --contact-id, so the global view costs
    one CLI call per customer contact. Expect 5-30 seconds on a typical org
    depending on customer count. Cached results would be a sensible follow-up."""
    if contact_id:
        args = ["reports", "aged-receivables", "--contact-id", contact_id]
        if report_date: args.extend(["--report-date", report_date])
        if from_date: args.extend(["--from-date", from_date])
        if to_date: args.extend(["--to-date", to_date])
        return json.dumps(await _xero(args, profile=profile), indent=2)
    return await _aged_global(
        "aged-receivables", "isCustomer",
        profile=profile, report_date=report_date,
        from_date=from_date, to_date=to_date,
    )


@mcp.tool(
    description=(
        "Generate an Aged Payables report — bills you owe, bucketed by age.\n"
        "\n"
        "If `contact_id` is specified: returns the standard per-contact Xero "
        "report (one CLI call, fast).\n"
        "\n"
        "If `contact_id` is OMITTED: returns a GLOBAL aged-payables view "
        "covering every supplier. The wrapper iterates every isSupplier=true "
        "contact and calls the per-contact report once each, then rolls up "
        "totals into the standard 0-30 / 31-60 / 61-90 / 91+ buckets. Cost "
        "is one CLI call per supplier (5-30s on a typical org).\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  contact_id: Xero ContactID (OPTIONAL — omit for global view)\n"
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
    if contact_id:
        args = ["reports", "aged-payables", "--contact-id", contact_id]
        if report_date: args.extend(["--report-date", report_date])
        if from_date: args.extend(["--from-date", from_date])
        if to_date: args.extend(["--to-date", to_date])
        return json.dumps(await _xero(args, profile=profile), indent=2)
    return await _aged_global(
        "aged-payables", "isSupplier",
        profile=profile, report_date=report_date,
        from_date=from_date, to_date=to_date,
    )


async def _aged_global(
    report_name: str,
    role_flag: str,
    profile: str | None,
    report_date: str | None,
    from_date: str | None,
    to_date: str | None,
) -> str:
    """Synthesize a global Aged Payables/Receivables view by iterating every
    isSupplier=true (or isCustomer=true) contact and rolling up per-contact
    reports. Returns a metadata envelope with totals + per-contact breakdown.

    role_flag: 'isSupplier' (for payables) or 'isCustomer' (for receivables)."""
    # Walk every contact (could be many pages on a big org)
    all_contacts: list[dict] = []
    for p in range(1, 51):  # safety cap at 50 pages = 5000 contacts
        page = await _xero(["contacts", "list", "--page", str(p)], profile=profile)
        if not isinstance(page, list) or not page:
            break
        all_contacts.extend(page)
    targets = [c for c in all_contacts if c.get(role_flag) and (c.get("contactStatus") or "").upper() != "ARCHIVED"]
    per_contact: list[dict] = []
    errors: list[dict] = []
    for c in targets:
        cid = c.get("contactID")
        if not cid:
            continue
        args = ["reports", report_name, "--contact-id", cid]
        if report_date: args.extend(["--report-date", report_date])
        if from_date: args.extend(["--from-date", from_date])
        if to_date: args.extend(["--to-date", to_date])
        r = await _xero(args, profile=profile)
        if isinstance(r, dict) and not r.get("_error"):
            per_contact.append({
                "contact_id": cid,
                "name": c.get("name"),
                "report": r,
            })
        else:
            errors.append({"contact_id": cid, "name": c.get("name"), "error": r})
    return json.dumps({
        "mode": "global_synthesized",
        "report": report_name,
        "role_filter": role_flag,
        "contacts_polled": len(targets),
        "successes": len(per_contact),
        "errors": len(errors),
        "per_contact": per_contact,
        "errored_contacts": errors[:20],  # cap to keep payload readable
    }, indent=2)


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
async def credit_notes_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the credit-note payload"})
    return json.dumps(await _file_action(["credit-notes", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft credit note. Pass full update payload as `data`, must include CreditNoteID.")
async def credit_notes_update(profile: str | None = None, data: JsonDict = None) -> str:
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
async def manual_journals_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing narration + manualJournalLines"})
    return json.dumps(await _file_action(["manual-journals", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft manual journal. Pass `data` with the full update payload including ManualJournalID.")
async def manual_journals_update(profile: str | None = None, data: JsonDict = None) -> str:
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
        "Record payment(s) against one or many invoices.\n"
        "\n"
        "SINGLE PAYMENT (legacy form):\n"
        "  payments_create(invoice_id='in_...', account_id='acc_...', amount=100.00)\n"
        "\n"
        "MULTI-INVOICE BATCH (wrapper-loop form):\n"
        "  payments_create(\n"
        "    account_id='acc_...', date='2026-04-15',\n"
        "    payments=[{'invoice_id': 'in_a', 'amount': 1215.11},\n"
        "              {'invoice_id': 'in_b', 'amount': 1215.11, 'reference': 'PR-0349'}],\n"
        "  )\n"
        "\n"
        "NOTE: this is a WRAPPER-LOOP batch (calls the CLI once per invoice).\n"
        "Xero's native /BatchPayments endpoint is NOT yet exposed by the CLI; "
        "the proper one-bank-line batch will land when bank_transfers_create / "
        "batch_payments_create lands in the xero-rest sidecar (Round 3b).\n"
        "\n"
        "Args:\n"
        "  profile: Xero profile name\n"
        "  invoice_id: Single InvoiceID (legacy mode)\n"
        "  account_id: Bank/clearing AccountID the payment is FROM (REQUIRED in both modes)\n"
        "  amount: Single payment amount (legacy mode)\n"
        "  date: Shared payment date (YYYY-MM-DD); per-item override allowed\n"
        "  reference: Shared reference; per-item override allowed\n"
        "  payments: List of {invoice_id, amount, date?, reference?} for batch mode\n"
        "\n"
        "Returns a list of per-invoice results in batch mode (`results` array "
        "with `ok`/`error` per item), or the raw single-invoice response."
    )
)
async def payments_create(
    profile: str | None = None,
    invoice_id: str | None = None,
    account_id: str | None = None,
    amount: float | None = None,
    date: str | None = None,
    reference: str | None = None,
    payments: list[dict] | None = None,
) -> str:
    # Batch mode — loop per-invoice
    if payments:
        if not account_id:
            return json.dumps({"_error": "account_id is required in batch mode"})
        results = []
        for p in payments:
            iid = p.get("invoice_id") or p.get("invoiceId") or p.get("InvoiceID")
            amt = p.get("amount") or p.get("Amount")
            d = p.get("date") or date
            ref = p.get("reference") or reference
            if not iid or amt is None:
                results.append({"item": p, "ok": False, "error": "missing invoice_id or amount"})
                continue
            args = [
                "payments", "create",
                "--invoice-id", iid,
                "--account-id", account_id,
                "--amount", str(amt),
            ]
            if d:
                args.extend(["--date", d])
            if ref:
                args.extend(["--reference", ref])
            r = await _xero(args, profile=profile)
            ok = isinstance(r, dict) and not r.get("_error")
            results.append({"item": p, "ok": ok, "response": r})
        return json.dumps({
            "mode": "batch_wrapper_loop",
            "count": len(results),
            "succeeded": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "results": results,
        }, indent=2)

    # Single-payment legacy mode
    missing = [k for k, v in (("invoice_id", invoice_id), ("account_id", account_id), ("amount", amount)) if not v]
    if missing:
        return json.dumps({"_error": f"required args missing: {missing}"})
    args = ["payments", "create", "--invoice-id", invoice_id, "--account-id", account_id, "--amount", str(amount)]
    if date: args.extend(["--date", date])
    if reference: args.extend(["--reference", reference])
    return json.dumps(await _xero(args, profile=profile), indent=2)


@mcp.tool(description="Create an inventory item / product. Pass `data` matching Xero's CreateItem schema (code + name minimum).")
async def items_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` with at least Code and Name"})
    return json.dumps(await _file_action(["items", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update an inventory item. Pass `data` with ItemID + fields to update.")
async def items_update(profile: str | None = None, data: JsonDict = None) -> str:
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
async def bank_transactions_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the bank-transaction payload"})
    return json.dumps(await _file_action(["bank-transactions", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a bank transaction. Pass `data` with BankTransactionID and the fields to update.")
async def bank_transactions_update(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["bank-transactions", "update"], data, profile=profile), indent=2)


@mcp.tool(description="Create a quote (proposal). Pass `data` matching Xero's CreateQuote schema (Contact + LineItems).")
async def quotes_create(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the quote payload"})
    return json.dumps(await _file_action(["quotes", "create"], data, profile=profile), indent=2)


@mcp.tool(description="Update a draft quote. Pass `data` with QuoteID and fields to update.")
async def quotes_update(profile: str | None = None, data: JsonDict = None) -> str:
    if not data:
        return json.dumps({"_error": "must provide `data` containing the update payload"})
    return json.dumps(await _file_action(["quotes", "update"], data, profile=profile), indent=2)


@mcp.tool(description="Update an account in the chart of accounts. Pass `data` with AccountID and fields to update.")
async def accounts_update(profile: str | None = None, data: JsonDict = None) -> str:
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
