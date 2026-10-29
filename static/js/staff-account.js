(() => {
  const form = document.querySelector('[data-create-team-member]');
  if (form) {
    const username = form.querySelector('#id_username');
    const fields = ['branch', 'first_name', 'last_name'];
    let timer;
    let version = 0;
    const update = () => {
      clearTimeout(timer);
      const current = ++version;
      username.value = '';
      timer = setTimeout(async () => {
        const url = new URL(form.dataset.previewUrl, window.location.origin);
        url.searchParams.set('preview', '1');
        fields.forEach(name => url.searchParams.set(name, form.elements[name].value));
        try {
          const response = await fetch(url, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
          if (!response.ok) return;
          const data = await response.json();
          if (current === version) username.value = data.username;
        } catch (_) {
          // Account creation still generates the username if the preview is unavailable.
        }
      }, 200);
    };
    fields.forEach(name => form.elements[name].addEventListener('input', update));
    update();
  }
  document.querySelectorAll('[data-copy-field]').forEach(button => {
    button.addEventListener('click', async () => {
      const field = document.getElementById(button.dataset.copyField);
      const status = document.querySelector('[data-copy-status]');
      try {
        await navigator.clipboard.writeText(field.value);
        status.textContent = button.dataset.copyField === 'created-password' ? 'Password copied.' : 'Username copied.';
      } catch (_) {
        field.focus();
        field.select();
        status.textContent = 'Select and copy the highlighted text.';
      }
    });
  });
})();
