// Janelas (<dialog class="form-dialog">) abertas por um botão data-dialog="<id>"
// (templates/_dialog.html): o "Não aceitar o placar" das confirmações de placar.
// data-dialog-close fecha; clicar fora da janela também.
(function () {
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-dialog]');
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute('data-dialog'));
      if (dialog && !dialog.open) {
        dialog.showModal();
        var field = dialog.querySelector('textarea, input:not([type=hidden])');
        if (field) field.focus();
      }
      return;
    }
    if (e.target.closest('[data-dialog-close]')) {
      e.target.closest('dialog').close();
      return;
    }
    if (e.target.matches('dialog.form-dialog')) e.target.close();  // clique no fundo, fora da janela
  });
  // data-auto-open: abre sozinha ao carregar a página (a "Conecte sua Conta
  // WE Camp no Kaillera" do base.html - templates/_wecamp_login.html).
  var auto = document.querySelector('dialog[data-auto-open]');
  if (auto && !auto.open && auto.showModal) auto.showModal();
})();
