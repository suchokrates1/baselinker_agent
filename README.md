# BaseLinker Print Agent

This repository contains `bl_api_print_agent.py`, a helper script for automating
printing of shipping labels from BaseLinker and sending order notifications via
Facebook Messenger.

The script polls BaseLinker for orders in a specific status, downloads the
shipping label, prints it using the `lp` command and notifies a Messenger user
about the order. Configuration is done through environment variables (usually
supplied via a `.env` file).

## Environment Variables

The following variables are supported:

| Variable | Description | Default |
| --- | --- | --- |
| `API_TOKEN` | BaseLinker API token. **Required**. | – |
| `PAGE_ACCESS_TOKEN` | Facebook Page access token used for sending Messenger messages. **Required**. | – |
| `RECIPIENT_ID` | Messenger recipient user ID. **Required**. | – |
| `STATUS_ID` | ID of the BaseLinker order status to monitor. | `91618` |
| `PRINTER_NAME` | Name of the printer used by the `lp` command. | `Xprinter` |
| `POLL_INTERVAL` | How often (in seconds) to poll for new orders. | `60` |
| `QUIET_HOURS_START` | Hour of the day (0‑23) when printing is paused. | `10` |
| `QUIET_HOURS_END` | Hour of the day when printing resumes. | `22` |
| `PRINTED_EXPIRY_DAYS` | How long to keep records of printed orders. | `5` |
| `LOG_LEVEL` | Logging verbosity. | `INFO` |
| `DATA_DB` | Path to the SQLite database file. | `data.db` in repo |

## Running

1. Install Python dependencies (requires Python 3):
   ```bash
   pip install requests python-dotenv
   ```
2. Copy `.env.example` to `.env` and fill in the required values.
3. Start the agent:
   ```bash
   python3 bl_api_print_agent.py
   ```

The script will continuously check BaseLinker, print new labels and send
Messenger notifications. During the configured quiet hours labels are queued and
printed later.

## Optional HTTP Server

A small HTTP server is started automatically on port `8082`. It provides a
single endpoint `GET /test` that sends a Messenger message for the last processed
order. This can be used to verify that Messenger integration works.

