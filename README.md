# xero-mcp-wrapper

A thin MCP server that wraps the official [Xero command-line tool](https://github.com/XeroAPI/xero-command-line) so Claude Desktop (and any other MCP client) can read and write Xero data via PKCE OAuth — **no client secrets in your MCP config.**

## Why this exists

The previously-recommended `@xeroapi/xero-mcp-server` requires a Xero `CLIENT_SECRET` to live inside `claude_desktop_config.json`. The new `xero` CLI uses PKCE OAuth, encrypts tokens at rest with the macOS keychain, and supports multiple Xero organisations via named profiles. This wrapper inherits all of that — your MCP config has no secrets, and switching organisations is a `profile` argument on each tool call.

The wrapper is a small adapter: every tool shells out to `xero <group> <action> --json [-p <profile>]`, parses the result, and returns it. When Xero ships new commands in the CLI, you can extend this file by adding a few lines.

## Prerequisites

```bash
# 1. Install the CLI
npm install -g @xeroapi/xero-command-line

# 2. Install uv (handles the wrapper's Python deps automatically)
brew install uv

# 3. Configure at least one Xero profile + log in
#    Get the Client ID from a PKCE OAuth 2.0 app at developer.xero.com/app/manage
#    Set the redirect URI on that app to http://localhost:8742/callback
xero profile add ets --client-id YOUR_PKCE_APP_CLIENT_ID
xero login -p ets
xero org details -p ets --json    # confirm auth works
```

## Wire into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"xero-cli": {
  "command": "/opt/homebrew/bin/uv",
  "args": [
    "run",
    "--script",
    "/Users/justin/justin-mcp-servers/xero-mcp-wrapper/server.py"
  ]
}
```

Then ⌘Q + reopen Claude Desktop.

## Tools (37 total)

Every tool accepts an optional `profile: str` argument. If omitted, the CLI's default profile is used. To target a specific Xero organisation, pass e.g. `profile="ets"` or `profile="jumbo"`.

**Org & profiles**
- `org_details` — active organisation's details (currency, tax setup, etc.)
- `profiles_list` — configured local profiles (no API call)

**Contacts**
- `contacts_list` (search, page) · `contacts_create` (inline or `data`) · `contacts_update` (inline or `data`)
- `contact_groups_list`

**Accounts** (chart of accounts)
- `accounts_list` · `accounts_update` (data)

**Invoices · Credit Notes · Manual Journals · Bank Transactions** (full CRUD)
- `invoices_list` / `invoices_create` / `invoices_update`
- `credit_notes_list` / `credit_notes_create` / `credit_notes_update`
- `manual_journals_list` (modified_after, page) / `manual_journals_create` / `manual_journals_update`
- `bank_transactions_list` (page) / `bank_transactions_create` / `bank_transactions_update`

**Items**
- `items_list` · `items_create` · `items_update`

**Quotes**
- `quotes_list` · `quotes_create` · `quotes_update`

**Payments**
- `payments_list` · `payments_create` (inline: invoice_id, account_id, amount, date, reference)

**Reference data**
- `tax_rates_list` · `currencies_list` · `tracking_categories_list` · `tracking_options_list` (tracking_category_id)

**Reports** — each with the right param schema
- `reports_balance_sheet` (date, periods, timeframe, payments_only, standard_layout, tracking_option_id_1/2)
- `reports_profit_and_loss` (from_date, to_date, periods, timeframe, payments_only, standard_layout)
- `reports_trial_balance` (date, payments_only)
- `reports_aged_receivables` (contact_id required, report_date, from_date, to_date)
- `reports_aged_payables` (contact_id required, report_date, from_date, to_date)

## Why this matters for Canadian Xero orgs

The official `@xeroapi/xero-mcp-server` package only authenticates via Xero "Custom Connections" — and **custom connections are a US-only feature**. Canadian (and many other non-US) orgs literally cannot authenticate that way. The CLI's PKCE OAuth works for any Xero org regardless of region, which makes this wrapper the only viable MCP path for Canadian organisations like ETS.

## Multi-organisation usage

Set up one profile per Xero org you have access to:

```bash
xero profile add ets --client-id <ets-app-client-id>
xero profile add jumbo --client-id <jumbo-app-client-id>
xero profile add personal --client-id <personal-app-client-id>
xero login -p ets
xero login -p jumbo
xero login -p personal
```

Each profile gets its own encrypted token cache. Pass `profile="<name>"` to any tool to target that org. There's no need for separate MCP instances per org — the single `xero-cli` connector with a per-call `profile` argument covers everything.

## Adding more tools

The CLI has many commands that aren't yet wrapped (credit-notes, manual-journals, tax-rates, currencies, tracking categories, contact groups). To add one, follow the existing pattern:

```python
@mcp.tool(description="...")
async def my_new_tool(profile: str | None = None, ...) -> str:
    args = ["<group>", "<action>"]
    # ...append flags from kwargs...
    return json.dumps(await _xero(args, profile=profile), indent=2)
```

The `_xero()` helper handles `--json`, profile selection, and error capture. The `_file_action()` helper handles commands that take complex bodies via `--file`.

## License

MIT.
