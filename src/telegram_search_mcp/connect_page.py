"""Static assets of the loopback connection screen: HTML, JS and CSS as string constants.

Nothing here is secret or per-run: the token travels in the URL fragment and the QR comes from
/api/state, so every asset is identical for every run. The strict CSP forbids inline script and
style, which is why the markup has no style attributes and all behaviour lives in APP_JS.
"""
from __future__ import annotations
import html
import json
import re

from . import product_identity as identity

PRODUCT = identity.DISPLAY_NAME

STRINGS = {
    'en': {
        'title': PRODUCT,
        'disclosure': 'Uses the Telegram API. Not an official Telegram app.',
        'connecting': 'Connecting…',
        'preparing': 'Preparing the QR code…',
        'finishing': 'Finishing the connection…',
        'cancelling': 'Cancelling…',
        'qrTitle': 'Scan to connect your Telegram account',
        'step1': 'Open Telegram on your phone',
        'step2': 'Settings → Devices → Link Desktop Device',
        'step3': 'Scan this code',
        'qrAlt': 'QR code for linking this device to your Telegram account',
        'timeLeft': 'Time left',
        'cancel': 'Cancel',
        'pwTitle': 'Two-step verification',
        'pwIntro': 'Enter your Telegram cloud password to finish connecting.',
        'pwLabel': 'Password',
        'pwHint': 'Hint:',
        'pwSubmit': 'Continue',
        'pwChecking': 'Checking…',
        'pwError': 'That password was not accepted. Try again.',
        'sendFailed': 'Could not send the password. Try again.',
        'readyTitle': 'Connected. You can close this tab and return to your assistant.',
        'failedTitle': 'Connection failed',
        'failedText': 'Run connect again from your assistant.',
        'retryAfter': 'Telegram asked to wait. Try again in {n} min.',
        'codeLabel': 'Code:',
        'expiredTitle': 'This screen has expired',
        'cancelledTitle': 'Connection cancelled',
        'retryText': 'Run connect again from your assistant.',
        'closedTitle': 'Screen closed',
        'closedText': ('This connection screen is no longer available. If you still need to connect, '
                       'run connect again from your assistant.'),
    },
    'ru': {
        'title': PRODUCT,
        'disclosure': 'Использует Telegram API. Не является официальным приложением Telegram.',
        'connecting': 'Подключение…',
        'preparing': 'Готовим QR-код…',
        'finishing': 'Завершаем подключение…',
        'cancelling': 'Отменяем…',
        'qrTitle': 'Отсканируйте код, чтобы подключить аккаунт Telegram',
        'step1': 'Откройте Telegram на телефоне',
        'step2': 'Настройки → Устройства → Подключить устройство',
        'step3': 'Отсканируйте этот код',
        'qrAlt': 'QR-код для привязки этого устройства к вашему аккаунту Telegram',
        'timeLeft': 'Осталось',
        'cancel': 'Отмена',
        'pwTitle': 'Двухэтапная проверка',
        'pwIntro': 'Введите облачный пароль Telegram, чтобы завершить подключение.',
        'pwLabel': 'Пароль',
        'pwHint': 'Подсказка:',
        'pwSubmit': 'Продолжить',
        'pwChecking': 'Проверяем…',
        'pwError': 'Пароль не подошёл. Попробуйте ещё раз.',
        'sendFailed': 'Не удалось отправить пароль. Попробуйте ещё раз.',
        'readyTitle': 'Подключено. Можно закрыть эту вкладку и вернуться к ассистенту.',
        'failedTitle': 'Не удалось подключиться',
        'failedText': 'Запустите connect заново из вашего ассистента.',
        'retryAfter': 'Telegram попросил подождать. Повторите через {n} мин.',
        'codeLabel': 'Код:',
        'expiredTitle': 'Время экрана истекло',
        'cancelledTitle': 'Подключение отменено',
        'retryText': 'Запустите connect заново из вашего ассистента.',
        'closedTitle': 'Экран закрыт',
        'closedText': ('Этот экран подключения больше недоступен. Если подключение всё ещё '
                       'нужно, запустите connect заново из вашего ассистента.'),
    },
}

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<title>{{title}}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/app.css">
<script src="/app.js" defer></script>
</head>
<body>
<main class="card">
<header class="brand">
<h1 data-i18n="title">{{title}}</h1>
<p class="disclosure" data-i18n="disclosure">{{disclosure}}</p>
</header>
<div class="sr-only" id="announce" role="status" aria-live="polite"></div>

