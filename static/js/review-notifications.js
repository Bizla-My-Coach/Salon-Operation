(function () {
  'use strict';
  var menus = Array.from(document.querySelectorAll('[data-review-notifications]'));
  if (!menus.length) return;
  var busy = false;
  function showError(message) {
    menus.forEach(function (menu) {
      var error = menu.querySelector('[data-notification-error]');
      error.textContent = message;
      error.hidden = !message;
    });
  }
  function renderReminders(menu, feed) {
    var box = menu.querySelector('[data-notification-reminders]');
    if (!box) return;
    var total = feed.reminder_count || 0;
    box.hidden = total === 0;
    box.querySelector('[data-reminder-title]').textContent =
      total + ' negative review' + (total === 1 ? '' : 's') + ' to follow up';
    var overdue = box.querySelector('[data-reminder-overdue]');
    overdue.textContent = (feed.overdue_count || 0) + ' overdue';
    overdue.hidden = !feed.overdue_count;
    var list = box.querySelector('[data-reminder-list]');
    if (list.contains(document.activeElement)) return;
    var fragment = document.createDocumentFragment();
    (feed.reminders || []).forEach(function (item) {
      var link = document.createElement('a');
      link.className = 'notification-item notification-reminder' + (item.overdue ? ' is-overdue' : '');
      link.href = item.url;
      [['strong', '\u2605 ' + item.stars + ' \u00b7 ' + item.name], ['span', item.branch], ['small', item.label]].forEach(function (entry) {
        var element = document.createElement(entry[0]);
        element.textContent = entry[1];
        link.appendChild(element);
      });
      fragment.appendChild(link);
    });
    list.replaceChildren(fragment);
  }
  function render(feed) {
    menus.forEach(function (menu) {
      var badge = feed.badge_count === undefined ? feed.unread_count : feed.badge_count;
      var count = menu.querySelector('[data-notification-count]');
      count.textContent = badge > 99 ? '99+' : String(badge);
      count.hidden = badge === 0;
      menu.querySelector('summary').setAttribute('aria-label', 'Review notifications, ' + badge + ' need attention');
      renderReminders(menu, feed);
      menu.querySelector('[data-notification-total]').textContent = feed.unread_count + ' unread';
      menu.querySelector('[data-notifications-read-all] button').disabled = !feed.unread_count;
      var list = menu.querySelector('[data-notification-list]');
      var csrf = menu.querySelector('[name=csrfmiddlewaretoken]');
      var fragment = document.createDocumentFragment();
      feed.items.forEach(function (alert) {
        var form = document.createElement('form');
        form.method = 'post';
        form.action = alert.open_url;
        form.appendChild(csrf.cloneNode(true));
        var button = document.createElement('button');
        button.type = 'submit';
        button.className = 'notification-item' + (alert.unread ? ' is-unread' : '');
        [['strong', alert.subject], ['span', alert.branch + ' · ' + alert.time],
          ['small', (alert.unread ? 'Unread' : 'Read') + ' · Open review']].forEach(function (entry) {
          var element = document.createElement(entry[0]);
          element.textContent = entry[1];
          button.appendChild(element);
        });
        form.appendChild(button);
        fragment.appendChild(form);
      });
      if (!feed.items.length) {
        var empty = document.createElement('p');
        empty.className = 'notification-empty';
        empty.textContent = 'No review notifications yet.';
        fragment.appendChild(empty);
      }
      // Keep keyboard focus stable while a manager is reading the dropdown.
      if (!list.contains(document.activeElement)) list.replaceChildren(fragment);
    });
    showError('');
  }
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      var response = await fetch(menus[0].dataset.feedUrl, {
        credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}
      });
      if (!response.ok) throw new Error('Refresh failed');
      render(await response.json());
    } catch (error) {
      showError('Notifications could not refresh. Open the bell to try again.');
    } finally { busy = false; }
  }
  menus.forEach(function (menu) {
    menu.addEventListener('toggle', function () {
      if (!menu.open) return;
      document.querySelectorAll('.account-menu, [data-review-notifications]').forEach(function (other) {
        if (other !== menu) other.open = false;
      });
      refresh();
    });
    menu.querySelector('[data-notifications-read-all]').addEventListener('submit', async function (event) {
      event.preventDefault();
      var form = event.currentTarget;
      var button = form.querySelector('button');
      button.disabled = true;
      try {
        var response = await fetch(form.action, {method: 'POST', body: new FormData(form),
          credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error('Update failed');
        render(await response.json());
      } catch (error) {
        button.disabled = false;
        showError('Could not mark notifications as read. Please try again.');
      }
    });
  });
  document.addEventListener('click', function (event) {
    menus.forEach(function (menu) { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') menus.forEach(function (menu) {
      if (menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
    });
  });
  document.addEventListener('visibilitychange', refresh);
  window.addEventListener('focus', refresh);
  window.addEventListener('pageshow', refresh);
  window.setInterval(refresh, 30000);
}());
