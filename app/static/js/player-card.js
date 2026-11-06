// Resumo do jogador num popup: qualquer elemento com data-player-card="<url>"
// (miniatura da foto nas listas - templates/_avatar.html) abre um <dialog> com o
// HTML de tournaments.player_card. Fecha no ✕, com Esc ou clicando fora.
(function () {
  var dialog = null;
  function open() {
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.className = 'player-dialog';
      dialog.setAttribute('aria-label', 'Resumo do jogador');
      dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
      document.body.appendChild(dialog);
    }
    if (!dialog.open) dialog.showModal();
    return dialog;
  }
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-player-card]');
    if (!trigger) return;
    e.preventDefault();
    var box = open();
    box.innerHTML = '<div class="player-card"><p class="muted">Carregando…</p></div>';
    fetch(trigger.dataset.playerCard).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      box.innerHTML = html;
    }).catch(function () {
      box.innerHTML = '<div class="player-card"><p>Não foi possível carregar o resumo agora.</p>'
        + '<form method="dialog"><button class="secondary">Fechar</button></form></div>';
    });
  });
})();
