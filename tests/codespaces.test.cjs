const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

test('Codespaces initialization creates stable private secrets and exact forwarding origins', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'salonops-codespaces-'));
    try {
        const dir = path.join(root, '.devcontainer');
        fs.mkdirSync(dir);
        const script = path.join(dir, 'initialize.sh');
        fs.copyFileSync(path.join(__dirname, '../.devcontainer/initialize.sh'), script);
        const env = {...process.env, CODESPACE_NAME: 'fictional-test-123', GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN: 'app.github.dev'};
        assert.equal(spawnSync('bash', [script], {env}).status, 0);
        const filename = path.join(dir, '.env'), text = fs.readFileSync(filename, 'utf8');
        assert.match(text, /POSTGRES_DB=salonops_codespaces\n/);
        assert.match(text, /CSRF_TRUSTED_ORIGINS=https:\/\/fictional-test-123-8000\.app\.github\.dev\n/);
        assert.match(text, /SESSION_COOKIE_SECURE=True/);
        assert.match(text, /SECRET_KEY=[\w-]{80,}/);
        assert.equal(fs.statSync(filename).mode & 0o777, 0o600);
        assert.equal(spawnSync('bash', [script], {env}).status, 0);
        assert.equal(fs.readFileSync(filename, 'utf8'), text);
    } finally { fs.rmSync(root, {recursive: true, force: true}); }
});
