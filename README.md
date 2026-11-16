# priority-mcp

MCP server for [Priority Software ERP](https://www.priority-software.com/)’s REST API, which is an OData service. It lists forms, describes their fields and subforms from `$metadata`, queries records, and can create, update, or delete them.

The server speaks [stdio](https://modelcontextprotocol.io) and [streamable HTTP](https://modelcontextprotocol.io), so a desktop host can launch it locally and a catalog can list a remote deployment.

```bash
pip install -e .
priority-mcp
```

## Configure

Set the service root and one way to sign in. Nothing is read from a file.

| Variable | Required | Purpose |
| --- | --- | --- |
| `PRIORITY_URL` | yes | `https://<server>/odata/Priority/<tabula.ini>/<company>` |
| `PRIORITY_USER` and `PRIORITY_PASSWORD` | one of these | HTTP Basic auth |
| `PRIORITY_PAT` | one of these | Personal access token. Sent as username = token, password = `PAT` |
| `PRIORITY_APP_ID` | no | `X-App-Id` for a per-application license |
| `PRIORITY_APP_KEY` | no | `X-App-Key` for a per-application license |
| `PRIORITY_READ_ONLY` | no | `1` refuses create, update, and delete |
| `PRIORITY_TIMEOUT` | no | Seconds before a call is given up. Default `180` (Priority’s own limit is 3 minutes) |
| `PRIORITY_MAX_RETRIES` | no | Extra attempts after HTTP 429. Default `4` |
| `PRIORITY_MAX_RESPONSE_BYTES` | no | Largest JSON body returned to the model. Default `5000000` |

The Priority administrator enables each upper-level form in **API Forms** (Limited Access). Subforms of an enabled form are included automatically. A system administrator can copy the service root from **Send Program Activation Link**.

`PRIORITY_PAT` wins when it is set. The token is the username and the password is the literal string `PAT`.

## Run

Stdio is the default. The process waits on stdin; a host such as Cursor or Claude Desktop starts it.

```bash
export PRIORITY_URL="https://<server>/odata/Priority/<tabula.ini>/<company>"
export PRIORITY_USER="apiuser"
export PRIORITY_PASSWORD="secret"
priority-mcp
```

Streamable HTTP listens on `http://127.0.0.1:8741/mcp`:

```bash
priority-mcp --transport http --host 0.0.0.0 --port 8741
```

The same switches exist as environment variables: `PRIORITY_MCP_TRANSPORT` (`stdio` or `http`), `PRIORITY_MCP_HOST`, `PRIORITY_MCP_PORT`, `PRIORITY_MCP_PATH`.

`--stateless` (`PRIORITY_MCP_STATELESS=1`) drops MCP session affinity so more than one process can sit behind a load balancer. `--json-response` (`PRIORITY_MCP_JSON_RESPONSE=1`) answers each POST with one JSON body instead of a server-sent event stream. Clients that expect the session-based streamable HTTP handshake should leave both off.

Binding `127.0.0.1` turns on DNS-rebinding checks for localhost. On a public address those checks stay off until you set `--allowed-hosts` (or `PRIORITY_MCP_ALLOWED_HOSTS`) to a comma-separated list such as `priority.example.com:*,localhost:*`.

The HTTP listener does not authenticate MCP clients. Put it on a private network, or terminate TLS and authenticate at a reverse proxy. Anyone who can open the endpoint can use the Priority credentials of the server process.

`--read-only` is the right switch for a shared or production-reporting deployment.

A container runs the HTTP transport:

```bash
docker build -t priority-mcp .
docker run --rm -p 8741:8741 \
  -e PRIORITY_URL -e PRIORITY_USER -e PRIORITY_PASSWORD \
  -e PRIORITY_READ_ONLY=1 \
  priority-mcp
```

## Tools

| Tool | Reads | What it does |
| --- | --- | --- |
| `list_entities` | yes | Entity sets from the OData service document. Optional `grep` |
| `describe_entity` | yes | Keys, fields, and subforms from `GetMetadataFor` (or `$metadata` on older servers) |
| `query_entity` | yes | `$filter`, `$select`, `$expand`, `$orderby`, `$top` (default 50), `$skip` |
| `get_record` | yes | One record. `key` is a single value or `KEY=value` pairs; `keys` is a composite-key object |
| `get_subform` | yes | Rows of a navigation property such as `ORDERITEMS_SUBFORM` |
| `create_record` | no | `POST`. `data` may include subform arrays |
| `update_record` | no | `PATCH`. Only fields in `data` change |
| `delete_record` | no | `DELETE` |

Write tools are annotated `readOnlyHint: false` and `destructiveHint: true`. Create is not idempotent. Update and delete are. Hosts that honor annotations should ask before running them.

Names are case-sensitive and almost always `UPPERCASE`. A unique case-insensitive `describe_entity` match is accepted and the canonical name is returned in `name_note`.

Dates use `YYYY-MM-DDTHH:MM:SS+HH:MM`. From Priority 21 the company timezone (`TZSERVER`) can differ from the server. Results are capped by `MAXAPILINES` (2,000 by default on 25.1+). Page with `top` and `skip`.

Every created or updated record, including each subform line, counts as a billed API transaction.

Priority Cloud throttles a tenant to about 100 calls per minute and 10 concurrent requests. HTTP 429 is retried with exponential backoff (1s, 2s, 4s, …, capped at 30s). A `Retry-After` header is honored up to 120 seconds. The error that remains after the retries names the status, the URL, the API message, and what to do next.

Common forms:

| Entity | Key | Notes |
| --- | --- | --- |
| `CUSTOMERS` | `CUSTNAME` | `CUSTDES` is the description |
| `SUPPLIERS` | `SUPNAME` | |
| `ORDERS` | `ORDNAME` | Subform `ORDERITEMS_SUBFORM` |
| `LOGPART` | `PARTNAME` | Parts |
| `AINVOICES`, `CINVOICES` | | Invoices |
| `PORDERS` | | Purchase orders |
| `DOCUMENTS_D` | | Shipments |
| `CPROF` | | Price quotes |

```text
query_entity CUSTOMERS  select=CUSTNAME,CUSTDES  top=10
query_entity ORDERS  filter=CUSTNAME eq '1001' and CURDATE ge 2025-01-01T00:00:00Z
                     select=ORDNAME,TOTPRICE  orderby=CURDATE desc
get_record   ORDERS  key=SO26000001  expand=ORDERITEMS_SUBFORM($select=PARTNAME,TQUANT,PRICE)
get_subform  ORDERS  key=SO26000001  subform=ORDERITEMS_SUBFORM
get_record   CINVOICESCONT  keys={"DEBIT":"D","IVNUM":"T4862"}
```

## Cursor

`.cursor/mcp.json` (project) or the MCP settings file (user):

```json
{
  "mcpServers": {
    "priority-erp": {
      "command": "priority-mcp",
      "env": {
        "PRIORITY_URL": "https://<server>/odata/Priority/<tabula.ini>/<company>",
        "PRIORITY_USER": "apiuser",
        "PRIORITY_PASSWORD": "secret"
      }
    }
  }
}
```

A remote streamable HTTP server:

```json
{
  "mcpServers": {
    "priority-erp": {
      "url": "https://priority-mcp.example.com/mcp"
    }
  }
}
```

## Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "priority-erp": {
      "command": "priority-mcp",
      "env": {
        "PRIORITY_URL": "https://<server>/odata/Priority/<tabula.ini>/<company>",
        "PRIORITY_PAT": "<personal-access-token>"
      }
    }
  }
}
```

## Claude Code

`.mcp.json` in the project (or `~/.claude.json` for the user):

```json
{
  "mcpServers": {
    "priority-erp": {
      "command": "priority-mcp",
      "env": {
        "PRIORITY_URL": "https://<server>/odata/Priority/<tabula.ini>/<company>",
        "PRIORITY_USER": "apiuser",
        "PRIORITY_PASSWORD": "secret",
        "PRIORITY_READ_ONLY": "1"
      }
    }
  }
}
```

## Public sandbox

Priority publishes a Version 25 sandbox in the [REST API docs](https://prioritysoftware.github.io/restapi/):

```bash
export PRIORITY_URL="https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo"
export PRIORITY_USER="apidemo"
export PRIORITY_PASSWORD="123"
export PRIORITY_READ_ONLY="1"
priority-mcp
```

The company is shared. Leave it read-only. Writes are visible to everyone else using the sandbox and still count as API transactions on that tenant.

## Development

```bash
pip install -e ".[dev]"
pytest                      # unit tests, plus read-only sandbox tests
pytest -m "not integration" # offline
PRIORITY_SKIP_INTEGRATION=1 pytest
```

Integration tests call `list_entities`, `describe_entity`, `query_entity`, `get_record`, and `get_subform` on the public sandbox. The test client is read-only and rejects any non-GET before it is sent.

## License

MIT. See [LICENSE](LICENSE).

## Built by BiAiVision

priority-connect is maintained by [BiAiVision](https://biaivision.com), which builds production AI systems for Israeli businesses on top of Priority ERP: AI assistants, sales and budget forecasting, and OCR for accounting documents.

- Hebrew setup guide: [How to connect Claude or ChatGPT to Priority ERP via MCP](https://biaivision.com/blog/connect-claude-priority/)
- [Priority AI by BiAiVision](https://biaivision.com/priority-ai/)
