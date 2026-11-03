(function () {
  var form = document.querySelector('[data-service-create]');
  if (!form) return;
  var sources = JSON.parse(document.getElementById('service-sources').textContent);
  var fields = ['category', 'customer_type', 'service_type', 'standard_duration_minutes', 'base_price'];
  var source = form.querySelector('[name="source"]');
  var count = form.querySelector('[data-step-count]');
  var button = form.querySelector('[data-submit-label]');

  function mode() {
    var checked = form.querySelector('[name="start"]:checked');
    return checked ? checked.value : 'copy';
  }
  function refresh() {
    var copying = mode() === 'copy';
    form.querySelectorAll('[data-copy-only]').forEach(function (el) { el.hidden = !copying; });
    source.required = copying;
    button.textContent = copying ? 'Create copy' : 'Create empty service';
    var data = copying && sources[source.value];
    count.textContent = data ? data.steps + ' step(s) will be copied.' : '';
  }
  function fill() {
    var data = sources[source.value];
    if (!data) return;
    fields.forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      if (input && !input.dataset.touched) input.value = data[name] === 'None' ? '' : data[name];
    });
    refresh();
  }
  fields.forEach(function (name) {
    var input = form.querySelector('[name="' + name + '"]');
    if (input) input.addEventListener('input', function () { input.dataset.touched = '1'; });
  });
  form.querySelectorAll('[name="start"]').forEach(function (el) { el.addEventListener('change', refresh); });
  source.addEventListener('change', fill);
  if (source.value && mode() === 'copy') fill(); else refresh();
})();
