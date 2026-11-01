const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/store_page.html'), 'utf8');
const loader = html.match(/<!-- Paystack Inline -->\s*<script>([\s\S]*?)<\/script>/)[1];

function browser(outcomes) {
  const window = {};
  const scripts = [];
  const context = {
    window,
    setTimeout: (fn, ms) => setTimeout(fn, ms === 10000 ? 20 : 0),
    clearTimeout,
    document: {
      createElement: () => ({ remove() { this.removed = true; } }),
      head: { appendChild(script) {
        scripts.push(script);
        const outcome = outcomes.shift() || 'error';
        queueMicrotask(() => {
          if (outcome === 'success') {
            window.PaystackPop = { setup() {} };
            script.onload();
          } else if (outcome === 'error') script.onerror();
          else if (outcome === 'invalid') script.onload();
        });
      } }
    }
  };
  vm.runInNewContext(loader, context);
  return { window, scripts };
}

test('checkout shares preload and reuses a ready library', async () => {
  const { window, scripts } = browser(['success']);
  const first = window.ensureStorePaystack();
  assert.equal(first, window.ensureStorePaystack());
  assert.equal(await first, window.PaystackPop);
  await window.ensureStorePaystack();
  assert.equal(scripts.length, 1);
});

test('failed and invalid downloads recover automatically', async () => {
  const { window, scripts } = browser(['error', 'invalid', 'success']);
  await window.ensureStorePaystack();
  assert.equal(scripts.length, 3);
  assert.ok(scripts[0].removed && scripts[1].removed);
});

test('timeouts stop after three attempts and a later click can recover', async () => {
  const outcomes = ['timeout', 'timeout', 'timeout'];
  const { window, scripts } = browser(outcomes);
  await assert.rejects(window.ensureStorePaystack(), /timed out/);
  assert.equal(scripts.length, 3);
  assert.ok(scripts.every(script => script.removed));
  outcomes.push('success');
  await window.ensureStorePaystack();
  assert.equal(scripts.length, 4);
});

test('all executable template scripts parse after replacing template expressions', () => {
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (/application\/(?:ld\+)?json/.test(match[1])) continue;
    const source = match[2].replace(/\{\{[\s\S]*?\}\}/g, 'null');
    new vm.Script(source);
  }
});

function checkout(ensureStorePaystack, fetch) {
  let click;
  const button = { addEventListener(event, fn) { click = fn; } };
  const inline = {};
  const messages = [];
  const source = html.slice(html.indexOf('    let paymentInProgress = false;'),
    html.indexOf('    if (btnProcess) btnProcess.addEventListener'));
  vm.runInNewContext(source, {
    window: { PAYSTACK_PUBLIC_KEY: 'pk_test', ensureStorePaystack },
    btnPay: button, inlineCheckoutButton: inline,
    cart: { _data: [{}] }, SLUG: 'test',
    ensurePaystackIdentityOrExplain: () => true,
    setLoading: (btn, on) => { btn.disabled = on; },
    tell: message => messages.push(message), fetch
  });
  return { click: () => click(), button, inline, messages };
}

test('repeated taps wait for the library and validation failure unlocks checkout', async () => {
  let resolve;
  let requests = 0;
  const ready = new Promise(done => { resolve = done; });
  const ui = checkout(() => ready, async () => {
    requests++;
    return { ok: false, json: async () => ({ success: false }) };
  });
  const first = ui.click();
  await ui.click();
  assert.equal(requests, 0);
  assert.equal(ui.button.disabled, true);
  assert.equal(ui.inline.disabled, true);
  resolve();
  await first;
  assert.equal(requests, 1);
  assert.equal(ui.button.disabled, false);
  assert.equal(ui.inline.disabled, false);
});

test('library failure unlocks checkout for a fresh attempt without sending payment requests', async () => {
  let loads = 0;
  const ui = checkout(async () => { loads++; throw new Error('offline'); }, () => {
    assert.fail('must not contact checkout before library is ready');
  });
  await ui.click();
  assert.equal(ui.button.disabled, false);
  await ui.click();
  assert.equal(loads, 2);
  assert.match(ui.messages.at(-1), /try again/);
});
