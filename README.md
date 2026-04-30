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

## Tools

Every tool that talks to the Xero API accepts an optional `profile: str` argument. If omitted, the CLI's default profile is used. To target a specific Xero organisation, pass e.g. `profile="ets"` or `profile="jumbo"`.

| Tool | What |
|---|---|
| `org_details` | Get the active organisation's details (currency, tax setup, etc.) |
| `profiles_list` | List configured local profiles (no API call) |
| `contacts_list` | List contacts, optional `search` and `page` filters |
| `contacts_create` | Create a contact — inline `name`/`email`/`phone` or full `data` dict |
| `contacts_update` | Update a contact — `contact_id` + inline fields, or full `data` dict |
| `accounts_list` | List the chart of accounts |
| `invoices_list` | List invoices, optional `status` / `contact_id` / `page` |
| `invoices_create` | Create an invoice — `data` dict required (line items) |
| `invoices_update` | Update an invoice — `data` dict required |
| `quotes_list` | List quotes / proposals |
| `payments_list` | List payments |
| `items_list` | List inventory items |
| `bank_transactions_list` | List bank transactions |
| `reports_run` | Run a financial report (BalanceSheet, ProfitAndLoss, TrialBalance, AgedReceivables, etc.) |

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
