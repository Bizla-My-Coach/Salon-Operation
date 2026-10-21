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
        assert.match(text, /CSRF_TRUSTED_ORIGINS=https:\/\/fictional-test-123-8000\.app\.github\.dev,http:\/\/localhost:8000,http:\/\/127\.0\.0\.1:8000\n/);
        assert.match(text, /SESSION_COOKIE_SECURE=True/);
        assert.match(text, /SECRET_KEY=[\w-]{80,}/);
        assert.equal(fs.statSync(filename).mode & 0o777, 0o600);
        assert.equal(spawnSync('bash', [script], {env}).status, 0);
        assert.equal(fs.readFileSync(filename, 'utf8'), text);
    } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test('Both startup commands refresh trusted origins without changing HTTPS cookie settings', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'salonops-codespaces-start-'));
    try {
        fs.mkdirSync(path.join(root, 'scripts'));
        fs.copyFileSync(path.join(__dirname, '../scripts/codespace.sh'), path.join(root, 'scripts/codespace.sh'));
        fs.copyFileSync(path.join(__dirname, '../scripts/codespaces.sh'), path.join(root, 'scripts/codespaces.sh'));
        const bin = path.join(root, 'bin');
        fs.mkdirSync(bin);
        fs.writeFileSync(path.join(bin, 'python'), '#!/bin/sh\nprintf "%s\\n%s\\n%s\\n" "$CSRF_TRUSTED_ORIGINS" "$SESSION_COOKIE_SECURE" "$CSRF_COOKIE_SECURE"\nexit 42\n', {mode: 0o755});
        for (const [name, script] of ['fictional-test-123', ''].flatMap(name =>
            ['codespace.sh', 'codespaces.sh'].map(script => [name, script]))) {
            const env = {...process.env, PATH: `${bin}:${process.env.PATH}`, CODESPACE_NAME: name,
                GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN: 'app.github.dev',
                CSRF_TRUSTED_ORIGINS: 'https://old-8000.app.github.dev',
                SESSION_COOKIE_SECURE: 'True', CSRF_COOKIE_SECURE: 'True'};
            const result = spawnSync('bash', [path.join(root, 'scripts', script)], {env, encoding: 'utf8', cwd: os.tmpdir()});
            assert.equal(result.status, 42);
            const origins = (name ? 'https://fictional-test-123-8000.app.github.dev,' : '') +
                'http://localhost:8000,http://127.0.0.1:8000';
            assert.equal(result.stdout, `${origins}\nTrue\nTrue\n`);
        }
    } finally { fs.rmSync(root, {recursive: true, force: true}); }
});
