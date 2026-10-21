const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const workflow = fs.readFileSync(path.join(__dirname, '..', '.github', 'workflows', 'salonops.yml'), 'utf8');

function job(name) {
    const lines = workflow.split(/\r?\n/);
    const start = lines.indexOf(`  ${name}:`);
    assert.notEqual(start, -1, `Missing ${name} job`);
    let end = start + 1;
    while (end < lines.length && !/^  [\w-]+:/.test(lines[end])) end++;
    return lines.slice(start, end).join('\n');
}

test('CI checks pushes and pull requests for the two permanent branches only', () => {
    assert.match(workflow, /  push:\s+branches: \[dev, main\]/);
    assert.match(workflow, /  pull_request:\s+branches: \[dev, main\]/);
    assert.doesNotMatch(workflow, /refs\/heads\/prod\b/);
    const block = job('checks');
    assert.match(block, /python manage\.py test/);
    assert.match(block, /node --test tests\/\*\.test\.cjs/);
    assert.doesNotMatch(workflow, /deploy-test|SALONOPS_TEST_AUTODEPLOY|SALONOPS_TEST_DEPLOY_MODE/);
});

test('production deployment targets main without renaming persistent resources', () => {
    const block = job('deploy-production');
    assert.ok(block.includes("if: github.event_name == 'push' && github.ref == 'refs/heads/main'"));
    assert.doesNotMatch(block, /SALONOPS_PROD_AUTODEPLOY/);
    assert.match(block, /runs-on: \[self-hosted, Linux, X64, salonops-production\]/);
    assert.match(block, /environment: production/);
    assert.match(block, /needs: checks/);
    assert.match(block, /root=\/opt\/salonops-prod/);
    assert.match(block, /\/etc\/salonops\/prod\.env salonops-prod/);
    assert.match(block, /\/var\/lib\/salonops-prod\/backups/);
    assert.doesNotMatch(block, /deploy_preview|compose\.preview|SALONOPS_TEST_DEPLOY_MODE|deploy_test_https|compose\.test/);
});
