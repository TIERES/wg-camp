// Olho que mostra/esconde a senha (templates/_password.html: password_toggle):
// troca o type do <input> do mesmo .input-icon entre password e text.
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-password-toggle]');
    if (!btn) return;
    e.preventDefault();  // dentro de um <label>: não repassa o clique para o campo
    var input = btn.parentNode.querySelector('input');
    if (!input) return;
    var show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    var label = show ? 'Esconder senha' : 'Mostrar senha';
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.setAttribute('aria-label', label);
    btn.title = label;
    input.focus();
    var end = input.value.length;
    try { input.setSelectionRange(end, end); } catch (err) { /* type sem seleção */ }
  });
})();
