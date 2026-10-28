(function () {
  'use strict';
  document.addEventListener('click', function (event) {
    var open = event.target.dataset.skipToggle;
    var cancel = event.target.dataset.skipCancel;
    if (open) document.getElementById(open).hidden = false;
    if (cancel) {
      var panel = document.getElementById(cancel);
      panel.reset();
      panel.hidden = true;
    }
  });
  document.addEventListener('change', function (event) {
    if (!event.target.matches('.skip-reason')) return;
    var other = event.target.closest('.skip-panel').querySelector('.other-reason');
    other.hidden = event.target.value !== 'OTHER';
    other.required = event.target.value === 'OTHER';
    if (!other.required) other.value = '';
  });
}());