<section class="view" id="view-wait" aria-labelledby="wait-title" aria-busy="true">
<div class="spinner" aria-hidden="true"></div>
<h2 id="wait-title" class="status">{{connecting}}</h2>
</section>

<section class="view" id="view-qr" aria-labelledby="qr-title" hidden>
<h2 id="qr-title" data-i18n="qrTitle">{{qrTitle}}</h2>
<ol class="steps">
<li data-i18n="step1">{{step1}}</li>
<li data-i18n="step2">{{step2}}</li>
<li data-i18n="step3">{{step3}}</li>
</ol>
<div class="qr-frame"><img id="qr-img" class="qr" width="264" height="264" alt="{{qrAlt}}"></div>
<p class="timer"><span data-i18n="timeLeft">{{timeLeft}}</span> <span class="countdown" id="qr-countdown" role="timer" aria-live="off">--:--</span></p>
<button type="button" class="btn btn-quiet" id="qr-cancel" data-i18n="cancel">{{cancel}}</button>
</section>

<section class="view" id="view-password" aria-labelledby="pw-title" hidden>
<h2 id="pw-title" data-i18n="pwTitle">{{pwTitle}}</h2>
<p data-i18n="pwIntro">{{pwIntro}}</p>
<form id="pw-form" class="form">
<label for="pw-input" data-i18n="pwLabel">{{pwLabel}}</label>
<input id="pw-input" name="password" type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" maxlength="900" required aria-describedby="pw-hint pw-error">
<p id="pw-hint" class="hint" hidden></p>
<p id="pw-error" class="error" role="alert" hidden></p>
<button type="submit" class="btn btn-primary" id="pw-submit">{{pwSubmit}}</button>
<button type="button" class="btn btn-quiet" id="pw-cancel" data-i18n="cancel">{{cancel}}</button>
</form>
<p class="timer"><span data-i18n="timeLeft">{{timeLeft}}</span> <span class="countdown" id="pw-countdown" role="timer" aria-live="off">--:--</span></p>
</section>

