(function () {
  const forms = document.querySelectorAll('form[data-confirm]');
  forms.forEach((form) => {
    form.addEventListener('submit', (e) => {
      const msg = form.getAttribute('data-confirm');
      if (msg && !confirm(msg)) {
        e.preventDefault();
      }
    });
  });

  const disableOnSubmit = document.querySelectorAll('form[data-disable-on-submit]');
  disableOnSubmit.forEach((form) => {
    form.addEventListener('submit', () => {
      const btn = form.querySelector('button[type="submit"], button:not([type])');
      if (btn) {
        btn.disabled = true;
        btn.dataset.originalText = btn.innerHTML;
        btn.innerHTML = '...';
      }
    });
  });
})();
