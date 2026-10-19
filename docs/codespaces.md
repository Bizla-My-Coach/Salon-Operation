# Test dev in GitHub Codespaces

1. On GitHub, select the **dev** branch, then **Code → Codespaces → Create codespace on dev**.
2. Wait for the dev container to finish building. In its terminal, run:

   ```bash
   bash scripts/codespaces.sh
   ```

3. Open **Ports → 8000 → Open in Browser** (or the URL printed in the terminal).
   Keep the forwarded port **Private**, its default visibility.
4. Sign in with **admin / admin**. The other fictional accounts are **manager**,
   **stylist**, and **gm**, also with password **admin**, for testing each role.

The command applies migrations, creates the demo accounts and salon/service/SOP
data, adds three fictional reviews to **Tickets**, and starts the app. The admin
can see all tickets and use Django Admin; the manager has a sample ticket and its
notification. Use the same command after Ctrl+C or reopening the Codespace.
Restart it after editing Python code; the development server uses `--noreload`.

Repeated starts preserve accounts, changed passwords, ticket progress, and edited
sample data. PostgreSQL lives in this Codespace's own Docker volume. Deleting the
Codespace deletes its test environment; there is no production database connection.
The weak demo passwords are accepted only by this explicit development setup.

If a Codespace was created before this configuration existed, update `dev`, then
run **Codespaces: Rebuild Container** from the command palette once. No `.env` from
your server is needed. Initialization generates a separate ignored
`.devcontainer/.env` with random database credentials and Django secret, and the
exact forwarded hostname/CSRF origin. Do not copy server secrets into it.

The **Codespaces dev smoke test** GitHub Actions workflow checks this startup and
admin login on pushes to `dev` and pull requests into `dev` or `main`. It uses its own
disposable database and does not deploy the server or create a billed Codespace.