<section class="view" id="view-end" aria-labelledby="end-title" hidden>
<div class="icon" id="end-icon" aria-hidden="true"></div>
<h2 id="end-title"></h2>
<p id="end-text" hidden></p>
<p id="end-code" class="muted" hidden></p>
</section>
</main>
</body>
</html>
"""


def _render_html():
    english = STRINGS['en']
    return re.sub(r'\{\{(\w+)\}\}', lambda match: html.escape(english[match.group(1)]), _HTML_TEMPLATE)


INDEX_HTML = _render_html()

_JS_TEMPLATE = r"""'use strict';
(function () {
  var STRINGS = __STRINGS__;
  var lang = String(navigator.language || '').toLowerCase().indexOf('ru') === 0 ? 'ru' : 'en';
  var T = STRINGS[lang];
  var KNOWN = ['starting', 'waiting_for_scan', 'waiting_for_password', 'authorizing', 'handing_off',
               'ready', 'failed', 'expired', 'cancelled'];
  var TERMINAL = ['ready', 'failed', 'expired', 'cancelled'];
  var POLL_MS = 1000;
  var MAX_FAILURES = 3;
  var STORE_KEY = 'tgmcp-connect';

  function $(id) { return document.getElementById(id); }
  function stored(op, value) {
    try {
      if (op === 'get') { return window.sessionStorage.getItem(STORE_KEY) || ''; }
      if (op === 'set') { window.sessionStorage.setItem(STORE_KEY, value); }
      if (op === 'clear') { window.sessionStorage.removeItem(STORE_KEY); }
    } catch (e) { /* storage may be blocked; the screen still works until reloaded */ }
    return '';
  }

  // The token arrives only in the fragment (never sent to the server or in a referrer); take it,
  // then drop the fragment from the address bar. sessionStorage lets a reload of this tab continue.
  var token = '';
  try {
    var match = /^#t=([A-Za-z0-9_-]{16,128})$/.exec(window.location.hash);
    if (match) { token = match[1]; stored('set', token); } else { token = stored('get'); }
    if (window.location.hash) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  } catch (e) { /* keep whatever token was read */ }

  document.documentElement.lang = lang;
  document.title = T.title;
  var tagged = document.querySelectorAll('[data-i18n]');
  for (var i = 0; i < tagged.length; i += 1) {
    var text = T[tagged[i].getAttribute('data-i18n')];
    if (typeof text === 'string') { tagged[i].textContent = text; }
  }
  $('qr-img').alt = T.qrAlt;
  $('pw-submit').textContent = T.pwSubmit;

  var views = { wait: $('view-wait'), qr: $('view-qr'), password: $('view-password'), end: $('view-end') };
  var input = $('pw-input');
  var submit = $('pw-submit');
  var current = '';
  var announced = '';
  var lastState = null;
  var sequence = 0;
  var failures = 0;
  var timer = null;
  var ticker = null;
  var done = false;
  var cancelling = false;
  var submitting = false;
  var localError = false;
  var wasBusy = true;
  var deadline = 0;
  var shownQr = '';

  function announce() {
    var heading = current && views[current].querySelector('h2');
    var text = heading ? heading.textContent : '';
    if (text !== announced) { announced = text; $('announce').textContent = text; }
  }
  function show(name) {
    for (var key in views) { if (Object.prototype.hasOwnProperty.call(views, key)) { views[key].hidden = key !== name; } }
    current = name;
    if (name !== 'qr') { shownQr = ''; $('qr-img').removeAttribute('src'); }
    announce();
  }
  function wait(key) {
    $('wait-title').textContent = T[key];
    show('wait');
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function tick() {
    if (!deadline) { return; }
    var left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    var label = pad(Math.floor(left / 60)) + ':' + pad(left % 60);
    $('qr-countdown').textContent = label;
    $('pw-countdown').textContent = label;
  }
  function stop() {
    done = true;
    window.clearTimeout(timer);
    window.clearInterval(ticker);
    stored('clear');
  }
  function end(kind, titleKey, textKey, code, override) {
    stop();
    $('end-icon').className = 'icon icon-' + kind;
    $('end-title').textContent = T[titleKey];
    var body = $('end-text');
    var text = override || (textKey ? T[textKey] : '');
    body.hidden = !text;
    body.textContent = text;
    var line = $('end-code');
    var safe = typeof code === 'string' && /^[a-z0-9_]{1,64}$/.test(code);
    line.hidden = !safe;
    line.textContent = safe ? T.codeLabel + ' ' + code : '';
    show('end');
  }
  // flood_wait: Telegram's own retry time, shown in whole minutes (at least one).
  function floodText(state) {
    var seconds = state.retry_after;
    if (state.code !== 'flood_wait' || typeof seconds !== 'number' || !isFinite(seconds) || seconds <= 0) { return ''; }
    return T.retryAfter.replace('{n}', String(Math.max(1, Math.ceil(seconds / 60))));
  }
  function closed() {
    if (cancelling) { end('idle', 'cancelledTitle', 'retryText', null); } else { end('idle', 'closedTitle', 'closedText', null); }
  }

  function renderPassword(state) {
    var pending = !!state.password_pending || submitting;
    var hint = typeof state.password_hint === 'string' ? state.password_hint : '';
    var hintNode = $('pw-hint');
    hintNode.hidden = !hint;
    hintNode.textContent = hint ? T.pwHint + ' ' + hint : '';
    var errorNode = $('pw-error');
    var message = localError ? T.sendFailed : (state.password_error && !pending ? T.pwError : '');
    errorNode.hidden = !message;
    errorNode.textContent = message;
    input.disabled = pending;
    submit.disabled = pending;
    submit.textContent = pending ? T.pwChecking : T.pwSubmit;
    var entering = current !== 'password';
    show('password');
    if ((entering || wasBusy) && !pending) { input.focus(); }
    wasBusy = pending;
  }

  function apply(state) {
    lastState = state;
    if (typeof state.expires_in === 'number' && isFinite(state.expires_in)) {
      var target = Date.now() + state.expires_in * 1000;
      if (!deadline || Math.abs(target - deadline) > 1500) { deadline = target; }
      tick();
    }
    var phase = KNOWN.indexOf(state.phase) >= 0 ? state.phase : 'starting';
    if (cancelling && TERMINAL.indexOf(phase) < 0) { wait('cancelling'); return; }
    if (phase === 'waiting_for_scan') {
      if (typeof state.qr === 'string' && state.qr.indexOf('data:image/svg+xml') === 0) {
        if (state.qr !== shownQr) { shownQr = state.qr; $('qr-img').src = state.qr; }
        show('qr');
      } else { wait('preparing'); }
    } else if (phase === 'waiting_for_password') {
      renderPassword(state);
    } else if (phase === 'authorizing' || phase === 'handing_off') {
      wait('finishing');
    } else if (phase === 'ready') {
      end('ok', 'readyTitle', null, null);
    } else if (phase === 'failed') {
      end('bad', 'failedTitle', 'failedText', state.code, floodText(state));
    } else if (phase === 'expired') {
      end('idle', 'expiredTitle', 'retryText', null);
    } else if (phase === 'cancelled') {
      end('idle', 'cancelledTitle', 'retryText', null);
    } else {
      wait('connecting');
    }
  }

  function api(path, method, body) {
    var headers = { 'Authorization': 'Bearer ' + token };
    var init = { method: method, headers: headers, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    // A starved or vanished server must not leave the page waiting forever.
    var controller = new AbortController();
    init.signal = controller.signal;
    var limit = window.setTimeout(function () { controller.abort(); }, 5000);
    return window.fetch(path, init).finally(function () { window.clearTimeout(limit); });
  }

  function poll() {
    window.clearTimeout(timer);
    if (done) { return; }
    sequence += 1;
    var mine = sequence;
    api('/api/state', 'GET').then(function (response) {
      if (response.status === 401) { return Promise.reject('unauthorized'); }
      if (!response.ok) { return Promise.reject('http'); }
      return response.json();
    }).then(function (state) {
      if (mine !== sequence || done) { return; }
      if (!state || typeof state !== 'object') { return Promise.reject('shape'); }
      failures = 0;
      apply(state);
    }).catch(function (reason) {
      if (mine !== sequence || done) { return; }
      failures += 1;
      if (reason === 'unauthorized' || failures >= MAX_FAILURES) { closed(); }
    }).then(function () {
      if (mine === sequence && !done) { timer = window.setTimeout(poll, POLL_MS); }
    });
  }

  function cancel() {
    if (cancelling || done) { return; }
    cancelling = true;
    wait('cancelling');
    api('/api/cancel', 'POST', {}).catch(function () { return null; }).then(poll);
  }

  $('qr-cancel').addEventListener('click', cancel);
  $('pw-cancel').addEventListener('click', cancel);
  $('pw-form').addEventListener('submit', function (event) {
    event.preventDefault();
    if (submitting || done || cancelling) { return; }
    var value = input.value;
    if (!value) { input.focus(); return; }
    submitting = true;
    localError = false;
    if (lastState) { renderPassword(lastState); }
    api('/api/password', 'POST', { password: value }).then(function (response) {
      if (response.ok) { input.value = ''; } else if (response.status !== 409) { localError = true; }
    }, function () { localError = true; }).then(function () {
      value = '';
      submitting = false;
      poll();
    });
  });

  tick();
  ticker = window.setInterval(tick, 250);
  if (!token) { closed(); } else { poll(); }
})();
"""

APP_JS = _JS_TEMPLATE.replace('__STRINGS__', json.dumps(STRINGS, ensure_ascii=False, indent=2))

APP_CSS = """:root {
  --bg: #f3f5f8; --card: #ffffff; --fg: #151a22; --muted: #5d6877; --line: #dde2ea;
  --accent: #1b6fd8; --accent-fg: #ffffff; --danger: #b3242b; --ok: #1a7f43; --idle: #5d6877;
  --focus: #1b6fd8; --shadow: 0 1px 2px rgba(15, 23, 42, .05), 0 12px 32px rgba(15, 23, 42, .08);
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1218; --card: #171b23; --fg: #e9ecf1; --muted: #9aa4b2; --line: #2a303b;
    --accent: #5aa2ff; --accent-fg: #0b1220; --danger: #ff8f8f; --ok: #5fd68a; --idle: #9aa4b2;
    --focus: #8bbcff; --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 12px 32px rgba(0, 0, 0, .35);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; padding: 16px; display: flex; align-items: center; justify-content: center;
  background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
.card {
  width: 100%; max-width: 28rem; padding: 24px 20px; text-align: center;
  background: var(--card); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow);
}
.brand { margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
h1 { margin: 0; font-size: 1.125rem; font-weight: 650; letter-spacing: .01em; }
h2 { margin: 0 0 12px; font-size: 1.25rem; line-height: 1.3; font-weight: 600; overflow-wrap: anywhere; }
p { margin: 0 0 12px; overflow-wrap: anywhere; }
.disclosure { margin: 4px 0 0; color: var(--muted); font-size: .875rem; }
.muted { color: var(--muted); font-size: .875rem; }
.sr-only {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
.view { padding: 4px 0; }
.status { font-weight: 500; }
.spinner {
  width: 40px; height: 40px; margin: 8px auto 16px; border-radius: 50%;
  border: 4px solid var(--line); border-top-color: var(--accent); animation: spin .9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 3s; } }
.steps { margin: 0 0 16px; padding: 0; list-style: none; counter-reset: step; text-align: left; }
.steps li {
  position: relative; margin: 0 0 8px; padding-left: 36px; min-height: 28px; counter-increment: step;
  overflow-wrap: anywhere;
}
.steps li::before {
  content: counter(step); position: absolute; left: 0; top: 0; width: 28px; height: 28px; border-radius: 50%;
  background: var(--accent); color: var(--accent-fg); font-size: .875rem; font-weight: 600;
  display: flex; align-items: center; justify-content: center;
}
.qr-frame {
  width: min(100%, 288px); margin: 4px auto 12px; padding: 8px; background: #ffffff;
  border: 1px solid var(--line); border-radius: 12px; line-height: 0;
}
.qr { display: block; width: 100%; height: auto; aspect-ratio: 1; }
.timer { color: var(--muted); font-size: .9375rem; }
.countdown { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--fg); }
.form { display: flex; flex-direction: column; gap: 8px; text-align: left; margin-bottom: 12px; }
label { font-weight: 600; font-size: .9375rem; }
input[type="password"] {
  width: 100%; min-height: 48px; padding: 10px 12px; font: inherit; color: var(--fg);
  background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
}
input[type="password"]:disabled { opacity: .6; }
.hint { margin: 0; color: var(--muted); font-size: .9375rem; }
.error { margin: 0; color: var(--danger); font-size: .9375rem; font-weight: 500; }
.btn {
  min-height: 48px; padding: 10px 18px; font: inherit; font-weight: 600; border-radius: 10px;
  border: 1px solid transparent; cursor: pointer;
}
.btn:disabled { opacity: .6; cursor: default; }
.btn-primary { margin-top: 4px; background: var(--accent); color: var(--accent-fg); }
.btn-quiet { background: transparent; color: var(--muted); border-color: var(--line); }
.btn:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.icon {
  position: relative; width: 56px; height: 56px; margin: 4px auto 14px; border-radius: 50%;
  border: 3px solid var(--idle);
}
.icon::after { position: absolute; left: 0; right: 0; top: 0; bottom: 0; text-align: center; line-height: 50px; font-size: 1.6rem; font-weight: 700; }
.icon-ok { border-color: var(--ok); }
.icon-ok::after { content: "\\2713"; color: var(--ok); }
.icon-bad { border-color: var(--danger); }
.icon-bad::after { content: "!"; color: var(--danger); }
.icon-idle::after { content: "\\00b7\\00b7\\00b7"; color: var(--idle); letter-spacing: 1px; }
@media (max-width: 360px) { .card { padding: 20px 14px; } }
"""

ASSETS = {
    '/': ('text/html; charset=utf-8', INDEX_HTML.encode('utf-8')),
    '/app.js': ('text/javascript; charset=utf-8', APP_JS.encode('utf-8')),
    '/app.css': ('text/css; charset=utf-8', APP_CSS.encode('utf-8')),
}
