const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const workflow = fs.readFileSync(path.join(__dirname, '..', '.github', 'workflows', 'salonops.yml'), 'utf8');
function job(name) {
    const lines = workflow.split(/\r?\n/);
    const start = lines.indexOf(`  ${name}:`);
    assert.notEqual(start, -1, `Missing ${name} job`);
    let end = start + 1;
    while (end < lines.length && !/^  [\w-]+:/.test(lines[end])) end++;
    return lines.slice(start, end).join('\n');
}
function script(name) {
    return job(name).split('        run: |\n')[1].split('\n').map(line => line.slice(10)).join('\n');
}
const image = `ghcr.io/bizla-my-coach/salon-operation@sha256:${'a'.repeat(64)}`;

test('only successful main pushes can publish and deploy; CI stays off production', () => {
    assert.match(workflow, /  push:\s+branches: \[dev, main\]/);
    assert.match(workflow, /  pull_request:\s+branches: \[dev, main\]/);
    assert.match(job('checks'), /runs-on: ubuntu-latest/);
    assert.match(job('checks'), /python manage\.py test/);
    assert.match(job('checks'), /node --test tests\/\*\.test\.cjs/);
    for (const name of ['publish', 'deploy']) {
        assert.match(job(name), /if: github.event_name == 'push' && github.ref == 'refs\/heads\/main'/);
        assert.doesNotMatch(job(name), /always\(\)|continue-on-error/);
    }
    assert.match(job('publish'), /needs: checks/);
    assert.match(job('publish'), /runs-on: ubuntu-latest/);
    assert.match(job('publish'), /packages: write/);
    assert.match(job('deploy'), /needs: publish/);
    assert.match(job('deploy'), /runs-on: \[self-hosted, Linux, X64\]/);
    assert.match(job('deploy'), /environment: production/);
    assert.match(job('deploy'), /group: salonops-production\s+cancel-in-progress: false/);
    assert.match(job('deploy'), /packages: read/);
    assert.doesNotMatch(job('deploy'), /actions\/checkout|down -v|seed_demo|docker build/);
});

function sandbox(t) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'salonops-workflow-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    for (const dir of ['bin', 'etc/salonops', 'opt/salonops-prod/scripts', 'var/lib/salonops-prod/backups']) {
        fs.mkdirSync(path.join(root, dir), { recursive: true });
    }
    fs.writeFileSync(path.join(root, 'etc/salonops/prod.env'), 'SECRET_KEY=synthetic\nSALONOPS_IMAGE=old\n', { mode: 0o600 });
    fs.writeFileSync(path.join(root, 'etc/salonops/prod.restic.env'), 'synthetic');
    function executable(name, body) { fs.writeFileSync(path.join(root, name), `#!/bin/bash\nset -eu\n${body}\n`, { mode: 0o700 }); }
    executable('bin/docker', 'echo "docker $*" >> "$LOG"\nif [[ "$1" == login ]]; then cat >/dev/null; fi\nif [[ "$1 $2" == "image inspect" ]]; then echo "$RELEASE_IMAGE"; fi');
    executable('bin/curl', 'echo health >> "$LOG"');
    executable('opt/salonops-prod/scripts/deploy.sh', 'echo "deploy $*" >> "$LOG"\n[[ "${FAIL_DEPLOY:-0}" != 1 ]]');
    const env = { ...process.env, PATH: `${root}/bin:${process.env.PATH}`, LOG: `${root}/log`, RUNNER_TEMP: root,
        RUNNER_NAME: 'salonops-production-01', GHCR_TOKEN: 'synthetic', REGISTRY_USER: 'synthetic', RELEASE_IMAGE: image,
        GITHUB_SHA: 'b'.repeat(40), GITHUB_OUTPUT: `${root}/output`, GITHUB_STEP_SUMMARY: `${root}/summary` };
    function run(name, overrides = {}) {
        let body = script(name);
        for (const prefix of ['/etc/salonops', '/opt/salonops-prod', '/var/lib/salonops-prod']) body = body.replaceAll(prefix, root + prefix);
        return spawnSync('bash', ['-c', body], { env: { ...env, ...overrides }, encoding: 'utf8' });
    }
    return { root, run, log: () => fs.existsSync(`${root}/log`) ? fs.readFileSync(`${root}/log`, 'utf8') : '',
        config: () => fs.readFileSync(`${root}/etc/salonops/prod.env`, 'utf8') };
}

test('publisher emits immutable digest and removes temporary login files', t => {
    const s = sandbox(t);
    const result = s.run('publish');
    assert.equal(result.status, 0, result.stderr);
    assert.equal(fs.readFileSync(`${s.root}/output`, 'utf8'), `image=${image}\n`);
    assert.ok(s.log().indexOf('docker build') < s.log().indexOf('docker push'));
    assert.equal(fs.readdirSync(s.root).some(name => name.startsWith('salonops-registry.')), false);
});

test('production rejects wrong runners and non-digest images before Docker access', t => {
    const s = sandbox(t);
    assert.notEqual(s.run('deploy', { RUNNER_NAME: 'another-runner' }).status, 0);
    assert.notEqual(s.run('deploy', { RELEASE_IMAGE: 'synthetic:latest' }).status, 0);
    assert.equal(s.log(), '');
});

test('failed deployment preserves previous image and skips public health/success reporting', t => {
    const s = sandbox(t);
    assert.notEqual(s.run('deploy', { FAIL_DEPLOY: '1' }).status, 0);
    assert.match(s.config(), /SALONOPS_IMAGE=old/);
    assert.doesNotMatch(s.log(), /health/);
    assert.equal(fs.existsSync(`${s.root}/summary`), false);
    assert.equal(fs.readdirSync(s.root).some(name => name.startsWith('salonops-registry.')), false);
});

test('successful deployment uses fixed stack, persists digest privately, and verifies HTTPS', t => {
    const s = sandbox(t);
    const result = s.run('deploy');
    assert.equal(result.status, 0, result.stderr);
    assert.match(s.log(), /deploy .*prod.env salonops-prod .*compose.production.yaml .*backups .*prod.restic.env/);
    assert.ok(s.log().includes(image));
    assert.equal(s.config(), `SECRET_KEY=synthetic\nSALONOPS_IMAGE=${image}\n`);
    assert.equal(fs.statSync(`${s.root}/etc/salonops/prod.env`).mode & 0o777, 0o600);
    assert.match(s.log(), /health/);
    assert.equal(fs.readdirSync(s.root).some(name => name.startsWith('salonops-registry.')), false);
});
