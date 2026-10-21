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
    assert.doesNotMatch(workflow, /deploy-test|deploy-production|SALONOPS_TEST_AUTODEPLOY|SALONOPS_TEST_DEPLOY_MODE|SALONOPS_PROD_AUTODEPLOY|packages:\s*write|Publish only a tested/);
    const jobs = workflow.split(/^jobs:\s*$/m)[1];
    assert.ok(jobs);
    assert.deepEqual([...jobs.matchAll(/^  ([\w-]+):/gm)].map((match) => match[1]), ['checks']);
});
