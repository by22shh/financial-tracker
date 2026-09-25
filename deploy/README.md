# Production deployment

Existing server: `2.26.231.184`, project root `/opt/fintracker`.
The SSH identity must be provided by the operator; it is not stored in Git.

Prepare `/opt/fintracker/env/sheets.env` with mode `0600` using `.env.example`.
Reuse the existing Telegram token and OpenAI key from `env/runtime.env` on the server;
do not copy secrets into Git. Explicitly configure the permitted Telegram user IDs,
Google Apps Script URL and bridge secret, and the ASR provider/model/key.
Google Apps Script setup is documented in the root README.

From a clean committed checkout:

```bash
bash deploy/release.sh YOUR_SSH_ALIAS
```

The script uploads exactly `HEAD`, builds an image tagged with its commit, checks the
last worksheet connection and its category/date catalog and Telegram authentication, then stops the legacy api/worker/scheduler/
polling processes and starts the new bot. If validation fails, no processes are switched.
If startup fails after switching, the script attempts to restore the previous processes.
A webhook must be disabled before using polling; the script refuses to remove one silently.

The new bot uses the `fintracker-sheets` Compose project and its own persistent volume.
Legacy PostgreSQL volumes and server backups are retained. The local source archive was
removed at the user's request; previous committed versions remain in Git history.

After deployment:

```bash
cd /opt/fintracker/deploy-sheets
docker compose --env-file image.env -f compose.sheets.yml ps
docker compose --env-file image.env -f compose.sheets.yml logs --tail=30 bot
```

Readiness checks do not validate AI quality or perform a financial write. Test the full
text/voice flow on a dedicated worksheet before recording real expenses.

For an Apps Script write smoke test, add `integrations/google_sheets/Verify.gs` to the
project and run `verifyDeployment` in the editor. It creates a temporary worksheet,
checks decimal addition, preservation of formulas and duplicate delivery, then removes
its worksheet and receipt. It never writes to existing expense worksheets.
